import json
from pathlib import Path

import typer
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)
from rich.table import Table

from hubspot_crm_clean.audits.duplicates import find_duplicates, full_name, pair_count
from hubspot_crm_clean.audits.incomplete import (
    DEFAULT_MIN_COMPLETENESS,
    DEFAULT_REQUIRED_FIELDS,
    find_incomplete,
)
from hubspot_crm_clean.audits.stale import (
    DEFAULT_ACTIVITY_FIELDS,
    DEFAULT_INACTIVE_DAYS,
    find_stale,
)
from hubspot_crm_clean.client import all_property_names, fetch_all_contacts

app = typer.Typer(
    help="hubspot-crm-clean: audit your HubSpot CRM for data hygiene issues.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)
console = Console()

audit_app = typer.Typer(help="Run data hygiene audits.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")

# Each audit declares what it reads — don't rely on whatever HubSpot defaults to.
DUPLICATE_PROPERTIES = ["email", "firstname", "lastname"]
INCOMPLETE_PROPERTIES = ["email", "firstname", "lastname", *DEFAULT_REQUIRED_FIELDS]
STALE_PROPERTIES = ["email", "firstname", "lastname", *DEFAULT_ACTIVITY_FIELDS]
FETCH_PROPERTIES = ["email", "firstname", "lastname", "company", "phone", "lifecyclestage"]

# Shared option definitions, so every audit behaves the same way.
FROM_FILE_OPTION = typer.Option(
    None, "--from-file", exists=True, dir_okay=False, readable=True,
    help="Read contacts from a JSON file instead of calling HubSpot.",
)
STRICT_OPTION = typer.Option(
    False, "--strict", help="Exit with code 1 when findings are reported (for CI).",
)
# Hoisted like the options above: builds its help text with a call, which can't sit
# in a signature default.
REQUIRED_OPTION = typer.Option(
    None, "--required", "-r",
    help=f"Required field, repeatable. Defaults to: {', '.join(DEFAULT_REQUIRED_FIELDS)}.",
)


def load_contacts(path: Path) -> list[dict]:
    """Read contacts from a JSON file instead of calling HubSpot."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["results"] if isinstance(data, dict) else data


def resolve_contacts(from_file: Path | None, properties: list[str]) -> list[dict]:
    """Load contacts from disk or HubSpot, exiting cleanly on failure."""
    try:
        if from_file:
            return load_contacts(from_file)
        return fetch_with_progress(properties)
    except Exception as err:  # noqa: BLE001 - CLI boundary: any failure becomes a clean message
        console.print(f"[bold red]Failed to load contacts:[/bold red] {err}")
        raise typer.Exit(code=1)


def clean_panel(message: str, subtitle: str) -> Panel:
    """The 'nothing to report' box, shared by every audit."""
    return Panel(message, subtitle=subtitle, border_style="green", box=box.ROUNDED)


def summary_panel(message: str) -> Panel:
    """The findings-count box, shared by every audit."""
    return Panel(message, border_style="yellow", box=box.ROUNDED)


def audit_table(title: str) -> Table:
    """A table styled consistently across audits."""
    return Table(
        title=title, header_style="bold cyan", box=box.SIMPLE_HEAVY, title_justify="left",
    )


def fetch_with_progress(properties: list[str]) -> list[dict]:
    """Fetch every contact, showing a live running count as pages come in."""
    with Progress(
        SpinnerColumn(style="green"),
        TextColumn("[progress.description]{task.description}"),
        TimeElapsedColumn(),
        console=console,
        transient=True,    # tidy itself away once the fetch is done
    ) as progress:
        task = progress.add_task("Fetching contacts from HubSpot...", total=None)
        return fetch_all_contacts(
            properties,
            on_page=lambda count: progress.update(
                task, description=f"Fetching contacts from HubSpot... [bold]{count}[/bold] so far"
            ),
        )


def batched_advance(progress: Progress, task, every: int = 2000):
    """Advance a progress task in chunks.

    find_duplicates is O(n^2), so on a large account the callback fires millions of
    times. Rich's per-call cost adds ~45% to the comparison phase at that volume;
    batching makes it negligible while staying visually smooth.
    """
    pending = 0

    def advance(amount):
        nonlocal pending
        pending += amount
        if pending >= every:
            progress.advance(task, pending)
            pending = 0

    return advance


def confidence_style(score: float) -> str:
    """Colour-code how much to trust a cluster."""
    if score >= 95:
        return "bold green"
    if score >= 90:
        return "yellow"
    return "dark_orange"


def completeness_style(score: float) -> str:
    """Colour-code how badly a contact is missing data. Lower is worse."""
    if score >= 50:
        return "yellow"
    if score > 0:
        return "dark_orange"
    return "bold red"   # nothing at all


def staleness_style(days: int, cutoff: int) -> str:
    """Colour-code how far past the cutoff a contact has drifted."""
    if days >= cutoff * 4:
        return "bold red"
    if days >= cutoff * 2:
        return "dark_orange"
    return "yellow"


@app.command()
def fetch(
    all_properties: bool = typer.Option(
        False, "--all-properties", "-a",
        help="Request every property defined on the contact object, custom ones included. "
             "Much larger payloads - useful for exploring what your portal actually stores.",
    ),
):
    """Fetch contacts from HubSpot and print how many were found."""
    try:
        if all_properties:
            with console.status("[bold green]Listing contact properties...[/bold green]"):
                properties = all_property_names("contacts")
            console.print(f"[dim]Requesting {len(properties)} properties per contact.[/dim]")
        else:
            properties = FETCH_PROPERTIES
        contacts = fetch_with_progress(properties)
    except Exception as err:  # noqa: BLE001 - CLI boundary: any failure becomes a clean message
        console.print(f"[bold red]Failed to fetch contacts:[/bold red] {err}")
        raise typer.Exit(code=1)
    console.print(f"[bold green]Found {len(contacts)} contacts.[/bold green]")

@audit_app.command("duplicates")
def audit_duplicates(
    threshold: int = typer.Option(
        85, "--threshold", "-t", min=0, max=100,
        help="Name similarity score (0-100) required to call two contacts a match.",
    ),
    from_file: Path | None = FROM_FILE_OPTION,
    strict: bool = STRICT_OPTION,
):
    """Detect probable duplicate contacts."""
    contacts = resolve_contacts(from_file, DUPLICATE_PROPERTIES)

    with Progress(
        SpinnerColumn(style="green"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style="green", finished_style="green"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,    # leave the results, not the scaffolding
    ) as progress:
        total_pairs = pair_count(contacts)
        task = progress.add_task("Comparing names", total=total_pairs)
        clusters = find_duplicates(
            contacts,
            threshold=threshold,
            on_progress=batched_advance(progress, task),
        )
        progress.update(task, completed=total_pairs)   # flush the last partial batch

    if not clusters:
        console.print(clean_panel(
            f"[bold green]No duplicates found[/bold green] in {len(contacts)} contacts.",
            f"threshold {threshold}",
        ))
        return

    table = audit_table(f"[bold]Probable duplicates[/bold]  [dim](threshold {threshold})[/dim]")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Conf", justify="right")
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Email", style="cyan")

    for number, cluster in enumerate(clusters, start=1):
        style = confidence_style(cluster.confidence)
        for row, member in enumerate(cluster.members):
            first_row = row == 0  # only label the cluster once, on its first line
            table.add_row(
                str(number) if first_row else "",
                f"[{style}]{cluster.confidence:.0f}[/{style}]" if first_row else "",
                str(member["id"]),
                full_name(member) or "[dim]-[/dim]",
                member["properties"].get("email") or "[dim]-[/dim]",
            )
        table.add_section()

    console.print(table)
    involved = sum(len(cluster.members) for cluster in clusters)
    console.print(summary_panel(
        f"[bold yellow]{len(clusters)}[/bold yellow] cluster(s)  [dim]|[/dim]  "
        f"[bold yellow]{involved}[/bold yellow] contacts involved  [dim]|[/dim]  "
        f"[dim]{len(contacts)} scanned[/dim]"
    ))
    if strict:
        raise typer.Exit(code=1)


@audit_app.command("incomplete")
def audit_incomplete(
    min_completeness: int = typer.Option(
        DEFAULT_MIN_COMPLETENESS, "--min-completeness", "-m", min=0, max=100,
        help="Flag contacts scoring below this percentage of required fields.",
    ),
    required: list[str] | None = REQUIRED_OPTION,
    from_file: Path | None = FROM_FILE_OPTION,
    strict: bool = STRICT_OPTION,
):
    """Flag contacts missing required fields."""
    fields = list(required) if required else DEFAULT_REQUIRED_FIELDS
    properties = ["email", "firstname", "lastname", *fields]
    contacts = resolve_contacts(from_file, properties)

    flagged = find_incomplete(contacts, fields, min_completeness)

    if not flagged:
        console.print(clean_panel(
            f"[bold green]All {len(contacts)} contacts[/bold green] meet the completeness bar.",
            f"min {min_completeness}% of {len(fields)} fields",
        ))
        return

    table = audit_table(
        f"[bold]Incomplete contacts[/bold]  [dim](below {min_completeness}%)[/dim]"
    )
    table.add_column("Score", justify="right")
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Email", style="cyan")
    table.add_column("Missing")

    for item in flagged:
        style = completeness_style(item.score)
        table.add_row(
            f"[{style}]{item.score:.0f}%[/{style}]",
            str(item.contact["id"]),
            full_name(item.contact) or "[dim]-[/dim]",
            item.contact["properties"].get("email") or "[dim]-[/dim]",
            "[dim]" + ", ".join(item.missing) + "[/dim]",
        )

    console.print(table)
    console.print(summary_panel(
        f"[bold yellow]{len(flagged)}[/bold yellow] incomplete  [dim]|[/dim]  "
        f"[dim]{len(contacts)} scanned  |  required: {', '.join(fields)}[/dim]"
    ))
    if strict:
        raise typer.Exit(code=1)


@audit_app.command("stale")
def audit_stale(
    inactive_days: int = typer.Option(
        DEFAULT_INACTIVE_DAYS, "--inactive-days", "-d", min=0,
        help="Flag contacts with no activity in at least this many days.",
    ),
    from_file: Path | None = FROM_FILE_OPTION,
    strict: bool = STRICT_OPTION,
):
    """Flag contacts with no recent activity."""
    contacts = resolve_contacts(from_file, STALE_PROPERTIES)

    flagged = find_stale(contacts, inactive_days=inactive_days)

    if not flagged:
        console.print(clean_panel(
            f"[bold green]All {len(contacts)} contacts[/bold green] show recent activity.",
            f"within {inactive_days} days",
        ))
        return

    table = audit_table(
        f"[bold]Stale contacts[/bold]  [dim](no activity in {inactive_days}+ days)[/dim]"
    )
    table.add_column("Days", justify="right")
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Email", style="cyan")
    table.add_column("Last activity")

    never = 0
    for item in flagged:
        if item.days_inactive is None:
            never += 1
            days_cell, last_cell = "[dim]never[/dim]", "[dim]-[/dim]"
        else:
            style = staleness_style(item.days_inactive, inactive_days)
            days_cell = f"[{style}]{item.days_inactive}[/{style}]"
            last_cell = item.last_seen.date().isoformat()
        table.add_row(
            days_cell,
            str(item.contact["id"]),
            full_name(item.contact) or "[dim]-[/dim]",
            item.contact["properties"].get("email") or "[dim]-[/dim]",
            last_cell,
        )

    console.print(table)
    never_note = f"  [dim]|[/dim]  [dim]{never} never seen[/dim]" if never else ""
    console.print(summary_panel(
        f"[bold yellow]{len(flagged)}[/bold yellow] stale  [dim]|[/dim]  "
        f"[dim]{len(contacts)} scanned[/dim]{never_note}"
    ))
    if strict:
        raise typer.Exit(code=1)


@audit_app.command("all")
def audit_all():
    """Run every audit at once (Week 4)."""
    raise NotImplementedError


if __name__ == "__main__":
    app()

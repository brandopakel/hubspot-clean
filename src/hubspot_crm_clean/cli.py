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

from hubspot_crm_clean.audits.duplicates import (
    DEFAULT_THRESHOLD,
    excluded_from_matching,
    find_duplicates,
    full_name,
    pair_count,
)
from hubspot_crm_clean.audits.incomplete import (
    DEFAULT_MIN_COMPLETENESS,
    DEFAULT_REQUIRED_FIELDS,
    completeness_score,
    find_incomplete,
)
from hubspot_crm_clean.audits.stale import (
    DEFAULT_ACTIVITY_FIELDS,
    DEFAULT_INACTIVE_DAYS,
    find_stale,
    unparseable_dates,
)
from hubspot_crm_clean.client import (
    all_property_names,
    archive_contact,
    fetch_all_contacts,
    merge_contacts,
)
from hubspot_crm_clean.config import (
    DEFAULT_CONFIG_NAME,
    DEFAULTS,
    ConfigError,
    find_config,
    load_config,
)
from hubspot_crm_clean.fixes import (
    UNKNOWN_DATE,
    ArchiveOutcome,
    MergeOutcome,
    apply_archives,
    apply_plans,
    archive_failures,
    created,
    failure_count,
    merge_count,
    plan_archives,
    plan_merges,
    repoint,
)
from hubspot_crm_clean.reports import (
    ReportError,
    ReportFormat,
    archives_section,
    duplicates_section,
    incomplete_section,
    merges_section,
    resolve_target,
    stale_section,
    streams_to_stdout,
    write_report,
)

app = typer.Typer(
    help="hubspot-crm-clean: audit your HubSpot CRM for data hygiene issues, "
         "then fix them.",
    rich_markup_mode="rich",
    no_args_is_help=True,
)
console = Console()

audit_app = typer.Typer(help="Run data hygiene audits.", no_args_is_help=True)
app.add_typer(audit_app, name="audit")

fix_app = typer.Typer(
    help="Fix what the audits find. Every command previews by default and "
         "writes nothing until you ask it to.",
    no_args_is_help=True,
)
app.add_typer(fix_app, name="fix")

# Each audit declares what it reads — don't rely on whatever HubSpot defaults to.
# Which properties an audit needs now depends on config, so the field-driven sets
# are built per run; only these two are fixed.
IDENTITY_PROPERTIES = ["email", "firstname", "lastname"]     # every audit table shows these
FETCH_PROPERTIES = [*IDENTITY_PROPERTIES, "company", "phone", "lifecyclestage"]
# merge needs createdate on top of whatever it scores completeness against: the
# oldest record wins the tie-break when two are equally complete.
MERGE_PROPERTIES = [*IDENTITY_PROPERTIES, "createdate"]

# Shared option definitions, so every audit behaves the same way.
FROM_FILE_OPTION = typer.Option(
    None, "--from-file", exists=True, dir_okay=False, readable=True,
    help="Read contacts from a JSON file instead of calling HubSpot.",
)
STRICT_OPTION = typer.Option(
    False, "--strict", help="Exit with code 1 when findings are reported (for CI).",
)
VERBOSE_OPTION = typer.Option(
    False, "--verbose", "-v",
    help="List the records an audit could not consider, and why. "
         "The counts are always reported; this shows which contacts they are.",
)
CONFIG_OPTION = typer.Option(
    None, "--config", "-c", exists=True, dir_okay=False, readable=True,
    help=f"YAML file supplying defaults. Falls back to ./{DEFAULT_CONFIG_NAME} when it exists.",
)
# The tuning options all default to None rather than their real default, because
# that is the only way to tell "user asked for 85" from "user said nothing" — and
# a config file may only fill in the second case. show_default carries the real
# value into --help, which would otherwise advertise `None`.
THRESHOLD_OPTION = typer.Option(
    None, "--threshold", "-t", min=0, max=100, show_default=str(DEFAULT_THRESHOLD),
    help="Name similarity score (0-100) required to call two contacts a match.",
)
MIN_COMPLETENESS_OPTION = typer.Option(
    None, "--min-completeness", "-m", min=0, max=100,
    show_default=str(DEFAULT_MIN_COMPLETENESS),
    help="Flag contacts scoring below this percentage of required fields.",
)
INACTIVE_DAYS_OPTION = typer.Option(
    None, "--inactive-days", "-d", min=0, show_default=str(DEFAULT_INACTIVE_DAYS),
    help="Flag contacts with no activity in at least this many days.",
)
# Hoisted like the options above: builds its help text with a call, which can't sit
# in a signature default.
REQUIRED_OPTION = typer.Option(
    None, "--required", "-r",
    help=f"Required field, repeatable. Defaults to: {', '.join(DEFAULT_REQUIRED_FIELDS)}.",
)
ACTIVITY_FIELD_OPTION = typer.Option(
    None, "--activity-field",
    help="Timestamp field counted as activity, repeatable. "
         f"Defaults to: {', '.join(DEFAULT_ACTIVITY_FIELDS)}.",
)
FORMAT_OPTION = typer.Option(
    None, "--format", "-f",
    help="Export findings in this format. Inferred from --output's extension when omitted.",
)
# No exists=/writable= checks: this path is being written, not read, and "-" has
# to survive as a literal rather than being validated as a filename.
OUTPUT_OPTION = typer.Option(
    None, "--output", "-o", dir_okay=False,
    help="Write the report here; '-' streams it to stdout. "
         "Passing --format without --output streams to stdout too.",
)
SAVE_OPTION = typer.Option(
    False, "--save", "-s",
    help="Write a report to a generated filename in the current directory, "
         "e.g. hubspot-duplicates-20260727-143022.json. Defaults to json.",
)
# `fix` only. --apply is what changes your CRM, so it is opt-in, spelled out in
# full, and has no short form to fat-finger.
APPLY_OPTION = typer.Option(
    False, "--apply",
    help="Actually perform the merges. Without this, nothing is written.",
)
# --archive is the same flag under the name the plan uses: for stale records the
# fix *is* archiving, so `fix stale --archive` reads better than `--apply`.
ARCHIVE_OPTION = typer.Option(
    False, "--apply", "--archive",
    help="Actually archive the contacts. Without this, nothing is written.",
)
YES_OPTION = typer.Option(
    False, "--yes", "-y", help="Skip the confirmation prompt (for scripts and CI).",
)
INTERACTIVE_OPTION = typer.Option(
    False, "--interactive", "-i",
    help="Review each cluster and choose which record survives. "
         "Enter accepts the suggestion, s skips the cluster, q stops reviewing.",
)
LIMIT_OPTION = typer.Option(
    None, "--limit", "-n", min=1,
    help="Act on at most this many records. They are ordered worst-first, so a "
         "limited run handles the ones we're surest about.",
)


def fail(message: str) -> typer.Exit:
    """Print an error and exit 1. Raise the return value: `raise fail(...)`.

    Un-quiets the console first. A run that dies has no report on stdout left to
    corrupt, and a silent failure is far worse than a noisy one.
    """
    console.quiet = False
    console.print(message)
    return typer.Exit(code=1)


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
        raise fail(f"[bold red]Failed to load contacts:[/bold red] {err}")


def resolve_config(explicit: Path | None):
    """Load the config supplying this run's defaults, or fall back to the built-ins.

    Which file was applied is echoed, so a config picked up from the working
    directory never changes results invisibly.
    """
    path = find_config(explicit)
    if path is None:
        return DEFAULTS
    try:
        config = load_config(path)
    except ConfigError as err:
        raise fail(f"[bold red]Bad config:[/bold red] {err}")
    console.print(f"[dim]Using config {path}[/dim]")
    return config


def resolve_report(report_format, output, default_format, multi: bool = False,
                   save: bool = False, label: str = "audit", parts=()):
    """Where this run's report goes, or None when no export was asked for.

    Called before any fetching, so a bad --format/--output pair fails in a second
    rather than after paging through the whole portal.
    """
    try:
        return resolve_target(report_format, output, default_format, multi=multi,
                              save=save, label=label, parts=parts)
    except ReportError as err:
        raise fail(f"[bold red]Bad report options:[/bold red] {err}")


def export(target, scanned: int, sections: dict) -> None:
    """Write the report, if one was asked for, and say where it went."""
    if target is None:
        return
    try:
        written = write_report(target, scanned, sections)
    except OSError as err:
        raise fail(f"[bold red]Failed to write report:[/bold red] {err}")
    for path in written:
        # soft_wrap so a long path stays on one line and survives a copy-paste
        console.print(f"[dim]Wrote {path}[/dim]", soft_wrap=True)


def pick(flag, configured):
    """Resolve one option: an explicit flag wins, otherwise the config's value.

    `is None` rather than truthiness — `-t 0` and `-d 0` are real choices.
    """
    return configured if flag is None else flag


def unique(*groups):
    """Flatten property lists into one, first occurrence wins.

    dict keys deduplicate while preserving order, which keeps the request
    stable rather than reshuffling with set iteration.
    """
    return list(dict.fromkeys(name for group in groups for name in group))


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


def duplicates_with_progress(contacts: list[dict], threshold: int) -> list:
    """find_duplicates, wrapped in the live comparison bar. Shared with `audit all`."""
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
    return clusters


def _preview(plan):
    """A plan dressed as an outcome where nothing has happened yet.

    Lets the dry run and the real run share one renderer and one report section,
    so a preview can't drift from the thing it previews.
    """
    return MergeOutcome(plan, merged=[], failures=[])


def describe(contact, required_fields) -> str:
    """One line about a record, enough to choose between two of them."""
    when = created(contact)
    age = "created ?" if when == UNKNOWN_DATE else f"created {when.date().isoformat()}"
    return (
        f"{full_name(contact) or '[dim]no name[/dim]'}  "
        f"[cyan]{contact['properties'].get('email') or '-'}[/cyan]  "
        f"[dim]{completeness_score(contact, required_fields):.0f}% complete  {age}[/dim]"
    )


def choose_interactively(plans: list, required_fields: list) -> list:
    """Walk each cluster and let the caller pick the survivor.

    Option 1 is always the rule-based suggestion, so pressing Enter through the
    whole review reproduces exactly what the non-interactive run would do.

    Skipping drops that cluster; quitting stops asking but keeps every decision
    made so far, so abandoning a long review doesn't throw away the work.
    """
    chosen = []
    for number, plan in enumerate(plans, start=1):
        members = [plan.primary, *plan.absorbed]
        console.print(
            f"\n[bold]Cluster {number} of {len(plans)}[/bold]   "
            f"[dim]confidence {plan.confidence:.0f}   {plan.domain}[/dim]"
        )
        for index, member in enumerate(members, start=1):
            marker = "[green](suggested)[/green]" if index == 1 else ""
            console.print(f"  [bold]{index}[/bold]  {describe(member, required_fields)} {marker}")

        while True:
            answer = typer.prompt(
                f"  Keep which record? 1-{len(members)}, s(kip), q(uit)", default="1"
            ).strip().lower()
            if answer in {"q", "quit"}:
                return chosen
            if answer in {"s", "skip"}:
                break
            if answer.isdigit() and 1 <= int(answer) <= len(members):
                chosen.append(repoint(plan, members[int(answer) - 1]["id"]))
                break
            console.print("  [yellow]Enter a record number, s to skip, or q to quit.[/yellow]")
    return chosen


def render_archive_plan(outcomes: list, applied: bool) -> None:
    """Print what would be, or was, taken out of the active CRM."""
    title = "[bold]Archive[/bold]  " + (
        "[dim](applied)[/dim]" if applied
        else "[bold yellow](DRY RUN - nothing written)[/bold yellow]"
    )
    table = audit_table(title)
    table.add_column("Days", justify="right")
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Email", style="cyan")
    table.add_column("Status")

    for outcome in outcomes:
        days = outcome.plan.days_inactive
        if outcome.failure is not None:
            status = f"[bold red]failed[/bold red] [dim]{outcome.failure}[/dim]"
        elif outcome.archived:
            status = "[green]archived[/green]"
        else:
            status = "[yellow]would archive[/yellow]"
        table.add_row(
            "[dim]never[/dim]" if days is None else str(days),
            str(outcome.plan.contact["id"]),
            full_name(outcome.plan.contact) or "[dim]-[/dim]",
            outcome.plan.contact["properties"].get("email") or "[dim]-[/dim]",
            status,
        )

    console.print(table)


def archive_with_progress(plans: list) -> list:
    """apply_archives, wrapped in a live progress bar."""
    with Progress(
        SpinnerColumn(style="green"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style="green", finished_style="green"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Archiving", total=len(plans))
        return apply_archives(
            plans, archive_contact, on_progress=lambda amount: progress.advance(task, amount)
        )


def _archive_preview(plan):
    """A plan dressed as an outcome where nothing has happened yet."""
    return ArchiveOutcome(plan, archived=False, failure=None)


def confirm_merge(total: int, survivors: int) -> bool:
    """Ask before writing. Merges cannot be undone from here or from HubSpot."""
    console.print(
        f"\n[bold yellow]About to merge {total} contact(s) into {survivors} "
        f"survivor(s).[/bold yellow]  [bold red]This cannot be undone.[/bold red]"
    )
    return typer.confirm("Continue?")


def confirm_archive(total: int) -> bool:
    """Ask before writing. Recoverable, unlike a merge - and the prompt says so,
    because a warning that overstates the risk gets ignored on the one that doesn't."""
    console.print(
        f"\n[bold yellow]About to archive {total} contact(s).[/bold yellow]  "
        "[dim]Restorable from HubSpot's recycle bin for 90 days.[/dim]"
    )
    return typer.confirm("Continue?")


def merge_with_progress(plans: list, total: int) -> list:
    """apply_plans, wrapped in a live progress bar."""
    with Progress(
        SpinnerColumn(style="green"),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(complete_style="green", finished_style="green"),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
        transient=True,
    ) as progress:
        task = progress.add_task("Merging", total=total)
        return apply_plans(
            plans, merge_contacts, on_progress=lambda amount: progress.advance(task, amount)
        )


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


# --------------------------------------------------------------------------
# Renderers
#
# Tables only — the summary line stays with the caller, because a single audit
# reports its own count in a panel while `audit all` folds all three into one.
# --------------------------------------------------------------------------

def render_duplicates(clusters: list, threshold: int) -> None:
    """Print the duplicate clusters table."""
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


def render_incomplete(flagged: list, min_completeness: int) -> None:
    """Print the incomplete contacts table."""
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


def render_stale(flagged: list, inactive_days: int) -> None:
    """Print the stale contacts table."""
    table = audit_table(
        f"[bold]Stale contacts[/bold]  [dim](no activity in {inactive_days}+ days)[/dim]"
    )
    table.add_column("Days", justify="right")
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Email", style="cyan")
    table.add_column("Last activity")

    for item in flagged:
        if item.days_inactive is None:
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


def never_seen(flagged: list) -> int:
    """How many stale contacts have no usable activity date at all."""
    return sum(1 for item in flagged if item.days_inactive is None)


# --------------------------------------------------------------------------
# What an audit could not look at
#
# Both audits skip records they can't read, which is correct - but a skipped
# contact is invisible in the results, and "8 scanned" reads as "8 compared".
# The counts below always print; --verbose names the contacts.
# --------------------------------------------------------------------------

def report_skipped(verbose: bool, *counts) -> None:
    """Say what the audit couldn't look at, given (count, label) pairs.

    Printed whether anything was found, and whether --verbose was
    passed. A clean result that quietly excluded records is overclaiming, and a
    warning you only see once you know to ask for it isn't a warning.
    """
    phrases = [f"{count} {label}" for count, label in counts if count]
    if not phrases:
        return
    hint = "" if verbose else "  Re-run with --verbose to list them."
    console.print(f"  [dim]{', '.join(phrases)}.{hint}[/dim]")


def render_excluded(excluded: list) -> None:
    """Print the contacts duplicate matching could never consider."""
    table = audit_table(
        "[bold]Not compared[/bold]  [dim](excluded from duplicate matching)[/dim]"
    )
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Email", style="cyan")
    table.add_column("Reason")

    for item in excluded:
        table.add_row(
            str(item.contact["id"]),
            full_name(item.contact) or "[dim]-[/dim]",
            # the raw email, deliberately unnormalized - seeing the mess is the
            # point when the reason is that we couldn't parse it
            item.contact["properties"].get("email") or "[dim]-[/dim]",
            f"[yellow]{item.reason}[/yellow]",
        )

    console.print(table)


def render_merge_plan(outcomes: list, applied: bool) -> None:
    """Print what each cluster resolves to: one survivor, the rest folded in.

    The same table serves the dry run and the real run - only the Status column
    differs. Showing a preview through different code than the thing it previews
    is how a preview ends up lying.
    """
    title = "[bold]Merges[/bold]  " + (
        "[dim](applied)[/dim]" if applied
        else "[bold yellow](DRY RUN - nothing written)[/bold yellow]"
    )
    table = audit_table(title)
    table.add_column("#", justify="right", style="dim")
    table.add_column("Conf", justify="right")
    table.add_column("", style="dim")           # Keep / Merge
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Email", style="cyan")
    table.add_column("Status")

    for number, outcome in enumerate(outcomes, start=1):
        plan = outcome.plan
        failed = dict(outcome.failures)
        style = confidence_style(plan.confidence)

        def cells(contact, plan=plan):
            return (
                str(contact["id"]),
                full_name(contact) or "[dim]-[/dim]",
                contact["properties"].get("email") or "[dim]-[/dim]",
            )

        table.add_row(
            str(number),
            f"[{style}]{plan.confidence:.0f}[/{style}]",
            "[bold green]keep[/bold green]",
            *cells(plan.primary),
            f"[dim]{plan.reason}[/dim]",
        )
        for contact in plan.absorbed:
            if contact["id"] in failed:
                status = f"[bold red]failed[/bold red] [dim]{failed[contact['id']]}[/dim]"
            elif contact["id"] in outcome.merged:
                status = "[green]merged[/green]"
            else:
                status = "[yellow]would merge[/yellow]"
            table.add_row("", "", "[dim]merge[/dim]", *cells(contact), status)
        table.add_section()

    console.print(table)


def render_unparseable(items: list) -> None:
    """Print the contacts whose activity dates are present but unreadable."""
    table = audit_table(
        "[bold]Unreadable dates[/bold]  [dim](counted as no activity)[/dim]"
    )
    table.add_column("ID", style="dim")
    table.add_column("Name")
    table.add_column("Email", style="cyan")
    table.add_column("Field")
    table.add_column("Value")

    for item in items:
        props = item.contact["properties"]
        for row, field in enumerate(item.fields):
            first_row = row == 0     # label the contact once, like the cluster table
            table.add_row(
                str(item.contact["id"]) if first_row else "",
                (full_name(item.contact) or "[dim]-[/dim]") if first_row else "",
                (props.get("email") or "[dim]-[/dim]") if first_row else "",
                field,
                f"[yellow]{props.get(field)}[/yellow]",
            )

    console.print(table)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

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
    threshold: int | None = THRESHOLD_OPTION,
    from_file: Path | None = FROM_FILE_OPTION,
    strict: bool = STRICT_OPTION,
    verbose: bool = VERBOSE_OPTION,
    config: Path | None = CONFIG_OPTION,
    report_format: ReportFormat | None = FORMAT_OPTION,
    output: Path | None = OUTPUT_OPTION,
    save: bool = SAVE_OPTION,
):
    """Detect probable duplicate contacts."""
    # Set unconditionally, never toggled off later: the console is a module global,
    # so leaving it quiet would silence the next run in the same process.
    console.quiet = streams_to_stdout(report_format, output, save)
    settings = resolve_config(config)
    threshold = pick(threshold, settings.threshold)
    target = resolve_report(report_format, output, settings.default_format,
                            save=save, label='duplicates')

    contacts = resolve_contacts(from_file, IDENTITY_PROPERTIES)
    clusters = duplicates_with_progress(contacts, threshold)
    excluded = excluded_from_matching(contacts)

    if not clusters:
        console.print(clean_panel(
            f"[bold green]No duplicates found[/bold green] in {len(contacts)} contacts.",
            f"threshold {threshold}",
        ))
    else:
        render_duplicates(clusters, threshold)

    if verbose and excluded:
        render_excluded(excluded)

    if clusters:
        involved = sum(len(cluster.members) for cluster in clusters)
        console.print(summary_panel(
            f"[bold yellow]{len(clusters)}[/bold yellow] cluster(s)  [dim]|[/dim]  "
            f"[bold yellow]{involved}[/bold yellow] contacts involved  [dim]|[/dim]  "
            f"[dim]{len(contacts)} scanned[/dim]"
        ))
    report_skipped(verbose, (len(excluded), "not compared"))

    # After rendering and before --strict: a clean run still owes you a report,
    # and exiting 1 must not be what decides whether the file gets written.
    export(target, len(contacts), {"duplicates": duplicates_section(clusters, threshold)})
    if clusters and strict:
        raise typer.Exit(code=1)


@audit_app.command("incomplete")
def audit_incomplete(
    min_completeness: int | None = MIN_COMPLETENESS_OPTION,
    required: list[str] | None = REQUIRED_OPTION,
    from_file: Path | None = FROM_FILE_OPTION,
    strict: bool = STRICT_OPTION,
    verbose: bool = VERBOSE_OPTION,
    config: Path | None = CONFIG_OPTION,
    report_format: ReportFormat | None = FORMAT_OPTION,
    output: Path | None = OUTPUT_OPTION,
    save: bool = SAVE_OPTION,
):
    """Flag contacts missing required fields."""
    console.quiet = streams_to_stdout(report_format, output, save)
    settings = resolve_config(config)
    min_completeness = pick(min_completeness, settings.min_completeness)
    fields = list(required) if required else settings.required_fields
    target = resolve_report(report_format, output, settings.default_format,
                            save=save, label='incomplete')

    contacts = resolve_contacts(from_file, unique(IDENTITY_PROPERTIES, fields))
    flagged = find_incomplete(contacts, fields, min_completeness)

    if verbose:
        # this audit skips nothing - every contact is scored, and a blank field is
        # the finding rather than a reason to look away. Saying so beats a -v that
        # silently prints nothing and leaves you wondering.
        console.print(
            f"  [dim]All {len(contacts)} contact(s) were scored; "
            f"this audit skips nothing.[/dim]"
        )

    if not flagged:
        console.print(clean_panel(
            f"[bold green]All {len(contacts)} contacts[/bold green] meet the completeness bar.",
            f"min {min_completeness}% of {len(fields)} fields",
        ))
    else:
        render_incomplete(flagged, min_completeness)
        console.print(summary_panel(
            f"[bold yellow]{len(flagged)}[/bold yellow] incomplete  [dim]|[/dim]  "
            f"[dim]{len(contacts)} scanned  |  required: {', '.join(fields)}[/dim]"
        ))

    export(target, len(contacts), {
        "incomplete": incomplete_section(flagged, min_completeness, fields),
    })
    if flagged and strict:
        raise typer.Exit(code=1)


@audit_app.command("stale")
def audit_stale(
    inactive_days: int | None = INACTIVE_DAYS_OPTION,
    activity_field: list[str] | None = ACTIVITY_FIELD_OPTION,
    from_file: Path | None = FROM_FILE_OPTION,
    strict: bool = STRICT_OPTION,
    verbose: bool = VERBOSE_OPTION,
    config: Path | None = CONFIG_OPTION,
    report_format: ReportFormat | None = FORMAT_OPTION,
    output: Path | None = OUTPUT_OPTION,
    save: bool = SAVE_OPTION,
):
    """Flag contacts with no recent activity."""
    console.quiet = streams_to_stdout(report_format, output, save)
    settings = resolve_config(config)
    inactive_days = pick(inactive_days, settings.inactive_days)
    fields = list(activity_field) if activity_field else settings.activity_fields
    target = resolve_report(report_format, output, settings.default_format,
                            save=save, label='stale')

    contacts = resolve_contacts(from_file, unique(IDENTITY_PROPERTIES, fields))
    flagged = find_stale(contacts, inactive_days=inactive_days, activity_fields=fields)
    unreadable = unparseable_dates(contacts, fields)

    if not flagged:
        console.print(clean_panel(
            f"[bold green]All {len(contacts)} contacts[/bold green] show recent activity.",
            f"within {inactive_days} days",
        ))
    else:
        render_stale(flagged, inactive_days)

    if verbose and unreadable:
        render_unparseable(unreadable)

    if flagged:
        never = never_seen(flagged)
        never_note = f"  [dim]|[/dim]  [dim]{never} never seen[/dim]" if never else ""
        console.print(summary_panel(
            f"[bold yellow]{len(flagged)}[/bold yellow] stale  [dim]|[/dim]  "
            f"[dim]{len(contacts)} scanned[/dim]{never_note}"
        ))
    report_skipped(verbose, (len(unreadable), "unreadable date(s)"))

    export(target, len(contacts), {
        "stale": stale_section(flagged, inactive_days, fields),
    })
    if flagged and strict:
        raise typer.Exit(code=1)


@audit_app.command("all")
def audit_all(
    threshold: int | None = THRESHOLD_OPTION,
    min_completeness: int | None = MIN_COMPLETENESS_OPTION,
    required: list[str] | None = REQUIRED_OPTION,
    inactive_days: int | None = INACTIVE_DAYS_OPTION,
    activity_field: list[str] | None = ACTIVITY_FIELD_OPTION,
    from_file: Path | None = FROM_FILE_OPTION,
    strict: bool = STRICT_OPTION,
    verbose: bool = VERBOSE_OPTION,
    config: Path | None = CONFIG_OPTION,
    report_format: ReportFormat | None = FORMAT_OPTION,
    output: Path | None = OUTPUT_OPTION,
    save: bool = SAVE_OPTION,
):
    """Run every audit at once against a single fetch."""
    console.quiet = streams_to_stdout(report_format, output, save)
    settings = resolve_config(config)
    threshold = pick(threshold, settings.threshold)
    min_completeness = pick(min_completeness, settings.min_completeness)
    inactive_days = pick(inactive_days, settings.inactive_days)
    fields = list(required) if required else settings.required_fields
    activity_fields = list(activity_field) if activity_field else settings.activity_fields
    # multi=True: three audits mean three csv files, and no csv on stdout at all.
    target = resolve_report(report_format, output, settings.default_format, multi=True,
                            save=save, label='audit',
                            parts=('duplicates', 'incomplete', 'stale'))

    # One fetch, one union of every property the three audits read — running the
    # commands separately would page through the whole portal three times.
    properties = unique(IDENTITY_PROPERTIES, fields, activity_fields)
    contacts = resolve_contacts(from_file, properties)

    console.rule("[bold cyan]Duplicates[/bold cyan]")
    clusters = duplicates_with_progress(contacts, threshold)
    excluded = excluded_from_matching(contacts)
    if clusters:
        render_duplicates(clusters, threshold)
    else:
        console.print(f"  [green]No duplicates[/green]  [dim](threshold {threshold})[/dim]")
    if verbose and excluded:
        render_excluded(excluded)

    console.rule("[bold cyan]Incomplete[/bold cyan]")
    incomplete = find_incomplete(contacts, fields, min_completeness)
    if incomplete:
        render_incomplete(incomplete, min_completeness)
    elif not fields:
        console.print("  [dim]No required fields configured - nothing to check.[/dim]")
    else:
        console.print(
            f"  [green]All contacts complete[/green]  [dim](min {min_completeness}%)[/dim]"
        )

    console.rule("[bold cyan]Stale[/bold cyan]")
    stale = find_stale(contacts, inactive_days=inactive_days, activity_fields=activity_fields)
    unreadable = unparseable_dates(contacts, activity_fields)
    if stale:
        render_stale(stale, inactive_days)
    else:
        console.print(
            f"  [green]All contacts active[/green]  [dim](within {inactive_days} days)[/dim]"
        )
    if verbose and unreadable:
        render_unparseable(unreadable)

    console.rule("[bold]Summary[/bold]")
    found = len(clusters) + len(incomplete) + len(stale)
    if not found:
        console.print(clean_panel(
            f"[bold green]No issues found[/bold green] in {len(contacts)} contacts.",
            "3 audits",
        ))
    else:
        never = never_seen(stale)
        never_note = f"  [dim]|[/dim]  [dim]{never} never seen[/dim]" if never else ""
        console.print(summary_panel(
            f"[bold yellow]{len(clusters)}[/bold yellow] duplicate cluster(s)  [dim]|[/dim]  "
            f"[bold yellow]{len(incomplete)}[/bold yellow] incomplete  [dim]|[/dim]  "
            f"[bold yellow]{len(stale)}[/bold yellow] stale  [dim]|[/dim]  "
            f"[dim]{len(contacts)} scanned[/dim]{never_note}"
        ))
    report_skipped(
        verbose,
        (len(excluded), "not compared"),
        (len(unreadable), "unreadable date(s)"),
    )

    # Insertion order is the section order in the JSON file and the order the csv
    # files are named, so it matches the order the audits ran above.
    export(target, len(contacts), {
        "duplicates": duplicates_section(clusters, threshold),
        "incomplete": incomplete_section(incomplete, min_completeness, fields),
        "stale": stale_section(stale, inactive_days, activity_fields),
    })
    if found and strict:
        raise typer.Exit(code=1)


# `merge` was this command's name before the `fix` group existed. Kept as a
# hidden alias so anything already scripted against it keeps working.
@app.command("merge", hidden=True)
@fix_app.command("duplicates")
def fix_duplicates(
    threshold: int | None = THRESHOLD_OPTION,
    limit: int | None = LIMIT_OPTION,
    interactive: bool = INTERACTIVE_OPTION,
    apply: bool = APPLY_OPTION,
    yes: bool = YES_OPTION,
    from_file: Path | None = FROM_FILE_OPTION,
    config: Path | None = CONFIG_OPTION,
    report_format: ReportFormat | None = FORMAT_OPTION,
    output: Path | None = OUTPUT_OPTION,
    save: bool = SAVE_OPTION,
):
    """Merge duplicate contacts. Previews by default; --apply writes to HubSpot.

    Runs the duplicate audit fresh rather than reading a saved report, because
    merging against a stale list is how you merge the wrong records.
    """
    console.quiet = streams_to_stdout(report_format, output, save)
    settings = resolve_config(config)
    threshold = pick(threshold, settings.threshold)
    target = resolve_report(report_format, output, settings.default_format,
                            save=save, label="merge")

    if interactive and yes:
        raise fail(
            "[bold red]--interactive and --yes contradict each other:[/bold red] one "
            "asks about every cluster, the other answers everything up front."
        )
    if interactive and console.quiet:
        # typer.prompt writes to stdout, which is carrying the report
        raise fail(
            "[bold red]--interactive can't stream to stdout:[/bold red] the prompts "
            "and the report would share the pipe. Use --output FILE or --save."
        )
    if apply and from_file:
        # the ids in a dump were true when it was written; the portal has moved on
        raise fail(
            "[bold red]Refusing to apply from a file:[/bold red] --from-file is a "
            "snapshot, and merging on stale ids can merge the wrong records. "
            "Drop --from-file to re-run the audit live."
        )

    contacts = resolve_contacts(from_file, MERGE_PROPERTIES)
    clusters = duplicates_with_progress(contacts, threshold)
    plans = plan_merges(clusters, settings.required_fields, limit=limit)

    if not plans:
        console.print(clean_panel(
            f"[bold green]Nothing to merge[/bold green] in {len(contacts)} contacts.",
            f"threshold {threshold}",
        ))
        return

    if interactive:
        plans = choose_interactively(plans, settings.required_fields)
        if not plans:
            console.print("[dim]No clusters selected. Nothing to do.[/dim]")
            return

    total = merge_count(plans)
    dropped = len(clusters) - len(plans)
    if dropped and not interactive:
        console.print(
            f"  [dim]--limit {limit}: acting on {len(plans)} of {len(clusters)} "
            f"cluster(s), highest confidence first.[/dim]"
        )

    if not apply:
        render_merge_plan([_preview(plan) for plan in plans], applied=False)
        console.print(summary_panel(
            f"[bold yellow]{total}[/bold yellow] contact(s) would be merged into "
            f"[bold yellow]{len(plans)}[/bold yellow] survivor(s)  [dim]|[/dim]  "
            "[bold]nothing was written[/bold]  [dim]|[/dim]  "
            "[dim]re-run with --apply to commit[/dim]"
        ))
        export(target, len(contacts), {
            "merges": merges_section([_preview(p) for p in plans], threshold, applied=False),
        })
        return

    if not yes and not confirm_merge(total, len(plans)):
        console.print("[dim]Cancelled. Nothing was written.[/dim]")
        raise typer.Exit(code=1)

    outcomes = merge_with_progress(plans, total)
    render_merge_plan(outcomes, applied=True)

    failures = failure_count(outcomes)
    merged = sum(len(outcome.merged) for outcome in outcomes)
    console.print(summary_panel(
        f"[bold green]{merged}[/bold green] merged  [dim]|[/dim]  "
        + (f"[bold red]{failures}[/bold red] failed  [dim]|[/dim]  " if failures else "")
        + f"[dim]{len(plans)} survivor(s)[/dim]"
    ))
    export(target, len(contacts), {
        "merges": merges_section(outcomes, threshold, applied=True),
    })
    if failures:
        raise typer.Exit(code=1)


@fix_app.command("stale")
def fix_stale(
    inactive_days: int | None = INACTIVE_DAYS_OPTION,
    activity_field: list[str] | None = ACTIVITY_FIELD_OPTION,
    limit: int | None = LIMIT_OPTION,
    archive: bool = ARCHIVE_OPTION,
    yes: bool = YES_OPTION,
    from_file: Path | None = FROM_FILE_OPTION,
    config: Path | None = CONFIG_OPTION,
    report_format: ReportFormat | None = FORMAT_OPTION,
    output: Path | None = OUTPUT_OPTION,
    save: bool = SAVE_OPTION,
):
    """Archive stale contacts. Previews by default; --archive writes to HubSpot.

    Unlike a merge, an archive is recoverable: the records land in HubSpot's
    recycle bin and can be restored from the UI for 90 days.
    """
    console.quiet = streams_to_stdout(report_format, output, save)
    settings = resolve_config(config)
    inactive_days = pick(inactive_days, settings.inactive_days)
    fields = list(activity_field) if activity_field else settings.activity_fields
    target = resolve_report(report_format, output, settings.default_format,
                            save=save, label="archive")

    contacts = resolve_contacts(from_file, unique(IDENTITY_PROPERTIES, fields))
    flagged = find_stale(contacts, inactive_days=inactive_days, activity_fields=fields)
    plans = plan_archives(flagged, limit=limit)

    if not plans:
        console.print(clean_panel(
            f"[bold green]Nothing to archive[/bold green] in {len(contacts)} contacts.",
            f"within {inactive_days} days",
        ))
        return

    if len(plans) < len(flagged):
        console.print(
            f"  [dim]--limit {limit}: acting on {len(plans)} of {len(flagged)} "
            f"stale contact(s), longest-silent first.[/dim]"
        )

    if not archive:
        render_archive_plan([_archive_preview(plan) for plan in plans], applied=False)
        console.print(summary_panel(
            f"[bold yellow]{len(plans)}[/bold yellow] contact(s) would be archived  "
            "[dim]|[/dim]  [bold]nothing was written[/bold]  [dim]|[/dim]  "
            "[dim]re-run with --archive to commit[/dim]"
        ))
        export(target, len(contacts), {
            "archives": archives_section(
                [_archive_preview(p) for p in plans], inactive_days, applied=False
            ),
        })
        return

    if not yes and not confirm_archive(len(plans)):
        console.print("[dim]Cancelled. Nothing was written.[/dim]")
        raise typer.Exit(code=1)

    outcomes = archive_with_progress(plans)
    render_archive_plan(outcomes, applied=True)

    failures = archive_failures(outcomes)
    archived = sum(1 for outcome in outcomes if outcome.archived)
    console.print(summary_panel(
        f"[bold green]{archived}[/bold green] archived  [dim]|[/dim]  "
        + (f"[bold red]{failures}[/bold red] failed  [dim]|[/dim]  " if failures else "")
        + f"[dim]{len(contacts)} scanned[/dim]"
    ))
    export(target, len(contacts), {
        "archives": archives_section(outcomes, inactive_days, applied=True),
    })
    if failures:
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()

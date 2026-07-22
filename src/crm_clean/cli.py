"""
Typer CLI entrypoint. All commands are registered on `app`.

The Typer wiring below is boilerplate — safe to use as-is. The body of
`fetch()` is Week 1 core logic: call into client.py yourself and print
something useful (contact count, maybe a Rich table/progress bar).
"""

import typer
from rich.console import Console
from crm_clean.client import fetch_all_contacts

app = typer.Typer(help="crm-clean: audit your HubSpot CRM for data hygiene issues.")
console = Console()

audit_app = typer.Typer(help="Run data hygiene audits.")
app.add_typer(audit_app, name="audit")


@app.command()
def fetch():
    """Fetch contacts from HubSpot and print how many were found."""
    try:
        with console.status("[bold green]Fetching contacts from HubSpot...[/bold green]"):
            contacts = fetch_all_contacts()
    except Exception as err:
        console.print(f"[bold red]Failed to fetch contacts:[/bold red] {err}")
        raise typer.Exit(code=1)
    console.print(f"[bold green]Found {len(contacts)} contacts: {contacts}[/bold green]")

@audit_app.command("duplicates")
def audit_duplicates():
    """Detect probable duplicate contacts (Week 2)."""
    raise NotImplementedError


@audit_app.command("incomplete")
def audit_incomplete():
    """Flag contacts missing required fields (Week 3)."""
    raise NotImplementedError


@audit_app.command("stale")
def audit_stale():
    """Flag contacts with no recent activity (Week 3)."""
    raise NotImplementedError


@audit_app.command("all")
def audit_all():
    """Run every audit at once (Week 4)."""
    raise NotImplementedError


if __name__ == "__main__":
    app()

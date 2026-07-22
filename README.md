# crm-clean

A Python CLI that connects to HubSpot, audits your CRM contacts, and outputs
actionable data hygiene reports — duplicates, incomplete records, and stale
contacts — so your sales and marketing data stays clean and trustworthy.

## Status

✅ **Week 1 complete** — foundation is in place: HubSpot client with pagination
and a working `crm-clean fetch` command.

🚧 Audits (duplicates, incomplete, stale) are scaffolded and land in Weeks 2–4.
See the [Roadmap](#roadmap) below.

## Features

- **`fetch`** — connects to HubSpot, pages through *every* contact (not just the
  first 100), and reports what it found.
- Built on [Typer](https://typer.tiangolo.com/) for the CLI and
  [Rich](https://rich.readthedocs.io/) for readable terminal output.
- Reads credentials from a local `.env` file — no secrets in code.

## Requirements

- Python **3.11+**
- A HubSpot **private app access token** with CRM read scopes
  (`crm.objects.contacts.read`).

## Setup

```bash
# clone and enter the repo
git clone https://github.com/brandopakel/hubspot-clean.git
cd hubspot-clean

# create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate          # Windows (PowerShell): .venv\Scripts\Activate.ps1

# install in editable mode with dev dependencies
pip install -e ".[dev]"
```

## Configuration

The client loads your token from a `.env` file in the project root.

```bash
cp .env.example .env
```

Then edit `.env` and set your token:

```
HUBSPOT_ACCESS_TOKEN=your-private-app-token-here
```

To create a token: HubSpot → **Settings → Integrations → Private Apps →
Create a private app**, grant the `crm.objects.contacts.read` scope, and copy
the generated access token.

> **Note:** `.env` is gitignored — never commit your real token.

## Usage

Run commands from the project root with the virtual environment active.

```bash
crm-clean fetch                 # ✅ fetch all contacts and report the count
```

Planned (Weeks 2–4):

```bash
crm-clean audit duplicates      # 🚧 detect probable duplicate contacts
crm-clean audit incomplete      # 🚧 flag contacts missing required fields
crm-clean audit stale           # 🚧 flag contacts with no recent activity
crm-clean audit all             # 🚧 run every audit at once
```

You can also run the CLI without installing the entry point:

```bash
python -m crm_clean.cli fetch
```

## Development

```bash
pytest          # run tests
ruff check .    # lint
```

Quick sanity check of the client in a Python REPL (from the project root, venv active):

```python
from crm_clean.client import fetch_all_contacts
contacts = fetch_all_contacts()
len(contacts)      # how many contacts came back
contacts[0]        # inspect one record's shape
```

## Project structure

```
src/crm_clean/
├── cli.py            # Typer entrypoint — registers `fetch` and `audit` commands
├── client.py         # HubSpot client: auth, pagination, normalization
├── config.py         # configuration handling
├── reports.py        # report formatting/output
└── audits/
    ├── duplicates.py # Week 2
    ├── incomplete.py # Week 3
    └── stale.py      # Week 3
```

See `pyproject.toml` for the full dependency list.

## Roadmap

| Week | Focus | Status |
| ---- | ----- | ------ |
| 1 | HubSpot client + `fetch` command | ✅ Done |
| 2 | Duplicate detection | 🚧 Planned |
| 3 | Incomplete & stale record audits | 🚧 Planned |
| 4 | Combined `audit all` + report export | 🚧 Planned |

## License

MIT

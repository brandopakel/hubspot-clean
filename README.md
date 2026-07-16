# crm-clean

A Python CLI that connects to HubSpot, audits CRM records, and outputs actionable data hygiene reports.

## Status

🚧 Work in progress — Week 1 (foundation).

## Setup

```bash
# clone and enter the repo
git clone <your-repo-url>
cd crm-clean

# create and activate a venv (or let PyCharm do this)
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# install in editable mode with dev dependencies
pip install -e ".[dev]"

# set up your API key
cp .env.example .env
# then edit .env with your HubSpot private app token
```

## Usage

```bash
crm-clean fetch
crm-clean audit duplicates
crm-clean audit incomplete
crm-clean audit stale
crm-clean audit all --format csv --output report.csv
```

## Development

```bash
pytest          # run tests
ruff check .    # lint
```

## Project structure

See `pyproject.toml` for dependencies. Core logic lives in `src/crm_clean/`,
audits are split into `src/crm_clean/audits/`.

<!-- TODO: expand with real usage examples, screenshots/GIFs, and setup notes
     as the project comes together. -->

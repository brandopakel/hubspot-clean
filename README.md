# hubspot-crm-clean

A Python CLI that connects to HubSpot, audits your CRM contacts, and outputs
actionable data hygiene reports — duplicates, incomplete records, and stale
contacts — so your sales and marketing data stays clean and trustworthy.

## Status

✅ **Week 1 complete** — foundation is in place: HubSpot client with pagination
and a working `hubspot-crm-clean fetch` command.

✅ **Week 2 complete** — fuzzy duplicate detection with email normalization,
transitive clustering, and a `hubspot-crm-clean audit duplicates` command.

✅ **Week 3 complete** — completeness scoring and staleness detection, with
`hubspot-crm-clean audit incomplete` and `hubspot-crm-clean audit stale`.

🚧 Combined `audit all` and report export land in Week 4. See the
[Roadmap](#roadmap) below.

## Features

- **`fetch`** — connects to HubSpot, pages through *every* contact (not just the
  first 100), and reports what it found.
- **`audit duplicates`** — normalizes messy email addresses, buckets contacts by
  domain, fuzzy-matches names, and merges the matches into clusters.
- **`audit incomplete`** — scores each contact on how many required fields it
  actually fills in, and flags the ones below a threshold.
- **`audit stale`** — flags contacts with no activity in a configurable window,
  separating "silent for N days" from "never heard from at all".
- Built on [Typer](https://typer.tiangolo.com/) for the CLI and
  [Rich](https://rich.readthedocs.io/) for readable terminal output, with live
  progress bars on both the fetch and the comparison phase.
- Reads credentials from a local `.env` file — no secrets in code.

## Requirements

- Python **3.11+**
- A HubSpot **private app access token** with CRM read scopes
  (`crm.objects.contacts.read`).

## Setup

```bash
# clone and enter the repo
git clone https://github.com/brandopakel/hubspot-clean.git hubspot-crm-clean
cd hubspot-crm-clean

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
hubspot-crm-clean fetch                 # ✅ fetch all contacts and report the count
hubspot-crm-clean fetch --all-properties  # ✅ ...including every custom property
hubspot-crm-clean audit duplicates      # ✅ detect probable duplicate contacts
hubspot-crm-clean audit incomplete      # ✅ flag contacts missing required fields
hubspot-crm-clean audit stale           # ✅ flag contacts with no recent activity
```

By default `fetch` requests a useful core set of properties. Pass
`--all-properties` / `-a` to enumerate every property defined on the contact
object (custom fields included) and request them all — handy for discovering what
your portal actually stores, but noticeably slower and much larger. The HubSpot
objects API has no "all properties" wildcard, so this works by listing the
property definitions first and then passing the full set explicitly.

Planned (Week 4):

```bash
hubspot-crm-clean audit all             # 🚧 run every audit at once
```

Every audit shares the same three options: `--from-file` to run offline against
a JSON dump, `--strict` to exit 1 when anything is found (for CI), and its own
tuning flag.

### `audit duplicates`

```bash
hubspot-crm-clean audit duplicates                        # scan your live HubSpot contacts
hubspot-crm-clean audit duplicates -t 92                  # stricter name matching
hubspot-crm-clean audit duplicates --from-file dump.json  # run offline, no API calls
hubspot-crm-clean audit duplicates --strict               # exit 1 if anything is found (CI)
```

| Option | Default | What it does |
| ------ | ------- | ------------ |
| `--threshold`, `-t` | `85` | Name similarity score (0–100) required to call two contacts a match. |
| `--from-file` | — | Read contacts from a JSON file instead of calling HubSpot. Accepts a `{"results": [...]}` wrapper or a bare list. |
| `--strict` | off | Exit with code 1 when duplicates are found. |

Output groups each set of probable duplicates into a numbered cluster, with a
confidence score colour-coded by how much to trust it:

```
Probable duplicates  (threshold 85)

  #   Conf   ID   Name         Email
  ─────────────────────────────────────────────────────────
  1    100   1    Jane Doe     jane.doe@gmail.com
              2    Doe Jane     janedoe@googlemail.com
              3    Jane Doe     Jane Doe <j.doe+crm@GMIAL.COM>
  ─────────────────────────────────────────────────────────
  2     95   4    Bob Smith    bob@acme.com
              5    Bobb Smith   bsmith@acme.com

  2 cluster(s) | 5 contacts involved | 8 scanned
```

### `audit incomplete`

```bash
hubspot-crm-clean audit incomplete                     # default: below 75% of 4 fields
hubspot-crm-clean audit incomplete -m 100              # anything less than perfect
hubspot-crm-clean audit incomplete -r email -r phone   # only care about these two
```

| Option | Default | What it does |
| ------ | ------- | ------------ |
| `--min-completeness`, `-m` | `75` | Flag contacts scoring below this percentage. A contact landing exactly on the bar passes. |
| `--required`, `-r` | `email`, `company`, `lifecyclestage`, `phone` | Required field. Repeat the flag for each one. |
| `--from-file` | — | Run offline against a JSON dump. |
| `--strict` | off | Exit 1 when anything is flagged. |

```
Incomplete contacts  (below 75%)

  Score   ID   Name        Email              Missing
  ──────────────────────────────────────────────────────────────────────────
     0%   3    Zoe Quinn   -                  email, company, lifecyclestage, phone
    25%   2    Bob Smith   bob@widgetco.com   company, lifecyclestage, phone

  2 incomplete | 4 scanned | required: email, company, lifecyclestage, phone
```

### `audit stale`

```bash
hubspot-crm-clean audit stale                # default: no activity in 90+ days
hubspot-crm-clean audit stale -d 365         # only records silent for a year
```

| Option | Default | What it does |
| ------ | ------- | ------------ |
| `--inactive-days`, `-d` | `90` | Flag contacts with no activity in at least this many days. |
| `--from-file` | — | Run offline against a JSON dump. |
| `--strict` | off | Exit 1 when anything is flagged. |

```
Stale contacts  (no activity in 90+ days)

   Days   ID   Name        Email              Last activity
  ─────────────────────────────────────────────────────────
  never   3    Zoe Quinn   -                  -
    570   2    Bob Smith   bob@widgetco.com   2025-01-01

  2 stale | 4 scanned | 1 never seen
```

## How duplicate detection works

**1. Email normalization.** Before anything is compared, each address is reduced
to a canonical form — case, surrounding whitespace, `mailto:` prefixes, `<...>`
wrappers and display names, non-breaking spaces from copy-paste, and stray
leading/trailing dots are all stripped. `+tag` suffixes are dropped, dots in the
local part are ignored *for Gmail only* (where they're insignificant), and a
small map folds known typo domains (`gmial.com`, `googlemail.com`) onto their
real counterparts. Anything that can't be parsed into a confident
`local@domain.tld` is skipped rather than guessed at — a fabricated address
would silently merge two different people.

**2. Domain bucketing.** Contacts are grouped by normalized domain, so names are
only compared against plausible colleagues rather than every other contact.

**3. Fuzzy name matching.** Within each bucket, every pair of names is scored
with `rapidfuzz`'s `token_sort_ratio`, which is order-insensitive — so a
first/last name swap still matches. Contacts missing *both* name fields are
excluded; two blank names score 100 against each other and would otherwise be
reported as certain duplicates.

**4. Clustering.** Matched pairs are merged with union-find, so three records of
the same person become one cluster of three rather than three separate pairs.
Each cluster reports the **weakest** pairwise score holding it together, which
is the honest confidence for a chain. Note that clustering is transitive: if
A matches B and B matches C, all three group together even if A and C wouldn't
match directly.

## How the other audits work

**Completeness** is stricter than "the key exists". HubSpot returns an unset
property as `null`, but a field a human cleared comes back as an empty string,
and pasted data often leaves whitespace behind — all three count as missing.
Genuine falsy values (`0`, `False`) do not. The score is simply the percentage
of required fields filled, and the threshold is *exclusive*: a contact landing
exactly on the bar passes.

**Staleness** takes the *most recent* timestamp across all activity fields, so a
contact with an ancient `hs_last_activity_date` but a recent `lastmodifieddate`
counts as active. Unparseable dates are treated as absent rather than raising —
one malformed record shouldn't abort the audit. Contacts with no usable date at
all are still flagged, but reported as `never` with `days_inactive=None`, so
"silent for 400 days" stays distinguishable from "we have never heard from this
record". `find_stale` takes an injectable `now`, which is what keeps its tests
independent of the wall clock.

You can also run the CLI without installing the entry point:

```bash
python -m hubspot_crm_clean.cli fetch
```

## Development

```bash
pytest          # run tests
ruff check .    # lint
```

All three audits have full test suites — 114 tests covering email normalization,
domain bucketing, clustering, completeness scoring, timestamp parsing, staleness
windows, and every CLI command end to end. The CLI tests drive the real commands
through `--from-file`, so the whole suite runs offline with no HubSpot
credentials and no network access.

Quick sanity check of the client in a Python REPL (from the project root, venv active):

```python
from hubspot_crm_clean.client import fetch_all_contacts
contacts = fetch_all_contacts()
len(contacts)      # how many contacts came back
contacts[0]        # inspect one record's shape
```

## Project structure

```
src/hubspot_crm_clean/
├── cli.py            # Typer entrypoint — registers `fetch` and `audit` commands
├── client.py         # HubSpot client: auth, pagination, normalization
├── config.py         # configuration handling
├── reports.py        # report formatting/output
└── audits/
    ├── duplicates.py # ✅ fuzzy matching + clustering
    ├── incomplete.py # ✅ completeness scoring
    └── stale.py      # ✅ staleness detection
```

Audit modules never import a UI library. They return plain `NamedTuple` results
and accept optional callbacks (`on_progress`, `on_page`) so the CLI can drive
progress bars — which is what keeps them testable without a console.

See `pyproject.toml` for the full dependency list.

## Roadmap

| Week | Focus | Status |
| ---- | ----- | ------ |
| 1 | HubSpot client + `fetch` command | ✅ Done |
| 2 | Duplicate detection | ✅ Done |
| 3 | Incomplete & stale record audits | ✅ Done |
| 4 | Combined `audit all` + report export | 🚧 Planned |

## License

MIT

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

✅ **Week 4 complete** — a combined `hubspot-crm-clean audit all` that runs every
audit against a single fetch, a YAML config file so thresholds live somewhere
other than your shell history, and CSV/JSON report export via `--format` /
`--output`.

## Features

- **`fetch`** — connects to HubSpot, pages through *every* contact (not just the
  first 100), and reports what it found.
- **`audit duplicates`** — normalizes messy email addresses, buckets contacts by
  domain, fuzzy-matches names, and merges the matches into clusters.
- **`audit incomplete`** — scores each contact on how many required fields it
  actually fills in, and flags the ones below a threshold.
- **`audit stale`** — flags contacts with no activity in a configurable window,
  separating "silent for N days" from "never heard from at all".
- **`audit all`** — runs all three against a *single* fetch, section by section,
  then one combined summary. Running the commands separately pages through the
  whole portal three times.
- **`config.yaml`** — set your thresholds and required fields once instead of
  retyping flags. Command-line flags still win, and an unrecognized key is a
  loud error rather than a silent no-op.
- **`--format` / `--output`** — export findings as CSV or JSON, to a file or
  straight down a pipe. Every audit carries the settings that produced it, so a
  report stays readable long after the command that made it scrolled away.
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

### Audit defaults (`config.yaml`)

Thresholds and field lists can live in a YAML file instead of your shell history:

```bash
cp config.example.yaml config.yaml
```

`config.yaml` is picked up automatically from the directory you run in; pass
`--config path/to/other.yaml` to use a different one. Whichever file applies is
echoed at the top of the run, so a config file can never change your results
invisibly. Every key is optional — omit one and the built-in default applies.

```yaml
rules:
  duplicates:
    match_threshold: 85
  incomplete:
    required_fields: [email, company, lifecyclestage, phone]
    min_completeness: 75
  stale:
    inactive_days: 90
    activity_fields: [hs_last_activity_date, lastmodifieddate]

reports:
  default_format: json      # only consulted when --output can't imply one
```

Precedence is **command-line flag → config file → built-in default**, so a flag
always wins over the file for that one run.

Validation is deliberately strict: an unknown key is an error, not a silent
no-op. A config that quietly ignored `min_completness: 90` would be worse than
no config at all, because it reads as though it took effect.

```
$ hubspot-crm-clean audit incomplete --config bad.yaml
Bad config: bad.yaml: unknown key(s) under rules.incomplete: min_completness.
Valid keys: min_completeness, required_fields
```

Wrong types, out-of-range numbers, and a field list that repeats a name are
rejected the same way — the last of those because completeness scores divide by
the length of `required_fields`, so a duplicate would silently skew every score.
Errors name the dotted path to the offending key.

`reports.default_format` is the one key with no built-in default, so it ships
commented out in the example rather than restated: leaving it unset is a real
setting, and it means `--format` is required whenever `--output` has no `.csv`
or `.json` extension to infer from. Note that it never decides *whether* a
report is written — only `--format` / `--output` do that, so a configured
directory doesn't quietly start dropping files on every run.

`config.example.yaml` also lists, in comments, the settings that are *not*
configurable yet (`hubspot.object_types`, `rules.duplicates.match_fields`,
`reports.columns`) — they're commented out precisely because the loader refuses
keys it can't honour.

## Usage

Run commands from the project root with the virtual environment active.

```bash
hubspot-crm-clean fetch                 # ✅ fetch all contacts and report the count
hubspot-crm-clean fetch --all-properties  # ✅ ...including every custom property
hubspot-crm-clean audit duplicates      # ✅ detect probable duplicate contacts
hubspot-crm-clean audit incomplete      # ✅ flag contacts missing required fields
hubspot-crm-clean audit stale           # ✅ flag contacts with no recent activity
hubspot-crm-clean audit all             # ✅ run every audit against one fetch
```

By default `fetch` requests a useful core set of properties. Pass
`--all-properties` / `-a` to enumerate every property defined on the contact
object (custom fields included) and request them all — handy for discovering what
your portal actually stores, but noticeably slower and much larger. The HubSpot
objects API has no "all properties" wildcard, so this works by listing the
property definitions first and then passing the full set explicitly.

Every audit shares the same options: `--from-file` to run offline against a JSON
dump, `--strict` to exit 1 when anything is found (for CI), `--config` to point
at a YAML file of defaults, `--format` / `--output` to export the findings, and
its own tuning flags.

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
| `--config`, `-c` | `./config.yaml` | YAML file supplying defaults. Flags still win. |
| `--format`, `-f` | inferred | Export as `csv` or `json`. Alone, streams to stdout. |
| `--output`, `-o` | — | Write the report here; `-` streams to stdout. |

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
| `--config`, `-c` | `./config.yaml` | YAML file supplying defaults. Flags still win. |
| `--format`, `-f` | inferred | Export as `csv` or `json`. Alone, streams to stdout. |
| `--output`, `-o` | — | Write the report here; `-` streams to stdout. |

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
| `--activity-field` | `hs_last_activity_date`, `lastmodifieddate` | Timestamp field counted as activity. Repeat the flag for each one. |
| `--from-file` | — | Run offline against a JSON dump. |
| `--strict` | off | Exit 1 when anything is flagged. |
| `--config`, `-c` | `./config.yaml` | YAML file supplying defaults. Flags still win. |
| `--format`, `-f` | inferred | Export as `csv` or `json`. Alone, streams to stdout. |
| `--output`, `-o` | — | Write the report here; `-` streams to stdout. |

```
Stale contacts  (no activity in 90+ days)

   Days   ID   Name        Email              Last activity
  ─────────────────────────────────────────────────────────
  never   3    Zoe Quinn   -                  -
    570   2    Bob Smith   bob@widgetco.com   2025-01-01

  2 stale | 4 scanned | 1 never seen
```

### `audit all`

```bash
hubspot-crm-clean audit all                     # all three audits, one fetch
hubspot-crm-clean audit all --strict            # exit 1 if any audit finds anything
hubspot-crm-clean audit all -t 92 -m 100 -d 365 # each audit still tunable
```

Accepts every tuning flag the individual audits do (`--threshold`,
`--min-completeness`, `--required`, `--inactive-days`, `--activity-field`) plus
the shared `--from-file`, `--strict`, `--config`, `--format`, and `--output`.

The point of the command is the *single* fetch: it requests the union of every
property the three audits read and runs all of them over that one result set.
Running the three commands separately pages through your entire portal three
times. `--strict` exits 1 if *any* audit reports a finding.

```
──────────────────── Duplicates ────────────────────
Probable duplicates  (threshold 85)

  #   Conf   ID   Name       Email
  1    100   1    Jane Doe   jane.doe@acme.com
              2    Jane Doe   j.doe@acme.com

──────────────────── Incomplete ────────────────────
Incomplete contacts  (below 75%)

  Score   ID   Name        Email              Missing
    50%   3    Bob Smith   bob@widgetco.com   company, lifecyclestage

─────────────────────── Stale ──────────────────────
Stale contacts  (no activity in 90+ days)

   Days   ID   Name       Email            Last activity
    572   2    Jane Doe   j.doe@acme.com   2025-01-01

────────────────────── Summary ─────────────────────
  1 duplicate cluster(s) | 1 incomplete | 1 stale | 3 scanned
```

An audit that finds nothing collapses to a single line rather than a full table,
so a clean section stays out of the way. When `required_fields` is empty the
incomplete section says so explicitly instead of claiming every contact is
complete — nothing was checked, which is not the same as everything passing.

## Report export

Any audit can write its findings to a file or a pipe. The tables still print —
export is in addition to the terminal output, not instead of it.

```bash
hubspot-crm-clean audit all -o findings.json     # one JSON file, all three audits
hubspot-crm-clean audit all -o findings.csv      # three CSVs, one per audit
hubspot-crm-clean audit stale -o stale.csv       # one audit, one file
hubspot-crm-clean audit all -f json -o - | jq '.stale.findings'
```

**Picking the format.** Precedence is `--format` → the extension on `--output` →
`reports.default_format` in your config. The extension outranks the config
because it's part of *this* invocation. If none of the three settles it, the
command stops and says so rather than guessing:

```
$ hubspot-crm-clean audit stale --output findings.txt
Bad report options: could not tell the format from 'findings.txt': name it .csv
or .json, or pass --format csv|json
```

Bad flags are caught *before* the fetch, so a typo costs you a second rather
than a full pass over your portal.

**One file or several.** JSON is one document, so `audit all` gets one file with
a section per audit. CSV is flat and the three audits have three different column
layouts, so `--output findings.csv` writes `findings-duplicates.csv`,
`findings-incomplete.csv`, and `findings-stale.csv`. A single audit always keeps
exactly the path you gave it. For the same reason `--format csv --output -` is
rejected for `audit all` — three headers in one stream isn't a CSV file.

**Streaming.** `--output -` sends the report to stdout and silences everything
else, tables and progress bars included, so the output is safe to pipe. Passing
`--format` without `--output` does the same thing. Errors still print, because a
run that died has no data stream left to corrupt.

**What's in the file.** Each audit's findings travel with the settings that
produced them — a bare list of findings isn't reproducible, since "2 duplicates"
means nothing without the threshold that decided it.

```json
{
  "scanned": 5,
  "stale": {
    "inactive_days": 90,
    "activity_fields": ["hs_last_activity_date", "lastmodifieddate"],
    "findings": [
      {"id": "5", "name": null, "email": "ghost@widgetco.com",
       "days_inactive": null, "last_seen": null},
      {"id": "4", "name": "Zoe Quinn", "email": "zoe@widgetco.com",
       "days_inactive": 570, "last_seen": "2025-01-04T00:22:07+00:00"}
    ]
  }
}
```

The export is the machine-readable twin of the table: same findings, same
numbers, with the two things the table truncated purely for column width put
back. A cluster's confidence repeats on every member row, because a CSV row has
to stand alone once someone sorts the file, and `last_seen` keeps its time as
well as its date. `days_inactive: null` is how "never heard from" is reported —
the table's word `never` isn't something a spreadsheet formula should have to
know about, and `null` and `0` are different answers.

A run that finds nothing still writes its report, headers and all. A missing
file and a file that says "nothing wrong" mean different things, and only one of
them survives a scheduled run. `--strict` still exits 1, but the report is
written first — CI is exactly where the artifact matters most.

```
$ hubspot-crm-clean audit stale --from-file clean.json -o stale.csv
$ cat stale.csv
id,name,email,days_inactive,last_seen
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

Everything has a test suite — 249 tests covering email normalization, domain
bucketing, clustering, completeness scoring, timestamp parsing, staleness
windows, config parsing and rejection, flag-over-config precedence, report row
shapes, format resolution, and every CLI command end to end. The CLI tests drive
the real commands through `--from-file`, so the whole suite runs offline with no
HubSpot credentials and no network access.

Two guards worth knowing about, both in `tests/conftest.py`: every test runs in
an empty working directory (otherwise a real `config.yaml` sitting in the repo
would retune the whole suite), and Rich's console width is pinned (otherwise an
80-column fallback splits summary lines and breaks substring assertions for
reasons unrelated to the code).

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
├── config.py         # ✅ YAML config loading + strict validation
├── reports.py        # ✅ CSV/JSON export + destination resolution
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
| 4 | Combined `audit all` + YAML config + report export | ✅ Done |

Week 4 is complete. Still open, and deliberately rejected by the config loader
rather than silently ignored: auditing objects other than contacts
(`hubspot.object_types`), configurable duplicate match fields
(`rules.duplicates.match_fields`), and choosing which columns a report carries
(`reports.columns`).

## License

MIT

# hubspot-crm-clean

A Python CLI that connects to HubSpot, audits your CRM contacts for data
hygiene issues — duplicates, incomplete records, and stale contacts — exports
the findings as CSV or JSON, and cleans up what it finds.

Everything reads by default. The two commands that can modify your CRM preview
their work and write nothing until you explicitly tell them to.

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
- **`--verbose`** — every audit reports how many records it *couldn't* look at,
  always. Pass `-v` to see which ones and why.
- **`fix duplicates`** — folds each duplicate cluster into one surviving record.
  **Dry run by default**: it shows exactly what it would do and writes nothing
  until you pass `--apply`, which then asks for confirmation. `--interactive`
  walks you through each cluster so you choose the survivor yourself.
- **`fix stale`** — archives contacts nobody has touched. Same dry-run-first
  design; recoverable from HubSpot's recycle bin for 90 days.
- **`--save`** — don't want to think about a filename? Get
  `hubspot-audit-20260727-143022.json` in the current directory.
- Built on [Typer](https://typer.tiangolo.com/) for the CLI and
  [Rich](https://rich.readthedocs.io/) for readable terminal output, with live
  progress bars on both the fetch and the comparison phase.
- Reads credentials from a local `.env` file — no secrets in code.

## Requirements

- Python **3.11+**
- A HubSpot **private app access token**. Scopes:
  - `crm.objects.contacts.read` — every audit, and every `fix` dry run
  - `crm.objects.contacts.write` — only `fix duplicates --apply` and
    `fix stale --archive`

Nothing outside those two can modify your CRM, so if you leave the write scope
off, the tool is provably read-only.

## Setup

Once published, the short version is:

```bash
pip install hubspot-crm-clean
```

Until then, or to work on it:

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
the generated access token. Add `crm.objects.contacts.write` only when you're
ready to run a `fix` command for real — and try it against a HubSpot **sandbox**
portal first, because merges cannot be undone.

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
Your `config.yaml` is gitignored (`config.example.yaml` is the tracked one), so
your own thresholds stay yours.

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
hubspot-crm-clean fix duplicates        # ✅ preview duplicate merges (writes nothing)
hubspot-crm-clean fix duplicates -i     # ✅ ...choosing each survivor yourself
hubspot-crm-clean fix duplicates --apply  # ⚠️  actually merge them
hubspot-crm-clean fix stale             # ✅ preview archiving stale contacts
hubspot-crm-clean fix stale --archive   # ⚠️  actually archive them
```

By default `fetch` requests a useful core set of properties. Pass
`--all-properties` / `-a` to enumerate every property defined on the contact
object (custom fields included) and request them all — handy for discovering what
your portal actually stores, but noticeably slower and much larger. The HubSpot
objects API has no "all properties" wildcard, so this works by listing the
property definitions first and then passing the full set explicitly.

Every audit shares the same options: `--from-file` to run offline against a JSON
dump, `--strict` to exit 1 when anything is found (for CI), `--verbose` to list
what the audit couldn't look at, `--config` to point at a YAML file of defaults,
`--format` / `--output` / `--save` to export the findings, and its own tuning
flags.

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
| `--verbose`, `-v` | off | List the contacts excluded from matching, and why. |
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
| `--verbose`, `-v` | off | Confirm every contact was scored (this audit skips nothing). |
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
| `--verbose`, `-v` | off | List contacts whose activity dates are unreadable. |
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
the shared `--from-file`, `--strict`, `--verbose`, `--config`, `--format`, and
`--output`.

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

## What an audit couldn't look at (`--verbose`)

Both `duplicates` and `stale` skip records they can't read. The skips are
correct — but a skipped contact is invisible in the results, and `5 scanned`
reads as *5 compared*. So the **counts always print**, and `--verbose` names the
contacts:

```
  2 not compared, 1 unreadable date(s).  Re-run with --verbose to list them.
```

A warning you only see once you know to ask for it isn't a warning, which is why
the count isn't behind the flag. Only the detail is.

```bash
hubspot-crm-clean audit all -v
```

```
Not compared  (excluded from duplicate matching)

  ID   Name        Email                     Reason
  ─────────────────────────────────────────────────────────────
  3    Bob Smith   bob(at)widgetco.com       no usable email
  4    -           purchasing@widgetco.com   no name

Unreadable dates  (counted as no activity)

  ID   Name   Email                     Field                   Value
  ────────────────────────────────────────────────────────────────────────────
  4    -      purchasing@widgetco.com   hs_last_activity_date   15 January 2025
```

**`duplicates`** excludes two kinds of record, both deliberately. An address that
won't normalize can't be bucketed by domain, so it's never compared to anyone —
and guessing at it would merge two different people. A contact with no name at
all is skipped because two blank names score 100 against each other, which would
manufacture duplicates. Contact 3 above is the first case, contact 4 the second.

**`stale`** treats an unparseable timestamp as absent so that one malformed
record can't abort the audit. That's the right call, but it makes a broken date
indistinguishable from a missing one — both print as `never`. Contact 4 above is
listed as `never` in the stale table while actually storing `15 January 2025`.
"We have never contacted this person" and "we can't read the date we stored"
need completely different fixes, so `-v` shows the raw value.

**`incomplete`** skips nothing — every contact is scored, and a blank field is
the finding rather than a reason to look away. `-v` says so explicitly instead
of printing nothing and leaving you to wonder whether the flag worked.

This is diagnostic output, not findings, so it stays out of the exported report
and out of the pipe when you're streaming to stdout.

## Report export

Any audit can write its findings to a file or a pipe. The tables still print —
export is in addition to the terminal output, not instead of it.

```bash
hubspot-crm-clean audit all --save               # you don't care about the name
hubspot-crm-clean audit all -o findings.json     # one JSON file, all three audits
hubspot-crm-clean audit all -o findings.csv      # three CSVs, one per audit
hubspot-crm-clean audit stale -o stale.csv       # one audit, one file
hubspot-crm-clean audit all -f json -o - | jq '.stale.findings'
```

**`--save` / `-s`** writes to a generated name in the current directory, so you
never have to invent one:

```
$ hubspot-crm-clean audit duplicates --save
Wrote hubspot-duplicates-20260727-143022.json
```

The timestamp is in the *filename*, deliberately, and not in the file's
contents. Two runs of the same audit produce byte-identical files, so you can
diff last week's report against today's — while the names still never collide.
Run twice inside the same second and the second one gains a `-2`.

`--save` defaults to JSON. That's the one place a format is guessed for you, and
it's the one place it should be: `--output findings.txt` errors out because you
named a file and we can't tell what you wanted, whereas `--save` means *you pick
everything*. It still honours `--format` and `reports.default_format` if you'd
rather say.

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

## `fix` — actually cleaning up

Everything above this point only reads. The `fix` commands are the only ones
that can change your CRM, so they're built so the careless thing is the safe
thing.

```bash
hubspot-crm-clean fix duplicates                  # preview. writes nothing.
hubspot-crm-clean fix duplicates --interactive    # choose each survivor yourself
hubspot-crm-clean fix duplicates --limit 1        # preview just the surest cluster
hubspot-crm-clean fix duplicates --apply          # write, after confirming
hubspot-crm-clean fix duplicates --apply --yes    # write, no prompt (CI)

hubspot-crm-clean fix stale                       # preview. writes nothing.
hubspot-crm-clean fix stale --archive             # write, after confirming
```

> `merge` still works as a hidden alias for `fix duplicates`, so anything you
> scripted before the rename keeps running.

### The safety design (both commands)

**Dry run is the default, not a flag.** You opt *in* to writing. A `--dry-run`
you can forget to pass is not a safety feature.

**The preview runs the same code as the real thing.** `apply_plans()` and
`apply_archives()` take the write as a callable; the dry run simply never calls
it. A preview generated by different code than the thing it previews is how a
preview ends up lying — and it's the same seam that lets every test here run
with no API key.

**They ask before writing**, unless you pass `--yes`. The two prompts say
different things on purpose:

```
About to merge 3 contact(s) into 2 survivor(s).  This cannot be undone.
About to archive 5 contact(s).  Restorable from HubSpot's recycle bin for 90 days.
```

Archiving really is recoverable and merging really isn't. A warning that
overstates the risk is a warning you learn to click through — including on the
one that doesn't overstate it.

**`--limit N` exists for your first real run.** Records are already ordered
worst-first, so `--limit 3` acts on the three we're surest about.

**One failure doesn't stop the run.** Aborting halfway leaves the portal in a
state nobody planned, so each failure is recorded against its record, the rest
proceed, and the command exits `1` so CI still notices:

```
  3 merged | 1 failed | 2 survivor(s)
```

**Exported reports record what happened**, not just what was proposed — the
`applied` field distinguishes the two.

### `fix duplicates`

| Option | Default | What it does |
| ------ | ------- | ------------ |
| *(none)* | — | **Dry run.** Prints the full plan and writes nothing. |
| `--interactive`, `-i` | off | Review each cluster and pick the survivor yourself. |
| `--apply` | off | Actually perform the merges. Prompts first. |
| `--yes`, `-y` | off | Skip the prompt. For scripts and CI. |
| `--limit`, `-n` | — | At most this many clusters, highest confidence first. |
| `--threshold`, `-t` | `85` | Same matching bar as `audit duplicates`. |
| `--from-file` | — | Preview against a JSON dump. **Rejected with `--apply`.** |
| `--config`, `-c` | `./config.yaml` | `required_fields` decides "most complete". |
| `--format` / `--output` / `--save` | — | Record the plan or the outcome. |

```
Merges  (DRY RUN - nothing written)

  #   Conf          ID   Name        Email                Status
  ─────────────────────────────────────────────────────────────────────
  1    100   keep   1    Jane Doe    jane.doe@acme.com    most complete
             merge  2    Jane Doe    j.doe@acme.com       would merge
  ─────────────────────────────────────────────────────────────────────
  2    100   keep   9    Bob Smith   b.smith@beta.com     lowest id
             merge  10   Bob Smith   bob@beta.com         would merge

  2 contact(s) would be merged into 2 survivor(s) | nothing was written |
  re-run with --apply to commit
```

**`--apply` with `--from-file` is refused.** A dump's ids were true when it was
written and the portal has moved on; merging on stale ids merges the wrong
records:

```
$ hubspot-crm-clean fix duplicates --from-file dump.json --apply
Refusing to apply from a file: --from-file is a snapshot, and merging on stale
ids can merge the wrong records. Drop --from-file to re-run the audit live.
```

#### Which record survives

HubSpot keeps the **primary's** value wherever two records disagree on a
property, so picking the primary decides what data you lose. Three rules, in
order:

1. **Most complete** — the record filling the most `required_fields` wins, since
   it's the one that loses the least. This is why `rules.incomplete.required_fields`
   in your config quietly matters here too.
2. **Oldest** — on a tie, the record other systems have had longest to reference.
3. **Lowest id** — so the choice is deterministic rather than dependent on
   whatever order the API happened to return.

The winning rule is printed in the Status column, so you can always see *why*
that record was chosen. Ids sort numerically, not as text — otherwise `10` would
beat `9` and the tie-break would look arbitrary.

#### `--interactive`

When you'd rather decide yourself:

```
Cluster 1 of 2   confidence 100   acme.com
  1  Jane Doe  jane.doe@acme.com  100% complete  created 2019-01-01 (suggested)
  2  Jane Doe  j.doe@acme.com      25% complete  created 2017-01-01
  Keep which record? 1-2, s(kip), q(uit) [1]:
```

**Option 1 is always the rule-based suggestion**, so holding Enter through the
whole review reproduces exactly what the non-interactive run would have done.
`s` skips that cluster entirely. `q` stops asking but **keeps every decision
made so far** — abandoning a long review doesn't throw away the work.

The reason column then records `you chose` rather than a rule name, so the
exported report distinguishes a human override from an automatic pick.

Two combinations are rejected: `--interactive --yes` (one asks about every
cluster, the other answers everything up front) and `--interactive` while
streaming a report to stdout (the prompts and the report would share the pipe).

### `fix stale`

| Option | Default | What it does |
| ------ | ------- | ------------ |
| *(none)* | — | **Dry run.** Prints what would be archived. |
| `--archive` / `--apply` | off | Actually archive them. Two spellings, one flag. |
| `--yes`, `-y` | off | Skip the prompt. |
| `--limit`, `-n` | — | At most this many, longest-silent first. |
| `--inactive-days`, `-d` | `90` | Same window as `audit stale`. |
| `--activity-field` | `hs_last_activity_date`, `lastmodifieddate` | Repeatable. |
| `--from-file` | — | Preview against a JSON dump. |
| `--format` / `--output` / `--save` | — | Record the plan or the outcome. |

```
Archive  (DRY RUN - nothing written)

   Days   ID   Name        Email               Status
  ────────────────────────────────────────────────────────────
    900   3    Zoe Quinn   zoe@widgetco.com    would archive
    400   2    Bob Smith   bob@widgetco.com    would archive

  2 contact(s) would be archived | nothing was written |
  re-run with --archive to commit
```

Unlike `fix duplicates`, this one *is* allowed to run from `--from-file` even
with the write flag — archiving the wrong record is recoverable, so the stale-id
argument that blocks it for merging doesn't apply with the same force.

> **Merges cannot be undone**, from this tool or from the HubSpot UI. Archives
> can, for 90 days. Run either against a sandbox portal first, and start with
> `--limit`.

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
pytest              # run the offline suite
ruff check .        # lint
pytest -m live      # integration tests against a real portal (see below)
```

### Integration tests

`tests/test_live.py` is the only place that talks to a real portal, and the only
place that writes. It is **deselected by default** (`addopts = "-m 'not live'"`),
so `pytest` and CI stay offline. To run it:

```bash
export HUBSPOT_ACCESS_TOKEN=...     # needs contacts read AND write
export HUBSPOT_LIVE_TESTS=1
pytest -m live
```

Two switches on purpose. A token alone isn't enough, because you already have
one sitting in `.env` for ordinary use — so a stray `pytest -m live` would
otherwise start writing to whatever portal it points at. `HUBSPOT_LIVE_TESTS` is
the one you have to set deliberately. Without both, the module skips rather than
fails.

These tests create throwaway contacts tagged `crmclean-livetest`, merge and
archive them, and clean up in a fixture teardown that runs even on failure. Use
a **sandbox** portal. They prove the one thing the offline suite structurally
cannot: that our API call shapes are actually right.

Everything has a test suite — 345 offline tests covering email normalization, domain
bucketing, clustering, completeness scoring, timestamp parsing, staleness
windows, config parsing and rejection, flag-over-config precedence, report row
shapes, format resolution, skip accounting, merge and archive planning, partial
write failures, the interactive picker, and every CLI command end to end.
The CLI tests drive the real commands through `--from-file`, so the whole suite
runs offline with no HubSpot credentials and no network access.

Two guards worth knowing about, both in `tests/conftest.py`: every test runs in
an empty working directory (otherwise a real `config.yaml` sitting in the repo
would retune the whole suite), and Rich's console width is pinned (otherwise an
80-column fallback splits summary lines and breaks substring assertions for
reasons unrelated to the code).

Quick sanity check of the client in a Python REPL (from the project root, venv active):

```python
from hubspot_crm_clean.client import fetch_all_contacts

# `properties` is required — HubSpot returns almost nothing without it
contacts = fetch_all_contacts(["email", "firstname", "lastname"])
len(contacts)      # how many contacts came back
contacts[0]        # inspect one record's shape
```

## Project structure

```
src/hubspot_crm_clean/
├── cli.py            # Typer entrypoint — `fetch`, `audit *`, `fix *`
├── client.py         # HubSpot client: auth, pagination, normalization
├── config.py         # ✅ YAML config loading + strict validation
├── reports.py        # ✅ CSV/JSON export + destination resolution
├── fixes.py          # ✅ merge + archive planning and execution (the only writes)
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
| 5 | `fix` commands, auto-named reports, packaging | ✅ Done |

All five weeks are complete. Week 5 against the original plan:

| Planned | Shipped as |
| ------- | ---------- |
| `fix duplicates --interactive` | ✅ `fix duplicates -i` |
| `fix stale --archive` | ✅ `fix stale --archive` |
| Integration tests against a sandbox | ✅ `pytest -m live` |
| Publish to PyPI | 📦 packaged and validated; upload is a manual step |

Still open, and deliberately rejected by the config loader rather than silently
ignored — a config asking for any of these fails loudly instead of reading as
though it took effect:

| Not yet | Would be |
| ------- | -------- |
| `hubspot.object_types` | Audit companies and deals, not just contacts. |
| `rules.duplicates.match_fields` | Match on fields other than email domain + name. |
| `reports.columns` | Choose which columns a report carries. |
| `merge.*` | Configure which record survives, instead of the fixed three rules. |

The natural next step is `audit incomplete`'s obvious sequel — a `fix
incomplete` that *fills in* missing fields from a CSV, using the same
dry-run-by-default design the other two `fix` commands already share.

## License

MIT

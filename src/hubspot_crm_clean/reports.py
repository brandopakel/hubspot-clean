"""
Report export: findings to a CSV or JSON file.

The export is the machine-readable twin of the terminal tables in cli.py — same
findings, same numbers, with the two places the table truncated purely for column
width restored to full precision. (A cluster's confidence is repeated on every one
of its rows, and `last_seen` keeps its time as well as its date.)

Rows carry only JSON-native values: str, int, None, and list. That's what lets
the JSON writer serialize them untouched while the CSV writer flattens at the one
boundary where flattening is actually required (`_csv_cell`).

Two shape rules fall out of CSV being flat and JSON being not:

- JSON always writes one document, so `audit all` gets one file with a section
  per audit. CSV gets one file per audit, because three audits have three
  different column layouts and no single CSV can hold them.
- Consequently `--format csv --output -` is rejected for `audit all` rather than
  being answered with three headers glued into one stream.
"""

import csv
import json
import sys
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from hubspot_crm_clean.audits.duplicates import full_name

# What `--output` accepts to mean "stream it to stdout instead of a file".
STDOUT = "-"

# --save filenames: hubspot-duplicates-20260727-143022.json
#
# The timestamp lives in the *name*, deliberately, and not in the contents. Two
# runs of the same audit produce byte-identical files, so you can diff yesterday
# against today - while the names still never collide.
NAME_PREFIX = "hubspot"
NAME_STAMP = "%Y%m%d-%H%M%S"

# What --save falls back to when nothing else picks a format. There's no default
# for --output on purpose (see resolve_target), but --save means "you choose
# everything", and JSON is the one format that yields a single file for every
# command.
SAVE_FORMAT_FALLBACK = "json"


class ReportError(ValueError):
    """A report destination we can't write: no format to infer, or a
    format/destination pair that can't work. Carries a message meant to be shown
    to the user as-is."""


class ReportFormat(str, Enum):
    """The formats we can write. Typer turns this into the `--format` choices."""
    CSV = "csv"
    JSON = "json"


class ReportTarget(NamedTuple):
    """A validated destination: what to write, and where."""
    format: ReportFormat
    path: object    # Path, or None to stream to stdout

    @property
    def to_stdout(self):
        return self.path is None


# Column order per audit, and the single source of truth for it: the CSV header
# and the JSON keys are both built from here, so they can't drift apart.
COLUMNS = {
    "duplicates": ["cluster", "confidence", "domain", "id", "name", "email"],
    "incomplete": ["id", "name", "email", "score", "missing"],
    "stale": ["id", "name", "email", "days_inactive", "last_seen"],
    "merges": ["cluster", "confidence", "role", "id", "name", "email",
               "reason", "status", "detail"],
    "archives": ["id", "name", "email", "days_inactive", "status", "detail"],
}

SUFFIXES = {".csv": ReportFormat.CSV, ".json": ReportFormat.JSON}


# --------------------------------------------------------------------------
# Rows
#
# One dict per finding. `or None` on the text fields so a blank name and a blank
# email are both reported as absent rather than as an empty string that reads
# like a real value.
# --------------------------------------------------------------------------

def duplicate_rows(clusters):
    """Flatten clusters into one row per member.

    The cluster number and confidence repeat on every member's row, unlike the
    terminal table which labels only the first line. A CSV row has to stand on
    its own once someone sorts or filters the file.
    """
    rows = []
    for number, cluster in enumerate(clusters, start=1):
        for member in cluster.members:
            rows.append({
                "cluster": number,
                "confidence": round(cluster.confidence),
                "domain": cluster.domain,
                "id": member["id"],
                "name": full_name(member) or None,
                "email": member["properties"].get("email") or None,
            })
    return rows


def incomplete_rows(flagged):
    """One row per contact below the completeness bar."""
    return [
        {
            "id": item.contact["id"],
            "name": full_name(item.contact) or None,
            "email": item.contact["properties"].get("email") or None,
            "score": round(item.score),
            "missing": list(item.missing),
        }
        for item in flagged
    ]


def stale_rows(flagged):
    """One row per stale contact.

    days_inactive stays None for a contact we have never heard from, rather than
    becoming a magic number or the string "never" the table prints - null and 0
    are different answers, and a spreadsheet formula should not have to know that.
    """
    return [
        {
            "id": item.contact["id"],
            "name": full_name(item.contact) or None,
            "email": item.contact["properties"].get("email") or None,
            "days_inactive": item.days_inactive,
            "last_seen": item.last_seen.isoformat() if item.last_seen else None,
        }
        for item in flagged
    ]


# --------------------------------------------------------------------------
# Sections
#
# Each audit's findings, wrapped with the settings that produced them. A findings
# list on its own is not reproducible: "2 duplicates" means nothing without the
# threshold, and a report is read long after the command that made it.
# --------------------------------------------------------------------------

def duplicates_section(clusters, threshold):
    return {"threshold": threshold, "findings": duplicate_rows(clusters)}


def incomplete_section(flagged, min_completeness, required_fields):
    return {
        "min_completeness": min_completeness,
        "required_fields": list(required_fields),
        "findings": incomplete_rows(flagged),
    }


def stale_section(flagged, inactive_days, activity_fields):
    return {
        "inactive_days": inactive_days,
        "activity_fields": list(activity_fields),
        "findings": stale_rows(flagged),
    }


def merge_rows(outcomes):
    """Flatten merge outcomes into one row per record involved.

    The surviving record gets a row too, marked `kept`. A file listing only what
    was removed can't tell you what it was removed *into*, which is the one thing
    you need when a merge turns out to be wrong.
    """
    rows = []
    for number, outcome in enumerate(outcomes, start=1):
        plan = outcome.plan
        failed = dict(outcome.failures)

        def row(contact, role, status, detail=None, number=number, plan=plan):
            return {
                "cluster": number,
                "confidence": round(plan.confidence),
                "role": role,
                "id": contact["id"],
                "name": full_name(contact) or None,
                "email": contact["properties"].get("email") or None,
                "reason": plan.reason if role == "kept" else None,
                "status": status,
                "detail": detail,
            }

        rows.append(row(plan.primary, "kept", "kept"))
        for contact in plan.absorbed:
            if contact["id"] in failed:
                rows.append(row(contact, "absorbed", "failed", failed[contact["id"]]))
            elif contact["id"] in outcome.merged:
                rows.append(row(contact, "absorbed", "merged"))
            else:
                rows.append(row(contact, "absorbed", "planned"))    # dry run
    return rows


def merges_section(outcomes, threshold, applied):
    """`applied` is what separates a record of what happened from a proposal."""
    return {
        "threshold": threshold,
        "applied": applied,
        "findings": merge_rows(outcomes),
    }


def archive_rows(outcomes):
    """One row per record proposed for, or taken out of, the active CRM."""
    rows = []
    for outcome in outcomes:
        contact = outcome.plan.contact
        if outcome.failure is not None:
            status, detail = "failed", outcome.failure
        elif outcome.archived:
            status, detail = "archived", None
        else:
            status, detail = "planned", None       # dry run
        rows.append({
            "id": contact["id"],
            "name": full_name(contact) or None,
            "email": contact["properties"].get("email") or None,
            "days_inactive": outcome.plan.days_inactive,
            "status": status,
            "detail": detail,
        })
    return rows


def archives_section(outcomes, inactive_days, applied):
    return {
        "inactive_days": inactive_days,
        "applied": applied,
        "findings": archive_rows(outcomes),
    }


# --------------------------------------------------------------------------
# Destination
# --------------------------------------------------------------------------

def streams_to_stdout(report_format, output, save=False):
    """True when the report goes to stdout, so the CLI must print nothing else.

    Decided by the flags alone - deliberately, because the CLI has to silence its
    console before it reads the config file, and reading the config prints a line.
    """
    if save:
        # --save always names a file. Without this, `--save --format json` would
        # look exactly like `--format json` (which does stream) and the report
        # would go down the pipe instead of to the file it just named.
        return False
    if report_format is None and output is None:
        return False        # no export asked for at all
    return output is None or str(output) == STDOUT


def auto_name(label, report_format, now=None, parts=(), exists=None):
    """A generated filename for --save: hubspot-duplicates-20260727-143022.json

    `now` is injectable so tests don't depend on the wall clock, the same way
    find_stale takes one. Local time rather than UTC, because this name is read
    by a human looking at their own directory.

    A second run inside the same second would otherwise overwrite the first, so
    the name gains a counter. `parts` names the csv siblings that will really be
    written, since for a multi-file csv export the base name never exists on disk
    and checking it alone would always report free.
    """
    # Local wall-clock time is the point here, hence the suppression below: this
    # string is read by a human scanning their own directory, so 14:30 in the
    # filename should mean their 14:30.
    now = datetime.now() if now is None else now      # noqa: DTZ005
    exists = (lambda path: path.is_file()) if exists is None else exists
    stem = f"{NAME_PREFIX}-{label}-{now.strftime(NAME_STAMP)}"
    suffix = f".{report_format.value}"

    def taken(candidate):
        if not parts:
            return exists(candidate)
        return any(exists(path) for path in csv_paths(candidate, list(parts)).values())

    candidate = Path(f"{stem}{suffix}")
    counter = 2
    while taken(candidate):
        candidate = Path(f"{stem}-{counter}{suffix}")
        counter += 1
    return candidate


def resolve_target(report_format, output, default_format=None, multi=False,
                   save=False, label="audit", now=None, parts=()):
    """Work out where the report goes, or None when no export was asked for.

    Format precedence is flag -> the extension on --output -> the config file's
    default_format. The extension outranks the config because it is part of this
    invocation, and the more specific request should win. --save adds one last
    rung: having said "you pick the name", failing because no format was named
    would be missing the point, so it ends at JSON.

    Raises ReportError on a combination we can't honor. The CLI calls this before
    fetching anything, so bad flags fail in a second instead of after a full sync.
    """
    if save and output is not None:
        raise ReportError(
            "--save picks the filename, so it can't be combined with --output. "
            "Drop whichever one you didn't mean"
        )
    if save:
        resolved = report_format or default_format or ReportFormat(SAVE_FORMAT_FALLBACK)
        siblings = parts if (resolved is ReportFormat.CSV and multi) else ()
        return ReportTarget(resolved, auto_name(label, resolved, now=now, parts=siblings))
    if report_format is None and output is None:
        return None
    to_stdout = streams_to_stdout(report_format, output)

    resolved = report_format
    if resolved is None and not to_stdout:
        resolved = SUFFIXES.get(Path(output).suffix.lower())
    if resolved is None:
        resolved = default_format
    if resolved is None:
        raise ReportError(
            "no format to write: stdout has no extension to infer one from, "
            "so pass --format csv|json"
            if to_stdout else
            # plain quotes, not !r: repr doubles every backslash, which turns a
            # Windows path from something you can paste into something you can't
            f"could not tell the format from '{output}': "
            f"name it {' or '.join(sorted(SUFFIXES))}, or pass --format csv|json"
        )
    if resolved is ReportFormat.CSV and to_stdout and multi:
        raise ReportError(
            "csv cannot stream to stdout here: the three audits have different "
            "columns, so they cannot share one stream. Use --format json, or "
            "--output FILE to get one csv per audit"
        )
    return ReportTarget(resolved, None if to_stdout else Path(output))


def csv_paths(path, names):
    """Where each audit's CSV goes.

    A single audit keeps exactly the path it was given - asking for findings.csv
    and getting findings-stale.csv would be rude. Several audits can't share one
    file, so each takes a suffixed sibling: findings.csv -> findings-stale.csv.
    """
    if len(names) == 1:
        return {names[0]: path}
    return {name: path.with_name(f"{path.stem}-{name}{path.suffix}") for name in names}


# --------------------------------------------------------------------------
# Writers
# --------------------------------------------------------------------------

def _csv_cell(value):
    """Flatten one value into a CSV cell.

    None becomes empty rather than the string "None", and a list is joined -
    `missing` is the only list we export, and its members are field names.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def _write_csv(rows, columns, stream):
    """Write a header plus one line per row.

    lineterminator="\\n" pairs with the newline="" the caller opens files with.
    Without both, Python's text layer turns csv's default \\r\\n into \\r\\r\\n on
    Windows and every row gains a blank line.
    """
    writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({name: _csv_cell(row.get(name)) for name in columns})


def _write_json(payload, stream):
    json.dump(payload, stream, indent=2)
    stream.write("\n")      # a file ending mid-line annoys every other tool


def _open(path):
    """Open a report file. newline="" leaves line endings to the writers, so the
    same bytes come out on Windows as everywhere else."""
    return path.open("w", encoding="utf-8", newline="")


def write_report(target, scanned, sections):
    """Write the report and return the paths written - empty when it went to stdout.

    `sections` maps audit name -> the dict its *_section() builder returned, in
    the order they should appear. A section with no findings is still written:
    a report that is absent and a report that says "nothing wrong" are different
    answers, and only one of them survives a scheduled run.
    """
    if target.format is ReportFormat.JSON:
        payload = {"scanned": scanned, **sections}
        if target.to_stdout:
            _write_json(payload, sys.stdout)
            return []
        with _open(target.path) as stream:
            _write_json(payload, stream)
        return [target.path]

    if target.to_stdout:
        # resolve_target has already rejected multisection csv on stdout
        name, section = next(iter(sections.items()))
        _write_csv(section["findings"], COLUMNS[name], sys.stdout)
        return []

    destinations = csv_paths(target.path, list(sections))
    for name, section in sections.items():
        with _open(destinations[name]) as stream:
            _write_csv(section["findings"], COLUMNS[name], stream)
    return list(destinations.values())

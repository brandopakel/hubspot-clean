"""Tests for report export: row shapes, destination resolution, and the CLI flags."""

import csv
import json
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from typer.testing import CliRunner

from hubspot_crm_clean.audits.duplicates import find_duplicates
from hubspot_crm_clean.audits.incomplete import find_incomplete
from hubspot_crm_clean.audits.stale import find_stale
from hubspot_crm_clean.cli import app
from hubspot_crm_clean.reports import (
    COLUMNS,
    ReportError,
    ReportFormat,
    auto_name,
    csv_paths,
    duplicate_rows,
    incomplete_rows,
    resolve_target,
    stale_rows,
    streams_to_stdout,
)

runner = CliRunner()


def contact(contact_id, email, first, last, days_ago=1, complete=True):
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    return {
        "id": contact_id,
        "properties": {
            "email": email,
            "firstname": first,
            "lastname": last,
            "company": "Acme" if complete else None,
            "phone": "555-0100" if complete else None,
            "lifecyclestage": "customer",
            "hs_last_activity_date": stamp,
            "lastmodifieddate": stamp,
        },
    }


def write(tmp_path, name, contacts):
    path = tmp_path / name
    path.write_text(json.dumps({"results": contacts}), encoding="utf-8")
    return path


@pytest.fixture
def dirty(tmp_path):
    """One finding per audit — 1 and 2 duplicate, 3 is incomplete, 4 is ancient."""
    return write(tmp_path, "dirty.json", [
        contact("1", "jane.doe@acme.com", "Jane", "Doe"),
        contact("2", "j.doe@acme.com", "Janet", "Doe"),
        contact("3", "bob@widgetco.com", "Bob", "Smith", complete=False),
        contact("4", "zoe@widgetco.com", "Zoe", "Quinn", days_ago=400),
    ])


@pytest.fixture
def clean(tmp_path):
    return write(tmp_path, "clean.json", [
        contact("1", "ann@acme.com", "Ann", "Alpha"),
        contact("2", "bob@beta.com", "Bob", "Beta"),
    ])


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


# --------------------------------------------------------------------------
# Rows
# --------------------------------------------------------------------------

def test_duplicate_rows_repeat_the_cluster_on_every_member():
    # the table labels only the first line of a cluster; a csv row has to stand
    # alone once someone sorts the file, so the label repeats
    clusters = find_duplicates([
        contact("1", "jane.doe@acme.com", "Jane", "Doe"),
        contact("2", "j.doe@acme.com", "Jane", "Doe"),
    ])
    rows = duplicate_rows(clusters)
    assert [row["cluster"] for row in rows] == [1, 1]
    assert [row["confidence"] for row in rows] == [100, 100]
    assert [row["id"] for row in rows] == ["1", "2"]
    assert {row["domain"] for row in rows} == {"acme.com"}


def test_duplicate_rows_number_clusters_from_one():
    clusters = find_duplicates([
        contact("1", "jane@acme.com", "Jane", "Doe"),
        contact("2", "jane.d@acme.com", "Jane", "Doe"),
        contact("3", "bob@beta.com", "Bob", "Smith"),
        contact("4", "bob.s@beta.com", "Bob", "Smith"),
    ])
    rows = duplicate_rows(clusters)
    assert sorted({row["cluster"] for row in rows}) == [1, 2]


def test_incomplete_rows_keep_missing_as_a_list():
    # JSON should carry the real list; only the csv writer flattens it
    flagged = find_incomplete([contact("1", "bob@acme.com", "Bob", "Smith", complete=False)])
    rows = incomplete_rows(flagged)
    assert rows[0]["missing"] == ["company", "phone"]
    assert rows[0]["score"] == 50


def test_stale_rows_report_never_seen_as_null_not_zero():
    # `never` and `0 days` are different answers - a formula reading the file
    # must not have to know that "never" is a word the table happens to print
    flagged = find_stale(
        [{"id": "1", "properties": {"email": "x@acme.com", "firstname": "X", "lastname": "Y"}}],
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    rows = stale_rows(flagged)
    assert rows[0]["days_inactive"] is None
    assert rows[0]["last_seen"] is None


def test_stale_rows_keep_the_time_the_table_truncates():
    seen = datetime(2025, 1, 2, 3, 4, 5, tzinfo=UTC)
    flagged = find_stale(
        [{"id": "1", "properties": {"email": "x@acme.com", "firstname": "X", "lastname": "Y",
                                    "hs_last_activity_date": seen.isoformat()}}],
        now=datetime(2026, 1, 1, tzinfo=UTC),
    )
    rows = stale_rows(flagged)
    assert rows[0]["last_seen"].startswith("2025-01-02T03:04:05")


def test_a_blank_name_is_absent_not_an_empty_string():
    flagged = find_incomplete([{"id": "1", "properties": {"email": None}}])
    assert incomplete_rows(flagged)[0]["name"] is None


# --------------------------------------------------------------------------
# resolve_target
# --------------------------------------------------------------------------

def test_no_flags_means_no_export():
    assert resolve_target(None, None) is None
    assert streams_to_stdout(None, None) is False


@pytest.mark.parametrize("name,expected", [
    ("findings.csv", ReportFormat.CSV),
    ("findings.json", ReportFormat.JSON),
    ("FINDINGS.CSV", ReportFormat.CSV),
])
def test_format_is_inferred_from_the_extension(tmp_path, name, expected):
    target = resolve_target(None, tmp_path / name)
    assert target.format is expected
    assert target.to_stdout is False


def test_the_flag_beats_the_extension(tmp_path):
    target = resolve_target(ReportFormat.JSON, tmp_path / "findings.csv")
    assert target.format is ReportFormat.JSON


def test_the_extension_beats_the_config_default(tmp_path):
    # the extension is part of this invocation, so it outranks a stored default
    target = resolve_target(None, tmp_path / "findings.csv", default_format=ReportFormat.JSON)
    assert target.format is ReportFormat.CSV


def test_the_config_default_fills_in_what_the_extension_cannot(tmp_path):
    target = resolve_target(None, tmp_path / "findings.txt", default_format=ReportFormat.JSON)
    assert target.format is ReportFormat.JSON


def test_an_unknown_extension_with_no_default_is_rejected(tmp_path):
    with pytest.raises(ReportError, match="could not tell the format"):
        resolve_target(None, tmp_path / "findings.txt")


def test_the_rejected_path_is_shown_as_typed(tmp_path):
    # repr() would double every backslash on Windows, turning a path you could
    # paste back into one you couldn't
    path = tmp_path / "findings.txt"
    with pytest.raises(ReportError) as err:
        resolve_target(None, path)
    assert str(path) in str(err.value)


def test_format_alone_streams_to_stdout():
    target = resolve_target(ReportFormat.JSON, None)
    assert target.to_stdout is True
    assert streams_to_stdout(ReportFormat.JSON, None) is True


def test_dash_streams_to_stdout():
    target = resolve_target(ReportFormat.JSON, "-")
    assert target.to_stdout is True


def test_stdout_with_no_format_is_rejected():
    # there is no extension on "-" to infer anything from
    with pytest.raises(ReportError, match="no format"):
        resolve_target(None, "-")


def test_csv_to_stdout_is_rejected_for_a_combined_audit():
    # three audits have three column layouts; they cannot share one stream
    with pytest.raises(ReportError, match="csv cannot stream to stdout"):
        resolve_target(ReportFormat.CSV, "-", multi=True)


def test_csv_to_stdout_is_fine_for_a_single_audit():
    assert resolve_target(ReportFormat.CSV, "-").to_stdout is True


# --------------------------------------------------------------------------
# csv_paths
# --------------------------------------------------------------------------

def test_one_audit_keeps_the_path_it_was_given(tmp_path):
    path = tmp_path / "findings.csv"
    assert csv_paths(path, ["stale"]) == {"stale": path}


def test_several_audits_get_suffixed_siblings(tmp_path):
    paths = csv_paths(tmp_path / "findings.csv", ["duplicates", "stale"])
    assert paths["duplicates"].name == "findings-duplicates.csv"
    assert paths["stale"].name == "findings-stale.csv"


# --------------------------------------------------------------------------
# CLI - files
# --------------------------------------------------------------------------

def test_single_audit_writes_one_csv_at_the_given_path(tmp_path, dirty):
    out = tmp_path / "dupes.csv"
    result = runner.invoke(app, [
        "audit", "duplicates", "--from-file", str(dirty), "--output", str(out),
    ])
    assert result.exit_code == 0
    assert out.is_file()
    rows = read_csv(out)
    assert [row["id"] for row in rows] == ["1", "2"]
    assert list(rows[0]) == COLUMNS["duplicates"]


def test_audit_all_json_is_one_file_with_a_section_per_audit(tmp_path, dirty):
    out = tmp_path / "findings.json"
    result = runner.invoke(app, [
        "audit", "all", "--from-file", str(dirty), "--output", str(out),
    ])
    assert result.exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["scanned"] == 4
    assert list(payload) == ["scanned", "duplicates", "incomplete", "stale"]
    assert len(payload["duplicates"]["findings"]) == 2      # one cluster, two members
    assert len(payload["incomplete"]["findings"]) == 1
    assert len(payload["stale"]["findings"]) == 1


def test_audit_all_csv_writes_one_file_per_audit(tmp_path, dirty):
    out = tmp_path / "findings.csv"
    result = runner.invoke(app, [
        "audit", "all", "--from-file", str(dirty), "--output", str(out),
    ])
    assert result.exit_code == 0
    assert not out.exists()      # the bare name is never written, only the siblings
    for name in ("duplicates", "incomplete", "stale"):
        sibling = tmp_path / f"findings-{name}.csv"
        assert sibling.is_file(), f"missing {sibling.name}"
        assert list(read_csv(sibling)[0]) == COLUMNS[name]


def test_the_settings_that_produced_the_findings_travel_with_them(tmp_path, dirty):
    # a findings list alone isn't reproducible: "2 duplicates" means nothing
    # without the threshold that decided it
    out = tmp_path / "findings.json"
    runner.invoke(app, [
        "audit", "all", "--from-file", str(dirty), "--output", str(out), "-t", "92", "-d", "365",
    ])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["duplicates"]["threshold"] == 92
    assert payload["stale"]["inactive_days"] == 365
    assert payload["incomplete"]["required_fields"] == ["email", "company",
                                                        "lifecyclestage", "phone"]


def test_a_clean_run_still_writes_the_report(tmp_path, clean):
    # an absent report and one that says "nothing wrong" are different answers,
    # and only one of them survives a scheduled run
    out = tmp_path / "findings.json"
    result = runner.invoke(app, [
        "audit", "all", "--from-file", str(clean), "--output", str(out),
    ])
    assert result.exit_code == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["duplicates"]["findings"] == []
    assert payload["scanned"] == 2


def test_a_clean_csv_still_has_its_header(tmp_path, clean):
    out = tmp_path / "stale.csv"
    runner.invoke(app, ["audit", "stale", "--from-file", str(clean), "--output", str(out)])
    assert out.read_text(encoding="utf-8").strip() == ",".join(COLUMNS["stale"])


def test_strict_still_writes_the_report_before_exiting(tmp_path, dirty):
    # --strict is for CI, which is exactly where the artifact matters most
    out = tmp_path / "findings.json"
    result = runner.invoke(app, [
        "audit", "all", "--from-file", str(dirty), "--output", str(out), "--strict",
    ])
    assert result.exit_code == 1
    assert out.is_file()


def test_the_report_path_is_echoed(tmp_path, dirty):
    out = tmp_path / "findings.json"
    result = runner.invoke(app, [
        "audit", "duplicates", "--from-file", str(dirty), "--output", str(out),
    ])
    assert "Wrote" in result.stdout
    assert "findings.json" in result.stdout


def test_files_use_one_line_ending_everywhere(tmp_path, dirty):
    # csv defaults to \r\n, and Python's text layer would turn that into \r\r\n
    # on Windows - the writers and newline="" have to agree
    out = tmp_path / "dupes.csv"
    runner.invoke(app, ["audit", "duplicates", "--from-file", str(dirty), "--output", str(out)])
    assert b"\r" not in out.read_bytes()


# --------------------------------------------------------------------------
# CLI - stdout
# --------------------------------------------------------------------------

def test_streaming_to_stdout_emits_only_the_report(dirty):
    # every table, panel and progress bar has to stay out of the pipe
    result = runner.invoke(app, [
        "audit", "all", "--from-file", str(dirty), "--format", "json", "--output", "-",
    ])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)      # would raise on any stray table output
    assert payload["scanned"] == 4


def test_format_alone_streams_to_stdout_too(dirty):
    result = runner.invoke(app, ["audit", "stale", "--from-file", str(dirty), "-f", "json"])
    assert json.loads(result.stdout)["stale"]["findings"][0]["id"] == "4"


def test_csv_streams_to_stdout_for_a_single_audit(dirty):
    result = runner.invoke(app, ["audit", "incomplete", "--from-file", str(dirty), "-f", "csv"])
    rows = list(csv.DictReader(result.stdout.splitlines()))
    assert [row["id"] for row in rows] == ["3"]
    assert rows[0]["missing"] == "company, phone"


def test_a_config_echo_never_reaches_the_pipe(isolated_cwd, dirty):
    # `Using config ...` is printed on every run that finds one; it would be the
    # first line of the JSON document otherwise
    (isolated_cwd / "config.yaml").write_text(yaml.safe_dump({"rules": {}}), encoding="utf-8")
    result = runner.invoke(app, ["audit", "stale", "--from-file", str(dirty), "-f", "json"])
    assert json.loads(result.stdout)      # parses, so nothing was prepended


def test_quiet_does_not_leak_into_the_next_run(dirty):
    # the console is a module global; a streaming run must not silence the one after
    runner.invoke(app, ["audit", "duplicates", "--from-file", str(dirty), "-f", "json"])
    result = runner.invoke(app, ["audit", "duplicates", "--from-file", str(dirty)])
    assert "1 cluster(s)" in result.stdout


# --------------------------------------------------------------------------
# CLI - rejection
# --------------------------------------------------------------------------

def test_csv_to_stdout_for_audit_all_fails_with_a_message_not_a_traceback(dirty):
    result = runner.invoke(app, ["audit", "all", "--from-file", str(dirty), "-f", "csv"])
    assert result.exit_code == 1
    assert "Bad report options" in result.stdout
    assert "--format json" in result.stdout


def test_an_unknown_extension_is_rejected_by_the_cli(tmp_path, dirty):
    result = runner.invoke(app, [
        "audit", "stale", "--from-file", str(dirty), "--output", str(tmp_path / "out.txt"),
    ])
    assert result.exit_code == 1
    assert "could not tell the format" in result.stdout


def test_bad_report_flags_are_caught_before_any_contacts_are_loaded(tmp_path):
    # the point of validating up front: this must not wait for a full fetch, and
    # the from-file path here does not even exist
    result = runner.invoke(app, ["audit", "all", "-f", "csv", "-o", "-"])
    assert result.exit_code == 1
    assert "Bad report options" in result.stdout


def test_an_unwritable_destination_reports_cleanly(tmp_path, dirty):
    result = runner.invoke(app, [
        "audit", "stale", "--from-file", str(dirty),
        "--output", str(tmp_path / "nope" / "findings.json"),
    ])
    assert result.exit_code == 1
    assert "Failed to write report" in result.stdout


@pytest.mark.parametrize("value", ["xml", "yaml"])
def test_an_unsupported_format_is_rejected_by_typer(dirty, value):
    result = runner.invoke(app, ["audit", "stale", "--from-file", str(dirty), "-f", value])
    assert result.exit_code == 2      # Typer rejects the choice before we run


# --------------------------------------------------------------------------
# CLI - config
# --------------------------------------------------------------------------

def test_config_supplies_the_default_format(tmp_path, dirty):
    config = tmp_path / "c.yaml"
    config.write_text(yaml.safe_dump({"reports": {"default_format": "json"}}), encoding="utf-8")
    out = tmp_path / "findings.txt"      # nothing to infer from
    result = runner.invoke(app, [
        "audit", "stale", "--from-file", str(dirty),
        "--config", str(config), "--output", str(out),
    ])
    assert result.exit_code == 0
    assert json.loads(out.read_text(encoding="utf-8"))["stale"]["findings"]


def test_a_default_format_alone_does_not_trigger_an_export(tmp_path, dirty):
    # the config supplies the format, never the intent to export - otherwise every
    # run in a configured directory would start writing files
    config = tmp_path / "c.yaml"
    config.write_text(yaml.safe_dump({"reports": {"default_format": "json"}}), encoding="utf-8")
    result = runner.invoke(app, [
        "audit", "stale", "--from-file", str(dirty), "--config", str(config),
    ])
    assert result.exit_code == 0
    assert "Wrote" not in result.stdout
    assert list(tmp_path.glob("*.json")) == [dirty]


# --------------------------------------------------------------------------
# auto_name / --save
# --------------------------------------------------------------------------

# Naive on purpose: auto_name formats local wall-clock time into the filename,
# so a tz-aware value here would test something the code never sees.
FIXED = datetime(2026, 7, 27, 14, 30, 22)      # noqa: DTZ001


def test_the_generated_name_carries_the_audit_and_the_timestamp():
    name = auto_name("duplicates", ReportFormat.JSON, now=FIXED, exists=lambda p: False)
    assert name.name == "hubspot-duplicates-20260727-143022.json"


def test_the_extension_follows_the_format():
    name = auto_name("stale", ReportFormat.CSV, now=FIXED, exists=lambda p: False)
    assert name.suffix == ".csv"


def test_a_second_run_in_the_same_second_does_not_clobber_the_first():
    taken = {"hubspot-audit-20260727-143022.json"}
    name = auto_name("audit", ReportFormat.JSON, now=FIXED, exists=lambda p: p.name in taken)
    assert name.name == "hubspot-audit-20260727-143022-2.json"


def test_the_counter_keeps_climbing_past_two():
    taken = {"hubspot-audit-20260727-143022.json", "hubspot-audit-20260727-143022-2.json"}
    name = auto_name("audit", ReportFormat.JSON, now=FIXED, exists=lambda p: p.name in taken)
    assert name.name == "hubspot-audit-20260727-143022-3.json"


def test_multi_csv_collides_on_the_siblings_not_the_base_name():
    # the base name is never written for a multi-file csv export, so checking it
    # alone would always report free and silently overwrite last run's files
    taken = {"hubspot-audit-20260727-143022-stale.csv"}
    name = auto_name("audit", ReportFormat.CSV, now=FIXED,
                     parts=("duplicates", "stale"), exists=lambda p: p.name in taken)
    assert name.name == "hubspot-audit-20260727-143022-2.csv"


def test_save_defaults_to_json():
    # --output has no default format on purpose; --save means "you pick everything"
    assert resolve_target(None, None, save=True, now=FIXED).format is ReportFormat.JSON


def test_save_respects_an_explicit_format():
    target = resolve_target(ReportFormat.CSV, None, save=True, now=FIXED)
    assert target.format is ReportFormat.CSV


def test_save_respects_the_config_default():
    target = resolve_target(None, None, default_format=ReportFormat.CSV, save=True, now=FIXED)
    assert target.format is ReportFormat.CSV


def test_save_never_streams_to_stdout():
    # without the save guard, `--save --format json` looks exactly like
    # `--format json`, which does stream - and the file would stay empty
    assert streams_to_stdout(ReportFormat.JSON, None, save=True) is False
    assert resolve_target(ReportFormat.JSON, None, save=True, now=FIXED).to_stdout is False


def test_save_and_output_together_are_rejected(tmp_path):
    with pytest.raises(ReportError, match="--save picks the filename"):
        resolve_target(None, tmp_path / "x.json", save=True)


def test_cli_save_writes_a_generated_file(isolated_cwd, dirty):
    result = runner.invoke(app, ["audit", "duplicates", "--from-file", str(dirty), "--save"])
    assert result.exit_code == 0
    written = list(isolated_cwd.glob("hubspot-duplicates-*.json"))
    assert len(written) == 1
    assert json.loads(written[0].read_text(encoding="utf-8"))["duplicates"]["findings"]
    assert "Wrote" in result.stdout


def test_cli_save_lands_in_the_working_directory(isolated_cwd, dirty):
    runner.invoke(app, ["audit", "all", "--from-file", str(dirty), "-s"])
    assert list(isolated_cwd.glob("hubspot-audit-*.json"))


def test_cli_save_with_csv_for_audit_all_writes_one_file_per_audit(isolated_cwd, dirty):
    runner.invoke(app, ["audit", "all", "--from-file", str(dirty), "-s", "-f", "csv"])
    for name in ("duplicates", "incomplete", "stale"):
        assert list(isolated_cwd.glob(f"hubspot-audit-*-{name}.csv")), name


def test_cli_save_and_output_together_are_rejected(tmp_path, dirty):
    result = runner.invoke(app, [
        "audit", "stale", "--from-file", str(dirty), "-s", "-o", str(tmp_path / "x.json"),
    ])
    assert result.exit_code == 1
    assert "--save picks the filename" in result.stdout

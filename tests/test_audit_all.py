"""Tests for the combined `audit all` command."""

import json
from datetime import UTC, datetime, timedelta

import pytest
import yaml
from typer.testing import CliRunner

from hubspot_crm_clean import cli
from hubspot_crm_clean.cli import app

runner = CliRunner()


def contact(contact_id, email, first, last, days_ago, complete=True):
    stamp = (datetime.now(UTC) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")
    return {
        "id": contact_id,
        "properties": {
            "email": email,
            "firstname": first,
            "lastname": last,
            "company": "Acme" if complete else None,
            "phone": "555-0100",
            "lifecyclestage": "customer" if complete else None,
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
    """One of each finding, so a miss in any audit shows up as a wrong count.

    Contacts 1 and 2 score 94 on name similarity and share a domain (one cluster),
    3 is missing company and lifecyclestage (50%), and 4 has been silent well past
    the 90-day default. 3 and 4 share a domain but their names score nowhere near
    the bar, so they don't also register as duplicates.
    """
    return write(tmp_path, "dirty.json", [
        contact("1", "jane.doe@acme.com", "Jane", "Doe", days_ago=5),
        contact("2", "j.doe@acme.com", "Janet", "Doe", days_ago=5),
        contact("3", "bob@widgetco.com", "Bob", "Smith", days_ago=5, complete=False),
        contact("4", "zoe@widgetco.com", "Zoe", "Quinn", days_ago=400),
    ])


@pytest.fixture
def clean(tmp_path):
    return write(tmp_path, "clean.json", [
        contact("1", "ann@acme.com", "Ann", "Alpha", days_ago=1),
        contact("2", "bob@beta.com", "Bob", "Beta", days_ago=1),
    ])


def test_runs_every_audit(dirty):
    result = runner.invoke(app, ["audit", "all", "--from-file", str(dirty)])
    assert result.exit_code == 0
    for section in ("Duplicates", "Incomplete", "Stale", "Summary"):
        assert section in result.stdout


def test_reports_each_audits_count(dirty):
    result = runner.invoke(app, ["audit", "all", "--from-file", str(dirty)])
    assert "1 duplicate cluster(s)" in result.stdout
    assert "1 incomplete" in result.stdout
    assert "1 stale" in result.stdout
    assert "4 scanned" in result.stdout


def test_findings_reach_the_tables_not_just_the_counts(dirty):
    result = runner.invoke(app, ["audit", "all", "--from-file", str(dirty)])
    assert "jane.doe@acme.com" in result.stdout     # duplicate cluster member
    assert "Bob Smith" in result.stdout             # incomplete row
    assert "Zoe Quinn" in result.stdout             # stale row


def test_a_clean_portal_reports_no_issues(clean):
    result = runner.invoke(app, ["audit", "all", "--from-file", str(clean)])
    assert result.exit_code == 0
    assert "No issues found" in result.stdout


def test_clean_still_shows_that_all_three_audits_ran(clean):
    result = runner.invoke(app, ["audit", "all", "--from-file", str(clean)])
    assert "No duplicates" in result.stdout
    assert "All contacts complete" in result.stdout
    assert "All contacts active" in result.stdout


def test_strict_exit_codes(dirty, clean):
    assert runner.invoke(app, ["audit", "all", "--from-file", str(dirty)]).exit_code == 0
    assert runner.invoke(
        app, ["audit", "all", "--from-file", str(dirty), "--strict"]
    ).exit_code == 1
    assert runner.invoke(
        app, ["audit", "all", "--from-file", str(clean), "--strict"]
    ).exit_code == 0


def test_strict_trips_on_any_single_audit(tmp_path):
    # only a stale contact, nothing else wrong - strict must still exit 1
    only_stale = write(tmp_path, "stale.json", [
        contact("1", "ann@acme.com", "Ann", "Alpha", days_ago=400),
    ])
    assert runner.invoke(
        app, ["audit", "all", "--from-file", str(only_stale), "--strict"]
    ).exit_code == 1


def test_every_tuning_flag_is_accepted(dirty):
    result = runner.invoke(app, [
        "audit", "all", "--from-file", str(dirty),
        "-t", "95", "-m", "100", "-d", "365", "-r", "email", "--activity-field", "createdate",
    ])
    assert result.exit_code == 0


def test_flags_change_each_audit_independently(dirty):
    # threshold 95 clears the duplicates (they score 94) and touches nothing else
    result = runner.invoke(app, ["audit", "all", "--from-file", str(dirty), "-t", "95"])
    assert "No duplicates" in result.stdout
    assert "1 incomplete" in result.stdout
    assert "1 stale" in result.stdout


def test_reads_the_config(tmp_path, dirty):
    config = tmp_path / "c.yaml"
    config.write_text(yaml.safe_dump({"rules": {"duplicates": {"match_threshold": 95}}}),
                      encoding="utf-8")
    result = runner.invoke(app, [
        "audit", "all", "--from-file", str(dirty), "--config", str(config),
    ])
    assert "No duplicates" in result.stdout
    assert "1 incomplete" in result.stdout


def test_no_required_fields_says_so_rather_than_claiming_completeness(tmp_path, dirty):
    # "All contacts complete" would be a lie when nothing is being checked
    config = tmp_path / "c.yaml"
    config.write_text(yaml.safe_dump({"rules": {"incomplete": {"required_fields": []}}}),
                      encoding="utf-8")
    result = runner.invoke(app, [
        "audit", "all", "--from-file", str(dirty), "--config", str(config),
    ])
    assert "No required fields configured" in result.stdout
    assert "All contacts complete" not in result.stdout


# --------------------------------------------------------------------------
# The point of the command: one fetch feeding all three audits
# --------------------------------------------------------------------------

@pytest.fixture
def captured_fetch(monkeypatch):
    """Replace the HubSpot call and record what it was asked for."""
    calls = []

    def fake_fetch(properties, on_page=None):
        calls.append(properties)
        return []

    monkeypatch.setattr(cli, "fetch_all_contacts", fake_fetch)
    return calls


def test_fetches_once_for_all_three_audits(captured_fetch):
    # running the three commands separately pages through the whole portal
    # three times; this is the entire reason `audit all` exists
    result = runner.invoke(app, ["audit", "all"])
    assert result.exit_code == 0
    assert len(captured_fetch) == 1


def test_requests_the_union_of_every_audits_properties(captured_fetch):
    runner.invoke(app, ["audit", "all"])
    (properties,) = captured_fetch
    assert properties == [
        "email", "firstname", "lastname",            # identity, shown in every table
        "company", "lifecyclestage", "phone",        # required fields (email deduped)
        "hs_last_activity_date", "lastmodifieddate",  # activity fields
    ]


def test_properties_are_not_requested_twice(captured_fetch):
    runner.invoke(app, ["audit", "all", "-r", "email", "-r", "firstname"])
    (properties,) = captured_fetch
    assert len(properties) == len(set(properties))


def test_custom_fields_reach_the_fetch(captured_fetch):
    runner.invoke(app, ["audit", "all", "-r", "jobtitle", "--activity-field", "notes_last_updated"])
    (properties,) = captured_fetch
    assert "jobtitle" in properties
    assert "notes_last_updated" in properties
    assert "company" not in properties      # replaced by -r, not added to


def test_a_fetch_failure_exits_cleanly(monkeypatch):
    def boom(properties, on_page=None):
        raise RuntimeError("401 Unauthorized")

    monkeypatch.setattr(cli, "fetch_all_contacts", boom)
    result = runner.invoke(app, ["audit", "all"])
    assert result.exit_code == 1
    assert "Failed to load contacts" in result.stdout
    assert "401 Unauthorized" in result.stdout

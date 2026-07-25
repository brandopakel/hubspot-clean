"""Tests for audits/stale.py."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hubspot_crm_clean.audits.stale import (
    DEFAULT_ACTIVITY_FIELDS,
    find_stale,
    last_activity,
    parse_timestamp,
)
from hubspot_crm_clean.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "contacts.json"
NOW = datetime(2026, 7, 24, tzinfo=UTC)    # pinned so tests never drift
runner = CliRunner()


def contact(contact_id, activity=None, modified=None):
    return {
        "id": contact_id,
        "properties": {
            "email": f"{contact_id}@acme.com",
            "firstname": "Test",
            "lastname": contact_id,
            "hs_last_activity_date": activity,
            "lastmodifieddate": modified,
        },
    }


def days_before(days):
    return (NOW - timedelta(days=days)).isoformat().replace("+00:00", "Z")


# --------------------------------------------------------------------------
# parse_timestamp
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("2026-06-01T00:00:00Z", datetime(2026, 6, 1, tzinfo=UTC)),
    ("2026-06-01T00:00:00.123Z", datetime(2026, 6, 1, 0, 0, 0, 123000, tzinfo=UTC)),
    ("2026-06-01T00:00:00+00:00", datetime(2026, 6, 1, tzinfo=UTC)),
    ("2026-06-01", datetime(2026, 6, 1, tzinfo=UTC)),
])
def test_parses_hubspot_timestamps(raw, expected):
    assert parse_timestamp(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "garbage", "not-a-date", 12345, []])
def test_unparseable_returns_none_instead_of_raising(raw):
    # one malformed record must not abort the whole audit
    assert parse_timestamp(raw) is None


def test_naive_timestamps_are_assumed_utc():
    parsed = parse_timestamp("2026-06-01T00:00:00")
    assert parsed.tzinfo is not None
    assert parsed == datetime(2026, 6, 1, tzinfo=UTC)


# --------------------------------------------------------------------------
# last_activity
# --------------------------------------------------------------------------

def test_takes_the_most_recent_across_fields():
    item = contact("1", activity="2024-01-01T00:00:00Z", modified="2026-07-01T00:00:00Z")
    assert last_activity(item) == datetime(2026, 7, 1, tzinfo=UTC)


def test_ignores_unparseable_fields_but_uses_the_rest():
    item = contact("1", activity="garbage", modified="2026-07-01T00:00:00Z")
    assert last_activity(item) == datetime(2026, 7, 1, tzinfo=UTC)


def test_no_usable_dates_returns_none():
    assert last_activity(contact("1")) is None
    assert last_activity(contact("1", activity="garbage")) is None


def test_empty_activity_field_list_checks_nothing():
    # `or` would silently fall back to the defaults here; `is None` must not
    item = contact("1", activity="2026-07-01T00:00:00Z")
    assert last_activity(item, []) is None


def test_custom_activity_fields():
    item = contact("1", activity="2020-01-01T00:00:00Z", modified="2026-07-01T00:00:00Z")
    assert last_activity(item, ["hs_last_activity_date"]) == datetime(
        2020, 1, 1, tzinfo=UTC
    )


# --------------------------------------------------------------------------
# find_stale
# --------------------------------------------------------------------------

def test_recent_activity_is_not_flagged():
    assert find_stale([contact("1", activity=days_before(10))], now=NOW) == []


def test_cutoff_is_inclusive():
    exactly = [contact("1", activity=days_before(90))]
    assert len(find_stale(exactly, inactive_days=90, now=NOW)) == 1
    assert find_stale(exactly, inactive_days=91, now=NOW) == []


def test_days_inactive_is_reported():
    (item,) = find_stale([contact("1", activity=days_before(200))], now=NOW)
    assert item.days_inactive == 200
    assert item.last_seen == NOW - timedelta(days=200)


def test_contacts_with_no_activity_at_all_are_flagged_as_never_seen():
    (item,) = find_stale([contact("1")], now=NOW)
    assert item.last_seen is None
    assert item.days_inactive is None       # distinguishes "never" from "silent for N days"


def test_never_seen_sorts_first_then_longest_silent():
    flagged = find_stale([
        contact("1", activity=days_before(100)),
        contact("2"),                            # never seen
        contact("3", activity=days_before(900)),
    ], now=NOW)
    assert [item.contact["id"] for item in flagged] == ["2", "3", "1"]


def test_most_recent_field_wins_so_a_touched_record_is_not_stale():
    # old activity date but recently modified -> still active
    item = contact("1", activity=days_before(900), modified=days_before(5))
    assert find_stale([item], now=NOW) == []


def test_now_defaults_to_the_wall_clock():
    # no now= passed; a decade-old record is stale whenever this runs
    assert len(find_stale([contact("1", activity="2015-01-01T00:00:00Z")])) == 1


def test_empty_input():
    assert find_stale([], now=NOW) == []


def test_against_the_real_fixture():
    contacts = json.load(FIXTURE.open(encoding="utf-8"))["results"]
    flagged = find_stale(contacts, now=NOW)
    assert [item.contact["id"] for item in flagged] == ["2"]
    assert flagged[0].days_inactive == 569


def test_defaults_match_the_documented_config():
    # config.example.yaml declares these under rules.stale.activity_fields
    assert DEFAULT_ACTIVITY_FIELDS == ["hs_last_activity_date", "lastmodifieddate"]


# --------------------------------------------------------------------------
# CLI
#
# These write fixtures with dates relative to the real clock, so they stay
# correct whenever the suite runs rather than drifting as the pinned NOW ages.
# --------------------------------------------------------------------------

def write_relative(tmp_path, name, day_offsets):
    real_now = datetime.now(UTC)
    results = []
    for index, offset in enumerate(day_offsets, start=1):
        stamp = None if offset is None else (
            (real_now - timedelta(days=offset)).isoformat().replace("+00:00", "Z")
        )
        results.append(contact(str(index), activity=stamp, modified=stamp))
    path = tmp_path / name
    path.write_text(json.dumps({"results": results}))
    return path


def test_cli_flags_stale_contacts(tmp_path):
    path = write_relative(tmp_path, "s.json", [5, 400])
    result = runner.invoke(app, ["audit", "stale", "--from-file", str(path)])
    assert result.exit_code == 0
    assert "1 stale" in result.stdout


def test_cli_reports_clean(tmp_path):
    path = write_relative(tmp_path, "s.json", [1, 2])
    result = runner.invoke(app, ["audit", "stale", "--from-file", str(path)])
    assert result.exit_code == 0
    assert "show recent activity" in result.stdout


def test_cli_inactive_days_option(tmp_path):
    path = write_relative(tmp_path, "s.json", [200])
    args = ["audit", "stale", "--from-file", str(path)]
    assert "1 stale" in runner.invoke(app, args).stdout
    assert "show recent activity" in runner.invoke(app, args + ["-d", "365"]).stdout


def test_cli_marks_never_seen(tmp_path):
    path = write_relative(tmp_path, "s.json", [None])
    result = runner.invoke(app, ["audit", "stale", "--from-file", str(path)])
    assert "never" in result.stdout
    assert "1 never seen" in result.stdout


def test_cli_strict_exit_codes(tmp_path):
    dirty = write_relative(tmp_path, "dirty.json", [400])
    clean = write_relative(tmp_path, "clean.json", [1])
    assert runner.invoke(app, ["audit", "stale", "--from-file", str(dirty)]).exit_code == 0
    assert runner.invoke(
        app, ["audit", "stale", "--from-file", str(dirty), "--strict"]
    ).exit_code == 1
    assert runner.invoke(
        app, ["audit", "stale", "--from-file", str(clean), "--strict"]
    ).exit_code == 0

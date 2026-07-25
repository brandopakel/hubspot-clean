"""Tests for audits/incomplete.py."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hubspot_crm_clean.audits.incomplete import (
    DEFAULT_REQUIRED_FIELDS,
    completeness_score,
    find_incomplete,
    is_blank,
    missing_fields,
)
from hubspot_crm_clean.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "contacts.json"
runner = CliRunner()


def contact(contact_id, **props):
    """Build a contact, defaulting every required field to present."""
    filled = {"email": "a@x.com", "company": "Acme", "lifecyclestage": "lead", "phone": "555"}
    filled.update(props)
    return {"id": contact_id, "properties": filled}


# --------------------------------------------------------------------------
# is_blank
# --------------------------------------------------------------------------

@pytest.mark.parametrize("value", [
    None,       # HubSpot's null for an unset property
    "",         # a field someone cleared
    "   ",      # whitespace left by a paste
    "\t\n",
])
def test_blank_values(value):
    assert is_blank(value) is True


@pytest.mark.parametrize("value", ["a", "0", " x ", 0, False, 123])
def test_non_blank_values(value):
    # 0 and False are real values, not missing data
    assert is_blank(value) is False


# --------------------------------------------------------------------------
# missing_fields / completeness_score
# --------------------------------------------------------------------------

def test_nothing_missing():
    assert missing_fields(contact("1")) == []
    assert completeness_score(contact("1")) == 100


def test_missing_reported_in_the_order_given():
    item = contact("1", email=None, phone="")
    assert missing_fields(item) == ["email", "phone"]


def test_score_is_a_percentage_of_required_fields():
    assert completeness_score(contact("1", email=None)) == 75          # 3 of 4
    assert completeness_score(contact("1", email=None, phone=None)) == 50
    assert completeness_score(
        contact("1", email=None, phone=None, company=None, lifecyclestage=None)
    ) == 0


def test_custom_required_fields():
    item = contact("1", email=None)
    assert missing_fields(item, ["company"]) == []      # not asked about email
    assert completeness_score(item, ["company"]) == 100
    assert completeness_score(item, ["email", "company"]) == 50


def test_empty_required_list_means_nothing_to_fail():
    assert completeness_score(contact("1", email=None), []) == 100


# --------------------------------------------------------------------------
# find_incomplete
# --------------------------------------------------------------------------

def test_complete_contacts_are_not_flagged():
    assert find_incomplete([contact("1")]) == []


def test_threshold_is_exclusive():
    # a contact landing exactly on the bar passes
    item = contact("1", email=None)     # scores 75
    assert find_incomplete([item], min_completeness=75) == []
    assert len(find_incomplete([item], min_completeness=76)) == 1


def test_worst_first():
    flagged = find_incomplete([
        contact("1", email=None, phone=None),                                 # 50
        contact("2", email=None, phone=None, company=None, lifecyclestage=None),  # 0
        contact("3", email=None, phone=None, company=None),                   # 25
    ])
    assert [item.contact["id"] for item in flagged] == ["2", "3", "1"]
    assert [item.score for item in flagged] == [0, 25, 50]


def test_result_carries_contact_score_and_missing():
    (item,) = find_incomplete([contact("9", email=None, phone=None)])
    assert item.contact["id"] == "9"
    assert item.score == 50
    assert item.missing == ["email", "phone"]


def test_whitespace_only_counts_as_missing():
    (item,) = find_incomplete([contact("1", company="   ", phone="")])
    assert item.missing == ["company", "phone"]


def test_against_the_real_fixture():
    contacts = json.load(FIXTURE.open(encoding="utf-8"))["results"]
    flagged = find_incomplete(contacts)
    assert [item.contact["id"] for item in flagged] == ["3"]
    assert flagged[0].missing == ["company", "lifecyclestage"]
    assert flagged[0].score == 50


def test_defaults_match_the_documented_config():
    # config.example.yaml declares these under rules.incomplete.required_fields
    assert DEFAULT_REQUIRED_FIELDS == ["email", "company", "lifecyclestage", "phone"]


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_flags_the_fixture():
    result = runner.invoke(app, ["audit", "incomplete", "--from-file", str(FIXTURE)])
    assert result.exit_code == 0
    assert "1 incomplete" in result.stdout
    assert "Bob Smith" in result.stdout


def test_cli_reports_clean(tmp_path):
    path = tmp_path / "clean.json"
    path.write_text(json.dumps({"results": [contact("1")]}))
    result = runner.invoke(app, ["audit", "incomplete", "--from-file", str(path)])
    assert result.exit_code == 0
    assert "meet the completeness bar" in result.stdout


def test_cli_min_completeness_option(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"results": [contact("1", email=None)]}))   # scores 75
    args = ["audit", "incomplete", "--from-file", str(path)]
    assert "meet the completeness bar" in runner.invoke(app, args).stdout
    assert "1 incomplete" in runner.invoke(app, args + ["-m", "100"]).stdout


def test_cli_required_option_is_repeatable(tmp_path):
    path = tmp_path / "c.json"
    path.write_text(json.dumps({"results": [contact("1", email=None, company=None)]}))
    result = runner.invoke(
        app, ["audit", "incomplete", "--from-file", str(path), "-r", "email", "-r", "phone"]
    )
    assert result.exit_code == 0
    assert "required: email, phone" in result.stdout


def test_cli_strict_exit_codes(tmp_path):
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps({"results": [contact("1")]}))
    dirty = ["audit", "incomplete", "--from-file", str(FIXTURE)]
    assert runner.invoke(app, dirty).exit_code == 0
    assert runner.invoke(app, dirty + ["--strict"]).exit_code == 1
    assert runner.invoke(
        app, ["audit", "incomplete", "--from-file", str(clean), "--strict"]
    ).exit_code == 0

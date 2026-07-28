"""Tests for config.py, plus the CLI precedence rules it feeds."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

from hubspot_crm_clean.cli import app
from hubspot_crm_clean.config import (
    DEFAULT_CONFIG_NAME,
    DEFAULTS,
    Config,
    ConfigError,
    find_config,
    load_config,
    parse_config,
)
from hubspot_crm_clean.reports import ReportFormat

EXAMPLE = Path(__file__).parent.parent / "config.example.yaml"
runner = CliRunner()


def write_config(path, body):
    """Write a config dict out as YAML and return the path."""
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# parse_config - defaults and overrides
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [None, {}, {"rules": None}, {"rules": {}}])
def test_an_empty_config_changes_nothing(raw):
    # a config file is allowed to exist and say nothing
    assert parse_config(raw) == DEFAULTS


def test_overrides_only_what_it_names():
    config = parse_config({"rules": {"duplicates": {"match_threshold": 92}}})
    assert config.threshold == 92
    assert config.min_completeness == DEFAULTS.min_completeness
    assert config.required_fields == DEFAULTS.required_fields


def test_overrides_every_setting():
    config = parse_config({"rules": {
        "duplicates": {"match_threshold": 70},
        "incomplete": {"required_fields": ["email"], "min_completeness": 100},
        "stale": {"inactive_days": 365, "activity_fields": ["notes_last_updated"]},
    }})
    assert config == Config(
        threshold=70,
        required_fields=["email"],
        min_completeness=100,
        inactive_days=365,
        activity_fields=["notes_last_updated"],
    )


def test_a_section_present_but_blank_is_ignored():
    # `duplicates:` with nothing under it parses to None, not a mapping
    assert parse_config({"rules": {"duplicates": None}}) == DEFAULTS


def test_an_explicitly_blank_value_keeps_the_default():
    assert parse_config({"rules": {"stale": {"inactive_days": None}}}) == DEFAULTS


def test_zero_is_a_setting_not_an_omission():
    # `or` would swap these for the defaults; the loader must use `is None`
    config = parse_config({"rules": {
        "incomplete": {"min_completeness": 0},
        "stale": {"inactive_days": 0},
        "duplicates": {"match_threshold": 0},
    }})
    assert (config.min_completeness, config.inactive_days, config.threshold) == (0, 0, 0)


def test_reports_sits_at_the_root_not_under_rules():
    # it's about output, not about what counts as a finding
    config = parse_config({"reports": {"default_format": "csv"}})
    assert config.default_format is ReportFormat.CSV
    assert config.threshold == DEFAULTS.threshold        # nothing else moved


def test_default_format_is_unset_unless_asked_for():
    # there's no sensible built-in: guessing csv over json would be picking a
    # data shape on the user's behalf
    assert parse_config(None).default_format is None


def test_default_format_is_case_insensitive_and_stripped():
    assert parse_config({"reports": {"default_format": " JSON "}}).default_format \
        is ReportFormat.JSON


@pytest.mark.parametrize("value", ["xml", "", 42, True, ["json"]])
def test_an_unsupported_default_format_is_rejected(value):
    with pytest.raises(ConfigError, match=r"reports\.default_format"):
        parse_config({"reports": {"default_format": value}})


def test_an_empty_field_list_means_check_nothing():
    config = parse_config({"rules": {"incomplete": {"required_fields": []}}})
    assert config.required_fields == []      # not the defaults


def test_field_names_are_stripped():
    config = parse_config({"rules": {"incomplete": {"required_fields": [" email ", "phone"]}}})
    assert config.required_fields == ["email", "phone"]


def test_field_lists_are_copies_not_shared_module_state():
    # a caller mutating what it got back must not reach into the audit modules'
    # defaults, or the shared DEFAULTS every other run starts from
    from hubspot_crm_clean.audits.incomplete import DEFAULT_REQUIRED_FIELDS

    assert DEFAULTS.required_fields is not DEFAULT_REQUIRED_FIELDS
    config = parse_config(None)
    assert config.required_fields is not DEFAULTS.required_fields
    assert config.activity_fields is not DEFAULTS.activity_fields


# --------------------------------------------------------------------------
# parse_config - rejection
# --------------------------------------------------------------------------

def test_unknown_top_level_key_is_rejected():
    # `hubspot.object_types` is on the roadmap but not honoured yet, so a config
    # asking for it has to fail rather than read as though it took effect
    with pytest.raises(ConfigError, match="hubspot"):
        parse_config({"rules": {}, "hubspot": {"object_types": ["companies"]}})


def test_unknown_reports_key_is_rejected():
    with pytest.raises(ConfigError) as err:
        parse_config({"reports": {"defualt_format": "json"}})
    assert "defualt_format" in str(err.value)
    assert "default_format" in str(err.value)        # the key they meant


def test_unknown_rules_section_is_rejected():
    with pytest.raises(ConfigError, match="companies"):
        parse_config({"rules": {"companies": {}}})


def test_a_typo_fails_loudly_and_names_the_valid_keys():
    # the whole point of strict validation: `min_completness: 90` silently doing
    # nothing is worse than no config at all
    with pytest.raises(ConfigError) as err:
        parse_config({"rules": {"incomplete": {"min_completness": 90}}})
    assert "min_completness" in str(err.value)
    assert "min_completeness" in str(err.value)      # the key they meant


def test_error_names_the_offending_path():
    with pytest.raises(ConfigError, match=r"rules\.duplicates\.match_threshold"):
        parse_config({"rules": {"duplicates": {"match_threshold": 101}}})


@pytest.mark.parametrize("raw", ["a string", ["a", "list"], 42])
def test_root_must_be_a_mapping(raw):
    with pytest.raises(ConfigError, match="mapping"):
        parse_config(raw)


@pytest.mark.parametrize("raw", ["a string", ["a", "list"], 42])
def test_rules_must_be_a_mapping(raw):
    with pytest.raises(ConfigError, match="mapping"):
        parse_config({"rules": raw})


def test_section_must_be_a_mapping():
    with pytest.raises(ConfigError, match="mapping"):
        parse_config({"rules": {"stale": ["inactive_days"]}})


@pytest.mark.parametrize("value", [101, -1])
def test_percentages_are_range_checked(value):
    with pytest.raises(ConfigError, match="between 0 and 100"):
        parse_config({"rules": {"duplicates": {"match_threshold": value}}})


def test_inactive_days_cannot_be_negative():
    with pytest.raises(ConfigError, match="0 or more"):
        parse_config({"rules": {"stale": {"inactive_days": -1}}})


@pytest.mark.parametrize("value", ["90", 90.5, [], {"a": 1}])
def test_non_integers_are_rejected(value):
    with pytest.raises(ConfigError, match="whole number"):
        parse_config({"rules": {"stale": {"inactive_days": value}}})


def test_a_boolean_is_not_a_number():
    # bool subclasses int, so `inactive_days: true` would otherwise pass as 1
    with pytest.raises(ConfigError, match="whole number"):
        parse_config({"rules": {"stale": {"inactive_days": True}}})


@pytest.mark.parametrize("value", ["email", 42, {"email": True}])
def test_field_lists_must_be_lists(value):
    with pytest.raises(ConfigError, match="list of field names"):
        parse_config({"rules": {"incomplete": {"required_fields": value}}})


@pytest.mark.parametrize("item", ["", "   ", None, 7, ["nested"]])
def test_field_names_must_be_non_empty_strings(item):
    with pytest.raises(ConfigError, match="non-empty field name"):
        parse_config({"rules": {"incomplete": {"required_fields": ["email", item]}}})


def test_repeated_field_names_are_rejected():
    # completeness_score divides by the list length, so a repeat skews every score
    with pytest.raises(ConfigError, match="repeats the field"):
        parse_config({"rules": {"incomplete": {"required_fields": ["email", "email"]}}})


def test_repeated_field_names_are_caught_after_stripping():
    with pytest.raises(ConfigError, match="repeats the field"):
        parse_config({"rules": {"incomplete": {"required_fields": ["email", " email "]}}})


def test_non_string_keys_do_not_crash_the_unknown_key_check():
    # YAML happily parses `90: x` into an int key
    with pytest.raises(ConfigError, match="unknown key"):
        parse_config({"rules": {"incomplete": {90: "x"}}})


# --------------------------------------------------------------------------
# load_config
# --------------------------------------------------------------------------

def test_loads_a_file(tmp_path):
    path = write_config(tmp_path / "c.yaml", {"rules": {"stale": {"inactive_days": 30}}})
    assert load_config(path).inactive_days == 30


def test_an_empty_file_is_a_valid_config(tmp_path):
    path = tmp_path / "empty.yaml"
    path.write_text("", encoding="utf-8")
    assert load_config(path) == DEFAULTS


def test_a_comments_only_file_is_a_valid_config(tmp_path):
    path = tmp_path / "comments.yaml"
    path.write_text("# nothing to see here\n", encoding="utf-8")
    assert load_config(path) == DEFAULTS


def test_missing_file_raises_config_error_not_oserror(tmp_path):
    with pytest.raises(ConfigError, match="could not read"):
        load_config(tmp_path / "nope.yaml")


def test_malformed_yaml_raises_config_error(tmp_path):
    path = tmp_path / "bad.yaml"
    path.write_text("rules: [unclosed\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_config(path)


def test_validation_errors_name_the_file(tmp_path):
    path = write_config(tmp_path / "c.yaml", {"rules": {"duplicates": {"match_threshold": 500}}})
    with pytest.raises(ConfigError) as err:
        load_config(path)
    assert "c.yaml" in str(err.value)
    assert "match_threshold" in str(err.value)


def test_the_shipped_example_loads_and_matches_the_built_in_defaults():
    # config.example.yaml is documentation; if it drifts from the defaults, or
    # picks up a key the loader rejects, this catches it before a user does
    assert load_config(EXAMPLE) == DEFAULTS


# --------------------------------------------------------------------------
# find_config
# --------------------------------------------------------------------------

def test_explicit_path_wins(tmp_path):
    assert find_config(tmp_path / "given.yaml") == tmp_path / "given.yaml"


def test_falls_back_to_config_yaml_in_the_working_directory(isolated_cwd):
    write_config(isolated_cwd / DEFAULT_CONFIG_NAME, {"rules": {}})
    assert find_config(None) == Path(DEFAULT_CONFIG_NAME)


def test_no_config_anywhere_returns_none(isolated_cwd):
    assert find_config(None) is None


# --------------------------------------------------------------------------
# CLI precedence: flag > config file > default
# --------------------------------------------------------------------------

# Two contacts calibrated so every default sits between the two outcomes, which
# is what lets a single fixture prove each knob was actually read:
#   names   "Jane Doe" vs "Janet Doe" score 94 - matched at 85, missed at 95
#   fields  contact 2 is missing only phone -> 75%, exactly on the default bar
#   dates   both were last active 100 days ago - stale at 90, fresh at 365
@pytest.fixture
def contacts_file(tmp_path):
    stamp = (datetime.now(UTC) - timedelta(days=100)).isoformat().replace("+00:00", "Z")
    results = [
        {"id": "1", "properties": {"email": "jane.doe@acme.com", "firstname": "Jane",
                                   "lastname": "Doe", "company": "Acme", "phone": "555-0100",
                                   "lifecyclestage": "customer",
                                   "hs_last_activity_date": stamp, "lastmodifieddate": stamp}},
        {"id": "2", "properties": {"email": "j.doe@acme.com", "firstname": "Janet",
                                   "lastname": "Doe", "company": "Acme", "phone": None,
                                   "lifecyclestage": "customer",
                                   "hs_last_activity_date": stamp, "lastmodifieddate": stamp}},
    ]
    path = tmp_path / "contacts.json"
    path.write_text(json.dumps({"results": results}), encoding="utf-8")
    return path


def test_config_supplies_the_default(tmp_path, contacts_file):
    # the pair scores 94, so raising the bar to 95 is what decides the outcome
    config = write_config(tmp_path / "c.yaml", {"rules": {"duplicates": {"match_threshold": 95}}})
    result = runner.invoke(app, [
        "audit", "duplicates", "--from-file", str(contacts_file), "--config", str(config),
    ])
    assert result.exit_code == 0
    assert "No duplicates found" in result.stdout


def test_a_flag_beats_the_config(tmp_path, contacts_file):
    config = write_config(tmp_path / "c.yaml", {"rules": {"duplicates": {"match_threshold": 95}}})
    result = runner.invoke(app, [
        "audit", "duplicates", "--from-file", str(contacts_file),
        "--config", str(config), "-t", "85",
    ])
    assert "1 cluster(s)" in result.stdout


def test_config_yaml_is_picked_up_from_the_working_directory(isolated_cwd, contacts_file):
    write_config(isolated_cwd / DEFAULT_CONFIG_NAME,
                 {"rules": {"duplicates": {"match_threshold": 95}}})
    result = runner.invoke(app, ["audit", "duplicates", "--from-file", str(contacts_file)])
    assert "No duplicates found" in result.stdout


def test_the_config_in_use_is_echoed(isolated_cwd, contacts_file):
    # a file that silently retunes the audit would be worse than no file at all
    write_config(isolated_cwd / DEFAULT_CONFIG_NAME, {"rules": {}})
    result = runner.invoke(app, ["audit", "duplicates", "--from-file", str(contacts_file)])
    assert DEFAULT_CONFIG_NAME in result.stdout


def test_no_config_means_no_echo(isolated_cwd, contacts_file):
    result = runner.invoke(app, ["audit", "duplicates", "--from-file", str(contacts_file)])
    assert "Using config" not in result.stdout


def test_a_bad_config_exits_one_with_a_message_not_a_traceback(tmp_path, contacts_file):
    config = write_config(tmp_path / "c.yaml", {"rules": {"incomplete": {"min_completness": 90}}})
    result = runner.invoke(app, [
        "audit", "incomplete", "--from-file", str(contacts_file), "--config", str(config),
    ])
    assert result.exit_code == 1
    assert "Bad config" in result.stdout
    assert "min_completness" in result.stdout


def test_a_missing_explicit_config_is_rejected_by_the_cli(contacts_file):
    result = runner.invoke(app, [
        "audit", "duplicates", "--from-file", str(contacts_file), "--config", "nope.yaml",
    ])
    assert result.exit_code != 0


@pytest.mark.parametrize("command,body,expected", [
    ("duplicates", {"duplicates": {"match_threshold": 95}}, "No duplicates found"),
    ("incomplete", {"incomplete": {"min_completeness": 100}}, "1 incomplete"),
    ("stale", {"stale": {"inactive_days": 365}}, "show recent activity"),
])
def test_every_audit_reads_the_config(tmp_path, contacts_file, command, body, expected):
    config = write_config(tmp_path / "c.yaml", {"rules": body})
    result = runner.invoke(app, [
        "audit", command, "--from-file", str(contacts_file), "--config", str(config),
    ])
    assert expected in result.stdout


def test_config_can_set_required_fields(tmp_path, contacts_file):
    # only `website` is required, and neither contact has it -> both score 0%
    config = write_config(tmp_path / "c.yaml",
                          {"rules": {"incomplete": {"required_fields": ["website"]}}})
    result = runner.invoke(app, [
        "audit", "incomplete", "--from-file", str(contacts_file), "--config", str(config),
    ])
    assert "2 incomplete" in result.stdout


def test_config_can_set_activity_fields(tmp_path, contacts_file):
    # neither contact carries this field, so both read as never seen
    config = write_config(tmp_path / "c.yaml",
                          {"rules": {"stale": {"activity_fields": ["notes_last_updated"]}}})
    result = runner.invoke(app, [
        "audit", "stale", "--from-file", str(contacts_file), "--config", str(config),
    ])
    assert "2 stale" in result.stdout
    assert "2 never seen" in result.stdout


def test_activity_field_flag_beats_the_config(tmp_path, contacts_file):
    # the config points at a field nobody has (never seen); the flag points at one
    # everybody has (stale, but dated) - so the wording tells us which one applied
    config = write_config(tmp_path / "c.yaml",
                          {"rules": {"stale": {"activity_fields": ["notes_last_updated"]}}})
    result = runner.invoke(app, [
        "audit", "stale", "--from-file", str(contacts_file), "--config", str(config),
        "--activity-field", "hs_last_activity_date",
    ])
    assert "2 stale" in result.stdout
    assert "never seen" not in result.stdout


def test_help_advertises_the_real_defaults():
    # the options default to None so the config can fill them in; --help must not
    # tell the user the default threshold is `None`
    result = runner.invoke(app, ["audit", "duplicates", "--help"])
    assert "85" in result.stdout
    assert "None" not in result.stdout

"""Tests for merges.py and the `merge` command.

Nothing here reaches HubSpot. apply_plans takes the write as a callable, so the
tests hand it a fake that records calls or raises on demand — which is the same
seam the dry run uses to do nothing at all.
"""

import json

import pytest
from typer.testing import CliRunner

from hubspot_crm_clean.audits.duplicates import find_duplicates
from hubspot_crm_clean.cli import app
from hubspot_crm_clean.merges import (
    LOWEST_ID,
    MOST_COMPLETE,
    OLDEST,
    UNKNOWN_DATE,
    apply_plans,
    choose_primary,
    created,
    failure_count,
    id_key,
    merge_count,
    plan_merges,
)

runner = CliRunner()


def contact(contact_id, email=None, first="Jane", last="Doe", created_at=None, **props):
    """A contact shaped like client.normalize_contacts() returns."""
    return {
        "id": contact_id,
        "properties": {
            "email": email or f"{contact_id}@acme.com",
            "firstname": first,
            "lastname": last,
            "createdate": created_at,
            **props,
        },
    }


def full(contact_id, **kwargs):
    """A contact that fills every default required field."""
    return contact(contact_id, company="Acme", phone="555-0100",
                   lifecyclestage="customer", **kwargs)


def write(tmp_path, name, contacts):
    path = tmp_path / name
    path.write_text(json.dumps({"results": contacts}), encoding="utf-8")
    return path


class Recorder:
    """A stand-in for client.merge_contacts."""

    def __init__(self, fail_on=()):
        self.calls = []
        self.fail_on = set(fail_on)

    def __call__(self, primary_id, merge_id):
        self.calls.append((primary_id, merge_id))
        if merge_id in self.fail_on:
            raise RuntimeError(f"boom {merge_id}")


# --------------------------------------------------------------------------
# created / id_key
# --------------------------------------------------------------------------

def test_a_missing_createdate_sorts_last():
    # otherwise a record with no date would win every "oldest" tie-break
    assert created(contact("1")) == UNKNOWN_DATE
    assert created(contact("2", created_at="2020-01-01T00:00:00Z")) < UNKNOWN_DATE


def test_an_unreadable_createdate_is_treated_as_missing():
    assert created(contact("1", created_at="15 January 2025")) == UNKNOWN_DATE


def test_numeric_ids_sort_numerically_not_as_text():
    # "10" < "9" as text, which would make the tie-break look arbitrary
    assert id_key(contact("9")) < id_key(contact("10"))


def test_non_numeric_ids_sort_after_numeric_ones_without_raising():
    assert id_key(contact("100")) < id_key(contact("abc"))
    assert min([contact("b"), contact("2"), contact("a")], key=id_key)["id"] == "2"


# --------------------------------------------------------------------------
# choose_primary
# --------------------------------------------------------------------------

def test_the_most_complete_record_survives():
    # HubSpot keeps the primary's value on conflict, so the fullest record loses least
    primary, reason = choose_primary([contact("1"), full("2")])
    assert (primary["id"], reason) == ("2", MOST_COMPLETE)


def test_completeness_beats_age():
    # a sparse record being older is not a reason to throw away a full one
    primary, reason = choose_primary([
        contact("1", created_at="2001-01-01T00:00:00Z"),
        full("2", created_at="2020-01-01T00:00:00Z"),
    ])
    assert (primary["id"], reason) == ("2", MOST_COMPLETE)


def test_equal_completeness_goes_to_the_oldest():
    primary, reason = choose_primary([
        full("1", created_at="2020-01-01T00:00:00Z"),
        full("2", created_at="2015-01-01T00:00:00Z"),
    ])
    assert (primary["id"], reason) == ("2", OLDEST)


def test_equal_completeness_and_age_goes_to_the_lowest_id():
    stamp = "2020-01-01T00:00:00Z"
    primary, reason = choose_primary([full("10", created_at=stamp), full("9", created_at=stamp)])
    assert (primary["id"], reason) == ("9", LOWEST_ID)


def test_no_dates_at_all_falls_through_to_the_id():
    primary, reason = choose_primary([full("10"), full("9")])
    assert (primary["id"], reason) == ("9", LOWEST_ID)


def test_the_choice_does_not_depend_on_input_order():
    members = [full("10", created_at="2020-01-01T00:00:00Z"),
               full("9", created_at="2015-01-01T00:00:00Z")]
    assert choose_primary(members)[0]["id"] == choose_primary(members[::-1])[0]["id"]


def test_required_fields_drive_completeness():
    # `website` is the only requirement and only one record has it
    members = [contact("1"), contact("2", website="acme.com")]
    assert choose_primary(members, ["website"])[0]["id"] == "2"


# --------------------------------------------------------------------------
# plan_merges
# --------------------------------------------------------------------------

@pytest.fixture
def clusters():
    return find_duplicates([
        full("1", email="jane.doe@acme.com"),
        contact("2", email="j.doe@acme.com"),
        full("3", email="bob@beta.com", first="Bob", last="Smith"),
        full("4", email="b.smith@beta.com", first="Bob", last="Smith"),
    ])


def test_a_plan_keeps_one_and_absorbs_the_rest(clusters):
    plans = plan_merges(clusters)
    by_primary = {plan.primary["id"]: [c["id"] for c in plan.absorbed] for plan in plans}
    assert by_primary["1"] == ["2"]     # 1 is complete, 2 isn't


def test_a_cluster_of_three_is_one_plan_but_two_merges():
    clusters = find_duplicates([
        full("1", email="a@acme.com"),
        contact("2", email="b@acme.com"),
        contact("3", email="c@acme.com"),
    ])
    plans = plan_merges(clusters)
    assert len(plans) == 1
    assert len(plans[0].absorbed) == 2
    assert merge_count(plans) == 2


def test_limit_truncates_the_plan_itself(clusters):
    # ordered by confidence, so a limited run acts on the surest clusters
    assert len(plan_merges(clusters, limit=1)) == 1
    assert plan_merges(clusters, limit=None) == plan_merges(clusters)


def test_limit_larger_than_the_plan_is_harmless(clusters):
    assert len(plan_merges(clusters, limit=99)) == len(clusters)


def test_the_cluster_confidence_travels_with_the_plan(clusters):
    assert all(plan.confidence > 0 for plan in plan_merges(clusters))


def test_no_clusters_means_no_plans():
    assert plan_merges([]) == []


# --------------------------------------------------------------------------
# apply_plans
# --------------------------------------------------------------------------

def test_every_absorbed_record_is_merged_into_the_primary(clusters):
    plans = plan_merges(clusters)
    recorder = Recorder()
    outcomes = apply_plans(plans, recorder)
    assert len(recorder.calls) == merge_count(plans)
    assert all(primary != absorbed for primary, absorbed in recorder.calls)
    assert failure_count(outcomes) == 0


def test_a_failure_is_recorded_and_the_run_continues():
    # stopping halfway would leave the portal in a state nobody planned
    clusters = find_duplicates([
        full("1", email="a@acme.com"),
        contact("2", email="b@acme.com"),
        contact("3", email="c@acme.com"),
    ])
    plans = plan_merges(clusters)
    recorder = Recorder(fail_on={"2"})
    outcomes = apply_plans(plans, recorder)
    assert len(recorder.calls) == 2          # did not abort after the failure
    assert outcomes[0].merged == ["3"]
    assert outcomes[0].failures == [("2", "boom 2")]
    assert failure_count(outcomes) == 1


def test_progress_fires_once_per_record(clusters):
    seen = []
    plans = plan_merges(clusters)
    apply_plans(plans, Recorder(), on_progress=seen.append)
    assert sum(seen) == merge_count(plans)


def test_nothing_is_called_for_an_empty_plan():
    recorder = Recorder()
    assert apply_plans([], recorder) == []
    assert recorder.calls == []


# --------------------------------------------------------------------------
# CLI - dry run
# --------------------------------------------------------------------------

@pytest.fixture
def dupes(tmp_path):
    return write(tmp_path, "d.json", [
        full("1", email="jane.doe@acme.com", created_at="2019-01-01T00:00:00Z"),
        contact("2", email="j.doe@acme.com", created_at="2017-01-01T00:00:00Z"),
    ])


def test_dry_run_is_the_default(dupes):
    result = runner.invoke(app, ["merge", "--from-file", str(dupes)])
    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    assert "nothing was written" in result.stdout
    assert "would merge" in result.stdout
    assert "--apply" in result.stdout          # tells you how to commit


def test_the_dry_run_names_the_survivor_and_the_rule(dupes):
    result = runner.invoke(app, ["merge", "--from-file", str(dupes)])
    assert "keep" in result.stdout
    assert MOST_COMPLETE in result.stdout


def test_nothing_to_merge_is_reported_cleanly(tmp_path):
    clean = write(tmp_path, "c.json", [full("1", email="ann@acme.com", first="Ann", last="Lee")])
    result = runner.invoke(app, ["merge", "--from-file", str(clean)])
    assert result.exit_code == 0
    assert "Nothing to merge" in result.stdout


def test_the_threshold_flag_applies(dupes):
    assert "DRY RUN" in runner.invoke(app, ["merge", "--from-file", str(dupes)]).stdout
    high = runner.invoke(app, ["merge", "--from-file", str(dupes), "-t", "100"])
    assert "DRY RUN" in high.stdout          # these two score 100, so still matched


def test_limit_is_reported_when_it_drops_clusters(tmp_path):
    path = write(tmp_path, "many.json", [
        full("1", email="a@acme.com"),
        contact("2", email="b@acme.com"),
        full("3", email="c@beta.com", first="Bob", last="Smith"),
        full("4", email="d@beta.com", first="Bob", last="Smith"),
    ])
    result = runner.invoke(app, ["merge", "--from-file", str(path), "--limit", "1"])
    assert "acting on 1 of 2 cluster(s)" in result.stdout


# --------------------------------------------------------------------------
# CLI - apply
# --------------------------------------------------------------------------

def test_apply_from_a_file_is_refused(dupes):
    # a dump's ids were true when it was written; the portal has moved on
    result = runner.invoke(app, ["merge", "--from-file", str(dupes), "--apply", "--yes"])
    assert result.exit_code == 1
    assert "Refusing to apply from a file" in result.stdout


def test_apply_writes_and_reports_what_it_did(monkeypatch, dupes):
    recorder = Recorder()
    monkeypatch.setattr("hubspot_crm_clean.cli.merge_contacts", recorder)
    monkeypatch.setattr("hubspot_crm_clean.cli.resolve_contacts",
                        lambda from_file, properties: json.loads(
                            dupes.read_text(encoding="utf-8"))["results"])
    result = runner.invoke(app, ["merge", "--apply", "--yes"])
    assert result.exit_code == 0
    assert recorder.calls == [("1", "2")]      # complete record survives
    assert "1 merged" in result.stdout
    assert "DRY RUN" not in result.stdout


def test_apply_prompts_before_writing(monkeypatch, dupes):
    recorder = Recorder()
    monkeypatch.setattr("hubspot_crm_clean.cli.merge_contacts", recorder)
    monkeypatch.setattr("hubspot_crm_clean.cli.resolve_contacts",
                        lambda from_file, properties: json.loads(
                            dupes.read_text(encoding="utf-8"))["results"])
    result = runner.invoke(app, ["merge", "--apply"], input="n\n")
    assert result.exit_code == 1
    assert recorder.calls == []                # declined, so nothing was written
    assert "cannot be undone" in result.stdout
    assert "Cancelled" in result.stdout


def test_answering_yes_at_the_prompt_writes(monkeypatch, dupes):
    recorder = Recorder()
    monkeypatch.setattr("hubspot_crm_clean.cli.merge_contacts", recorder)
    monkeypatch.setattr("hubspot_crm_clean.cli.resolve_contacts",
                        lambda from_file, properties: json.loads(
                            dupes.read_text(encoding="utf-8"))["results"])
    result = runner.invoke(app, ["merge", "--apply"], input="y\n")
    assert result.exit_code == 0
    assert recorder.calls == [("1", "2")]


def test_a_failed_merge_exits_one_and_says_which(monkeypatch, dupes):
    monkeypatch.setattr("hubspot_crm_clean.cli.merge_contacts", Recorder(fail_on={"2"}))
    monkeypatch.setattr("hubspot_crm_clean.cli.resolve_contacts",
                        lambda from_file, properties: json.loads(
                            dupes.read_text(encoding="utf-8"))["results"])
    result = runner.invoke(app, ["merge", "--apply", "--yes"])
    assert result.exit_code == 1
    assert "failed" in result.stdout
    assert "1 failed" in result.stdout


# --------------------------------------------------------------------------
# CLI - report export
# --------------------------------------------------------------------------

def test_a_dry_run_report_records_that_nothing_was_applied(tmp_path, dupes):
    out = tmp_path / "plan.json"
    runner.invoke(app, ["merge", "--from-file", str(dupes), "-o", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["merges"]["applied"] is False
    statuses = {row["status"] for row in payload["merges"]["findings"]}
    assert statuses == {"kept", "planned"}


def test_the_report_names_what_each_record_was_merged_into(tmp_path, dupes):
    # a file listing only what was removed can't say what it was removed into
    out = tmp_path / "plan.json"
    runner.invoke(app, ["merge", "--from-file", str(dupes), "-o", str(out)])
    rows = json.loads(out.read_text(encoding="utf-8"))["merges"]["findings"]
    kept = [row for row in rows if row["role"] == "kept"]
    assert len(kept) == 1
    assert kept[0]["id"] == "1"
    assert kept[0]["reason"] == MOST_COMPLETE


def test_an_applied_report_records_the_outcome(monkeypatch, tmp_path, dupes):
    monkeypatch.setattr("hubspot_crm_clean.cli.merge_contacts", Recorder())
    monkeypatch.setattr("hubspot_crm_clean.cli.resolve_contacts",
                        lambda from_file, properties: json.loads(
                            dupes.read_text(encoding="utf-8"))["results"])
    out = tmp_path / "done.json"
    runner.invoke(app, ["merge", "--apply", "--yes", "-o", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["merges"]["applied"] is True
    assert {row["status"] for row in payload["merges"]["findings"]} == {"kept", "merged"}

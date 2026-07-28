"""Tests for fixes.py and the `fix` commands.

Nothing here reaches HubSpot. apply_plans takes the write as a callable, so the
tests hand it a fake that records calls or raises on demand — which is the same
seam the dry run uses to do nothing at all.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest
from typer.testing import CliRunner

from hubspot_crm_clean.audits.duplicates import find_duplicates
from hubspot_crm_clean.audits.stale import find_stale
from hubspot_crm_clean.cli import app
from hubspot_crm_clean.fixes import (
    CHOSEN,
    LOWEST_ID,
    MOST_COMPLETE,
    OLDEST,
    UNKNOWN_DATE,
    ArchivePlan,
    apply_archives,
    apply_plans,
    archive_failures,
    choose_primary,
    created,
    failure_count,
    id_key,
    merge_count,
    plan_archives,
    plan_merges,
    repoint,
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
    result = runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes)])
    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    assert "nothing was written" in result.stdout
    assert "would merge" in result.stdout
    assert "--apply" in result.stdout          # tells you how to commit


def test_the_dry_run_names_the_survivor_and_the_rule(dupes):
    result = runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes)])
    assert "keep" in result.stdout
    assert MOST_COMPLETE in result.stdout


def test_nothing_to_merge_is_reported_cleanly(tmp_path):
    clean = write(tmp_path, "c.json", [full("1", email="ann@acme.com", first="Ann", last="Lee")])
    result = runner.invoke(app, ["fix", "duplicates", "--from-file", str(clean)])
    assert result.exit_code == 0
    assert "Nothing to merge" in result.stdout


def test_the_threshold_flag_applies(dupes):
    assert "DRY RUN" in runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes)]).stdout
    high = runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes), "-t", "100"])
    assert "DRY RUN" in high.stdout          # these two score 100, so still matched


def test_limit_is_reported_when_it_drops_clusters(tmp_path):
    path = write(tmp_path, "many.json", [
        full("1", email="a@acme.com"),
        contact("2", email="b@acme.com"),
        full("3", email="c@beta.com", first="Bob", last="Smith"),
        full("4", email="d@beta.com", first="Bob", last="Smith"),
    ])
    result = runner.invoke(app, ["fix", "duplicates", "--from-file", str(path), "--limit", "1"])
    assert "acting on 1 of 2 cluster(s)" in result.stdout


# --------------------------------------------------------------------------
# CLI - apply
# --------------------------------------------------------------------------

def test_apply_from_a_file_is_refused(dupes):
    # a dump's ids were true when it was written; the portal has moved on
    result = runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes), "--apply", "--yes"])
    assert result.exit_code == 1
    assert "Refusing to apply from a file" in result.stdout


def test_apply_writes_and_reports_what_it_did(monkeypatch, dupes):
    recorder = Recorder()
    monkeypatch.setattr("hubspot_crm_clean.cli.merge_contacts", recorder)
    monkeypatch.setattr("hubspot_crm_clean.cli.resolve_contacts",
                        lambda from_file, properties: json.loads(
                            dupes.read_text(encoding="utf-8"))["results"])
    result = runner.invoke(app, ["fix", "duplicates", "--apply", "--yes"])
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
    result = runner.invoke(app, ["fix", "duplicates", "--apply"], input="n\n")
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
    result = runner.invoke(app, ["fix", "duplicates", "--apply"], input="y\n")
    assert result.exit_code == 0
    assert recorder.calls == [("1", "2")]


def test_a_failed_merge_exits_one_and_says_which(monkeypatch, dupes):
    monkeypatch.setattr("hubspot_crm_clean.cli.merge_contacts", Recorder(fail_on={"2"}))
    monkeypatch.setattr("hubspot_crm_clean.cli.resolve_contacts",
                        lambda from_file, properties: json.loads(
                            dupes.read_text(encoding="utf-8"))["results"])
    result = runner.invoke(app, ["fix", "duplicates", "--apply", "--yes"])
    assert result.exit_code == 1
    assert "failed" in result.stdout
    assert "1 failed" in result.stdout


# --------------------------------------------------------------------------
# CLI - report export
# --------------------------------------------------------------------------

def test_a_dry_run_report_records_that_nothing_was_applied(tmp_path, dupes):
    out = tmp_path / "plan.json"
    runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes), "-o", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["merges"]["applied"] is False
    statuses = {row["status"] for row in payload["merges"]["findings"]}
    assert statuses == {"kept", "planned"}


def test_the_report_names_what_each_record_was_merged_into(tmp_path, dupes):
    # a file listing only what was removed can't say what it was removed into
    out = tmp_path / "plan.json"
    runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes), "-o", str(out)])
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
    runner.invoke(app, ["fix", "duplicates", "--apply", "--yes", "-o", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["merges"]["applied"] is True
    assert {row["status"] for row in payload["merges"]["findings"]} == {"kept", "merged"}


# --------------------------------------------------------------------------
# repoint / --interactive
# --------------------------------------------------------------------------

def test_repoint_swaps_the_survivor():
    plan = plan_merges(find_duplicates([
        full("1", email="a@acme.com"),
        contact("2", email="b@acme.com"),
    ]))[0]
    assert plan.primary["id"] == "1"
    moved = repoint(plan, "2")
    assert moved.primary["id"] == "2"
    assert [c["id"] for c in moved.absorbed] == ["1"]
    assert moved.reason == CHOSEN          # records that a human overrode the rules


def test_repoint_keeps_the_cluster_intact():
    plan = plan_merges(find_duplicates([
        full("1", email="a@acme.com"),
        contact("2", email="b@acme.com"),
        contact("3", email="c@acme.com"),
    ]))[0]
    moved = repoint(plan, "3")
    before = {plan.primary["id"], *[c["id"] for c in plan.absorbed]}
    after = {moved.primary["id"], *[c["id"] for c in moved.absorbed]}
    assert before == after == {"1", "2", "3"}
    assert (moved.domain, moved.confidence) == (plan.domain, plan.confidence)


def test_repoint_to_the_current_primary_leaves_membership_alone():
    plan = plan_merges(find_duplicates([
        full("1", email="a@acme.com"), contact("2", email="b@acme.com"),
    ]))[0]
    same = repoint(plan, "1")
    assert same.primary["id"] == "1"
    assert [c["id"] for c in same.absorbed] == ["2"]


def test_interactive_enter_accepts_the_suggestion(dupes):
    # pressing Enter through the whole review must reproduce the default run
    result = runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes), "-i"],
                           input="\n")
    assert result.exit_code == 0
    assert "suggested" in result.stdout
    assert "keep" in result.stdout


def test_interactive_can_pick_the_other_record(dupes):
    result = runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes), "-i"],
                           input="2\n")
    assert result.exit_code == 0
    assert CHOSEN in result.stdout          # the reason column records the override


def test_interactive_skip_drops_the_cluster(dupes):
    result = runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes), "-i"],
                           input="s\n")
    assert result.exit_code == 0
    assert "No clusters selected" in result.stdout


def test_interactive_quit_keeps_earlier_decisions(tmp_path):
    # abandoning a long review must not throw away what was already decided
    path = write(tmp_path, "two.json", [
        full("1", email="a@acme.com"), contact("2", email="b@acme.com"),
        full("3", email="c@beta.com", first="Bob", last="Smith"),
        full("4", email="d@beta.com", first="Bob", last="Smith"),
    ])
    result = runner.invoke(app, ["fix", "duplicates", "--from-file", str(path), "-i"],
                           input="1\nq\n")
    assert result.exit_code == 0
    assert "1 contact(s) would be merged" in result.stdout    # kept the first, dropped the rest


def test_interactive_reprompts_on_nonsense(dupes):
    result = runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes), "-i"],
                           input="banana\n9\n1\n")
    assert result.exit_code == 0
    assert "Enter a record number" in result.stdout


def test_interactive_and_yes_are_rejected(dupes):
    result = runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes), "-i", "-y"])
    assert result.exit_code == 1
    assert "contradict each other" in result.stdout


def test_interactive_cannot_stream_to_stdout(dupes):
    # typer.prompt writes to stdout, which would be carrying the report
    result = runner.invoke(app, ["fix", "duplicates", "--from-file", str(dupes),
                                 "-i", "-f", "json"])
    assert result.exit_code == 1
    assert "stream to stdout" in result.stdout


# --------------------------------------------------------------------------
# The `merge` alias
# --------------------------------------------------------------------------

def test_merge_still_works_as_a_hidden_alias(dupes):
    result = runner.invoke(app, ["merge", "--from-file", str(dupes)])
    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout


def test_the_alias_is_hidden_from_help():
    # only the command list matters - the app description mentions merging too
    commands = runner.invoke(app, ["--help"]).stdout.split("Commands")[-1]
    assert "merge" not in commands
    assert "fix" in commands


# --------------------------------------------------------------------------
# fix stale
# --------------------------------------------------------------------------

def stale_contact(contact_id, days_ago):
    stamp = None if days_ago is None else (
        datetime.now(UTC) - timedelta(days=days_ago)
    ).isoformat().replace("+00:00", "Z")
    item = contact(contact_id, email=f"{contact_id}@acme.com")
    item["properties"]["hs_last_activity_date"] = stamp
    item["properties"]["lastmodifieddate"] = stamp
    return item


@pytest.fixture
def stale_file(tmp_path):
    return write(tmp_path, "s.json", [
        stale_contact("1", 5), stale_contact("2", 400), stale_contact("3", 900),
    ])


def test_plan_archives_is_stalest_first_and_limits(stale_file):
    flagged = find_stale(json.loads(stale_file.read_text(encoding="utf-8"))["results"])
    plans = plan_archives(flagged)
    assert [p.contact["id"] for p in plans] == ["3", "2"]
    assert [p.contact["id"] for p in plan_archives(flagged, limit=1)] == ["3"]


def test_apply_archives_records_each_result():
    plans = [ArchivePlan(contact("1"), 400), ArchivePlan(contact("2"), 900)]
    calls = []

    def archive(contact_id):
        calls.append(contact_id)
        if contact_id == "2":
            raise RuntimeError("nope")

    outcomes = apply_archives(plans, archive)
    assert calls == ["1", "2"]                    # kept going after the failure
    assert [o.archived for o in outcomes] == [True, False]
    assert archive_failures(outcomes) == 1


def test_fix_stale_previews_by_default(stale_file):
    result = runner.invoke(app, ["fix", "stale", "--from-file", str(stale_file)])
    assert result.exit_code == 0
    assert "DRY RUN" in result.stdout
    assert "would archive" in result.stdout
    assert "--archive" in result.stdout


def test_fix_stale_reports_nothing_to_do(tmp_path):
    path = write(tmp_path, "fresh.json", [stale_contact("1", 2)])
    result = runner.invoke(app, ["fix", "stale", "--from-file", str(path)])
    assert "Nothing to archive" in result.stdout


def test_archive_flag_writes(monkeypatch, stale_file):
    calls = []
    monkeypatch.setattr("hubspot_crm_clean.cli.archive_contact", calls.append)
    result = runner.invoke(app, ["fix", "stale", "--from-file", str(stale_file),
                                 "--archive", "--yes"])
    assert result.exit_code == 0
    assert calls == ["3", "2"]
    assert "2 archived" in result.stdout


def test_apply_is_a_synonym_for_archive(monkeypatch, stale_file):
    calls = []
    monkeypatch.setattr("hubspot_crm_clean.cli.archive_contact", calls.append)
    runner.invoke(app, ["fix", "stale", "--from-file", str(stale_file), "--apply", "--yes"])
    assert calls == ["3", "2"]


def test_archiving_prompts_and_says_it_is_recoverable(monkeypatch, stale_file):
    calls = []
    monkeypatch.setattr("hubspot_crm_clean.cli.archive_contact", calls.append)
    result = runner.invoke(app, ["fix", "stale", "--from-file", str(stale_file), "--archive"],
                           input="n\n")
    assert result.exit_code == 1
    assert calls == []
    assert "recycle bin" in result.stdout      # a softer warning than merge's, on purpose
    assert "Cancelled" in result.stdout


def test_a_failed_archive_exits_one(monkeypatch, stale_file):
    def boom(contact_id):
        raise RuntimeError("denied")

    monkeypatch.setattr("hubspot_crm_clean.cli.archive_contact", boom)
    result = runner.invoke(app, ["fix", "stale", "--from-file", str(stale_file),
                                 "--archive", "--yes"])
    assert result.exit_code == 1
    assert "2 failed" in result.stdout


def test_fix_stale_limit_is_reported(stale_file):
    result = runner.invoke(app, ["fix", "stale", "--from-file", str(stale_file), "-n", "1"])
    assert "acting on 1 of 2" in result.stdout


def test_the_archive_report_records_what_happened(monkeypatch, tmp_path, stale_file):
    monkeypatch.setattr("hubspot_crm_clean.cli.archive_contact", lambda cid: None)
    out = tmp_path / "arch.json"
    runner.invoke(app, ["fix", "stale", "--from-file", str(stale_file),
                        "--archive", "--yes", "-o", str(out)])
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["archives"]["applied"] is True
    assert {row["status"] for row in payload["archives"]["findings"]} == {"archived"}

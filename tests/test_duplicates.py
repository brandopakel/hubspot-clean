"""Tests for audits/duplicates.py."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from hubspot_crm_clean.audits.duplicates import (
    NO_EMAIL,
    NO_NAME,
    email_groups,
    excluded_from_matching,
    find_duplicates,
    full_name,
    normalize_email,
    pair_count,
)
from hubspot_crm_clean.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "contacts.json"
runner = CliRunner()


def contact(contact_id, email=None, first=None, last=None):
    """Build a contact shaped like client.normalize_contacts() returns."""
    return {
        "id": contact_id,
        "properties": {"email": email, "firstname": first, "lastname": last},
    }


# --------------------------------------------------------------------------
# normalize_email
# --------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    None,                       # HubSpot sends null for unset properties
    "",
    "   ",
    "not-an-email",             # no @ at all
    "@acme.com",                # no local part
    "jane@",                    # no domain
    "jane@localhost",           # no TLD
    "a@b@acme.com",             # two @ - don't guess which is the separator
    "+tag@acme.com",            # local part is only a tag
    "Jane Doe jane@acme.com",   # display name with no brackets to extract from
    "<>",
])
def test_unusable_input_returns_none(raw):
    assert normalize_email(raw) is None


@pytest.mark.parametrize("raw,expected", [
    ("jane@acme.com", "jane@acme.com"),
    ("  JANE@ACME.COM  ", "jane@acme.com"),
    ("mailto:jane@acme.com", "jane@acme.com"),
    ("<jane@acme.com>", "jane@acme.com"),
    ("<mailto:jane@acme.com>", "jane@acme.com"),      # nested, brackets outside
    ("mailto:<jane@acme.com>", "jane@acme.com"),      # nested, the other order
    ("<<jane@acme.com>>", "jane@acme.com"),           # doubled brackets
    ("Jane Doe <jane@acme.com>", "jane@acme.com"),    # display name form
    ("jane @acme.com", "jane@acme.com"),         # NBSP from a paste
    ("jane@acme.com.", "jane@acme.com"),              # trailing dot (valid FQDN form)
    ("jane@.acme.com", "jane@acme.com"),              # leading dot
])
def test_cleanup(raw, expected):
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("raw", [
    "jane.doe@gmail.com",
    "janedoe+crm@gmail.com",
    "jane.doe+crm@gmail.com",
    "jane.doe@googlemail.com",      # alias domain
    "JANE.DOE@GMIAL.COM",           # typo domain
    "Jane Doe <j.a.n.e.doe+x@Gmail.Com>",
])
def test_gmail_variants_all_collapse_to_one_address(raw):
    assert normalize_email(raw) == "janedoe@gmail.com"


def test_dots_are_significant_outside_gmail():
    assert normalize_email("jane.doe@acme.com") == "jane.doe@acme.com"


def test_plus_tag_stripped_everywhere():
    assert normalize_email("jane+crm@acme.com") == "jane@acme.com"


# --------------------------------------------------------------------------
# full_name
# --------------------------------------------------------------------------

@pytest.mark.parametrize("first,last,expected", [
    ("Jane", "Doe", "Jane Doe"),
    ("Jane", None, "Jane"),         # no trailing space
    (None, "Doe", "Doe"),           # no leading space
    (None, None, ""),               # empty, NOT " " - two of those score 100
    ("", "", ""),
])
def test_full_name(first, last, expected):
    assert full_name(contact("1", first=first, last=last)) == expected


# --------------------------------------------------------------------------
# email_groups
# --------------------------------------------------------------------------

def test_groups_by_normalized_domain():
    groups = email_groups([
        contact("1", "a@acme.com"),
        contact("2", "b@ACME.COM."),        # case + trailing dot
        contact("3", "c@widgetco.com"),
    ])
    assert set(groups) == {"acme.com", "widgetco.com"}
    assert [c["id"] for c in groups["acme.com"]] == ["1", "2"]


def test_unusable_emails_are_skipped_not_bucketed():
    # the danger is a phantom bucket collecting every unparseable record
    groups = email_groups([
        contact("1", None),
        contact("2", "jane@"),
        contact("3", "not-an-email"),
    ])
    assert groups == {}


def test_gmail_typo_domains_share_a_bucket():
    groups = email_groups([
        contact("1", "a@gmail.com"),
        contact("2", "b@googlemail.com"),
        contact("3", "c@gmial.com"),
    ])
    assert set(groups) == {"gmail.com"}
    assert len(groups["gmail.com"]) == 3


# --------------------------------------------------------------------------
# find_duplicates / clustering
# --------------------------------------------------------------------------

def test_three_records_of_one_person_form_a_single_cluster():
    clusters = find_duplicates([
        contact("1", "jane.doe@gmail.com", "Jane", "Doe"),
        contact("2", "janedoe@googlemail.com", "Doe", "Jane"),   # first/last swapped
        contact("3", "Jane Doe <j.doe+crm@GMIAL.COM>", "Jane", "Doe"),
    ])
    assert len(clusters) == 1
    assert [c["id"] for c in clusters[0].members] == ["1", "2", "3"]
    assert clusters[0].domain == "gmail.com"
    assert clusters[0].confidence == 100


def test_distinct_people_in_one_domain_stay_separate():
    clusters = find_duplicates([
        contact("1", "jd@acme.com", "Jane", "Doe"),
        contact("2", "jdoe@acme.com", "Jane", "Doe"),
        contact("3", "bs@acme.com", "Bob", "Smith"),
        contact("4", "bsmith@acme.com", "Bob", "Smith"),
    ])
    assert len(clusters) == 2
    assert {tuple(c["id"] for c in cl.members) for cl in clusters} == {("1", "2"), ("3", "4")}


def test_unmatched_contact_is_not_reported():
    clusters = find_duplicates([
        contact("1", "jd@acme.com", "Jane", "Doe"),
        contact("2", "jdoe@acme.com", "Jane", "Doe"),
        contact("3", "zq@acme.com", "Zoe", "Quinn"),
    ])
    assert len(clusters) == 1
    assert "3" not in [c["id"] for c in clusters[0].members]


def test_nameless_contacts_never_match():
    # "" vs "" scores 100 - these would be reported as certain duplicates without a guard
    clusters = find_duplicates([
        contact("1", "purchasing@acme.com"),
        contact("2", "support@acme.com"),
    ])
    assert clusters == []


def test_named_contact_does_not_match_a_nameless_one():
    clusters = find_duplicates([
        contact("1", "jane@acme.com", "Jane", "Doe"),
        contact("2", "info@acme.com"),
    ])
    assert clusters == []


def test_threshold_is_respected():
    pair = [
        contact("1", "a@acme.com", "Bob", "Smith"),
        contact("2", "b@acme.com", "Bobb", "Smith"),   # scores ~95
    ]
    assert len(find_duplicates(pair, threshold=85)) == 1
    assert find_duplicates(pair, threshold=99) == []


def test_confidence_is_the_weakest_edge():
    # Kathy~Kathryn=91.7, Kathryn~Katherine=85.7 -> chained into one cluster
    clusters = find_duplicates([
        contact("1", "a@acme.com", "Kathy", "Brown"),
        contact("2", "b@acme.com", "Kathryn", "Brown"),
        contact("3", "c@acme.com", "Katherine", "Brown"),
    ])
    assert len(clusters) == 1
    assert len(clusters[0].members) == 3
    assert clusters[0].confidence == pytest.approx(85.7, abs=0.1)


def test_clusters_sorted_by_confidence_descending():
    clusters = find_duplicates([
        contact("1", "a@acme.com", "Bob", "Smith"),
        contact("2", "b@acme.com", "Bobb", "Smith"),      # ~95
        contact("3", "c@widgetco.com", "Jane", "Doe"),
        contact("4", "d@widgetco.com", "Jane", "Doe"),    # 100
    ])
    assert [c.confidence for c in clusters] == sorted(
        [c.confidence for c in clusters], reverse=True
    )
    assert clusters[0].domain == "widgetco.com"


def test_empty_input():
    assert find_duplicates([]) == []


# --------------------------------------------------------------------------
# pair_count / progress callback
# --------------------------------------------------------------------------

def test_pair_count_matches_actual_comparisons():
    contacts = [
        contact("1", "a@acme.com", "Jane", "Doe"),
        contact("2", "b@acme.com", "Jane", "Doe"),
        contact("3", "c@acme.com", "Bob", "Smith"),
        contact("4", "d@widgetco.com", "Zoe", "Quinn"),   # alone in its bucket
    ]
    seen = []
    find_duplicates(contacts, on_progress=seen.append)
    # 3 in acme (3 pairs) + 1 in widgetco (0 pairs)
    assert pair_count(contacts) == 3
    assert sum(seen) == pair_count(contacts)


def test_progress_counts_skipped_pairs_too():
    # nameless pairs are skipped for scoring but must still advance the bar
    contacts = [contact("1", "a@acme.com"), contact("2", "b@acme.com")]
    seen = []
    find_duplicates(contacts, on_progress=seen.append)
    assert sum(seen) == pair_count(contacts) == 1


# --------------------------------------------------------------------------
# excluded_from_matching
# --------------------------------------------------------------------------

def test_an_unparseable_email_is_reported_as_excluded():
    # this is the skip that used to be invisible: the contact is dropped by
    # email_groups and never appears in a cluster or a count
    excluded = excluded_from_matching([contact("1", "not-an-email", "Jane", "Doe")])
    assert [(item.contact["id"], item.reason) for item in excluded] == [("1", NO_EMAIL)]


@pytest.mark.parametrize("email", [None, "", "   ", "jane@", "a@b@c.com"])
def test_every_unbucketable_email_is_caught(email):
    assert excluded_from_matching([contact("1", email, "Jane", "Doe")])


def test_a_nameless_contact_is_excluded_even_with_a_good_email():
    # two blank names score 100 against each other, so find_duplicates skips them
    excluded = excluded_from_matching([contact("1", "ghost@acme.com")])
    assert [item.reason for item in excluded] == [NO_NAME]


def test_a_usable_contact_is_not_excluded():
    assert excluded_from_matching([contact("1", "jane@acme.com", "Jane", "Doe")]) == []


def test_one_reason_per_contact_and_email_wins():
    # no email *and* no name: the email is the more fundamental problem, and one
    # actionable reason beats two
    excluded = excluded_from_matching([contact("1")])
    assert [item.reason for item in excluded] == [NO_EMAIL]


def test_exclusions_account_for_every_contact_find_duplicates_ignored():
    # the invariant that makes the count trustworthy: anything not excluded and
    # not in a cluster was genuinely compared and found unique
    contacts = [
        contact("1", "jane@acme.com", "Jane", "Doe"),
        contact("2", "j.doe@acme.com", "Jane", "Doe"),
        contact("3", "broken", "Bob", "Smith"),
        contact("4", "ghost@acme.com"),
        contact("5", "solo@beta.com", "Ann", "Lee"),
    ]
    clustered = {m["id"] for c in find_duplicates(contacts) for m in c.members}
    excluded_ids = {item.contact["id"] for item in excluded_from_matching(contacts)}
    assert clustered == {"1", "2"}
    assert excluded_ids == {"3", "4"}
    assert clustered & excluded_ids == set()        # no contact is both


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def test_cli_reports_the_fixture_duplicate():
    result = runner.invoke(app, ["audit", "duplicates", "--from-file", str(FIXTURE)])
    assert result.exit_code == 0
    assert "Jane Doe" in result.stdout
    assert "1 cluster(s)" in result.stdout


def test_cli_reports_nothing_when_clean(tmp_path):
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps({"results": [contact("1", "solo@acme.com", "Ann", "Lee")]}))
    result = runner.invoke(app, ["audit", "duplicates", "--from-file", str(clean)])
    assert result.exit_code == 0
    assert "No duplicates found" in result.stdout


def test_cli_strict_exits_nonzero_on_findings():
    args = ["audit", "duplicates", "--from-file", str(FIXTURE)]
    assert runner.invoke(app, args).exit_code == 0
    assert runner.invoke(app, args + ["--strict"]).exit_code == 1


def test_cli_strict_still_exits_zero_when_clean(tmp_path):
    clean = tmp_path / "clean.json"
    clean.write_text(json.dumps({"results": [contact("1", "solo@acme.com", "Ann", "Lee")]}))
    result = runner.invoke(
        app, ["audit", "duplicates", "--from-file", str(clean), "--strict"]
    )
    assert result.exit_code == 0


def test_cli_threshold_option():
    result = runner.invoke(
        app, ["audit", "duplicates", "--from-file", str(FIXTURE), "-t", "101"]
    )
    assert result.exit_code != 0    # Typer rejects out-of-range before we run


def test_cli_missing_file_is_a_usage_error():
    result = runner.invoke(app, ["audit", "duplicates", "--from-file", "nope.json"])
    assert result.exit_code == 2


def test_cli_accepts_a_bare_list_as_well_as_results_wrapper(tmp_path):
    bare = tmp_path / "bare.json"
    bare.write_text(json.dumps([
        contact("1", "jd@acme.com", "Jane", "Doe"),
        contact("2", "jdoe@acme.com", "Jane", "Doe"),
    ]))
    result = runner.invoke(app, ["audit", "duplicates", "--from-file", str(bare)])
    assert result.exit_code == 0
    assert "1 cluster(s)" in result.stdout


# --------------------------------------------------------------------------
# CLI - --verbose
# --------------------------------------------------------------------------

@pytest.fixture
def with_skips(tmp_path):
    """Two real duplicates plus two contacts that can never be compared."""
    path = tmp_path / "skips.json"
    path.write_text(json.dumps({"results": [
        contact("1", "jane@acme.com", "Jane", "Doe"),
        contact("2", "j.doe@acme.com", "Jane", "Doe"),
        contact("3", "not-an-email", "Bob", "Smith"),
        contact("4", "ghost@acme.com"),
    ]}))
    return path


def test_the_skip_count_prints_without_verbose(with_skips):
    # the point of the change: `4 scanned` used to imply 4 were compared
    result = runner.invoke(app, ["audit", "duplicates", "--from-file", str(with_skips)])
    assert "2 not compared" in result.stdout
    assert "--verbose" in result.stdout          # tells you how to see which


def test_verbose_names_the_skipped_contacts(with_skips):
    result = runner.invoke(
        app, ["audit", "duplicates", "--from-file", str(with_skips), "--verbose"]
    )
    assert "Not compared" in result.stdout
    assert "not-an-email" in result.stdout       # the raw value, so you can see why
    assert NO_EMAIL in result.stdout
    assert NO_NAME in result.stdout
    assert "Re-run with --verbose" not in result.stdout      # already did


def test_a_clean_run_still_admits_what_it_skipped(tmp_path):
    # no duplicates found, but one contact was never eligible - the green panel
    # on its own would be overclaiming
    path = tmp_path / "clean.json"
    path.write_text(json.dumps({"results": [
        contact("1", "solo@acme.com", "Ann", "Lee"),
        contact("2", "broken", "Bob", "Smith"),
    ]}))
    result = runner.invoke(app, ["audit", "duplicates", "--from-file", str(path)])
    assert "No duplicates found" in result.stdout
    assert "1 not compared" in result.stdout


def test_nothing_is_said_when_nothing_was_skipped():
    result = runner.invoke(app, ["audit", "duplicates", "--from-file", str(FIXTURE), "-v"])
    assert "not compared" not in result.stdout
    assert "Not compared" not in result.stdout


def test_verbose_output_never_reaches_a_pipe(with_skips):
    result = runner.invoke(
        app, ["audit", "duplicates", "--from-file", str(with_skips), "-v", "-f", "json"]
    )
    assert json.loads(result.stdout)["duplicates"]["findings"]      # parses cleanly
    assert "Not compared" not in result.stdout

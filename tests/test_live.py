"""Integration tests against a real HubSpot portal.

Deselected by default. Every other test in this suite is offline; these are the
only ones that need a token, and they are the only ones that *write*. Running
them creates throwaway contacts, merges and archives them, and cleans up after
itself.

To run:

    export HUBSPOT_ACCESS_TOKEN=...        # needs contacts read AND write
    export HUBSPOT_LIVE_TESTS=1
    pytest -m live

Two separate switches, deliberately. The token alone isn't enough, because a
token is something you already have sitting in .env for ordinary use — so a
stray `pytest -m live` would otherwise start writing to whatever portal that
token points at. HUBSPOT_LIVE_TESTS is the one you have to set on purpose.

Use a **sandbox** portal. These tests archive and merge real records, and a
merge cannot be undone.
"""

import os
import uuid

import pytest

from hubspot_crm_clean.audits.duplicates import find_duplicates
from hubspot_crm_clean.audits.stale import find_stale
from hubspot_crm_clean.client import (
    archive_contact,
    fetch_all_contacts,
    get_client,
    merge_contacts,
)
from hubspot_crm_clean.fixes import apply_archives, apply_plans, plan_archives, plan_merges

pytestmark = pytest.mark.live

# Everything this module creates carries this in the email local part, so a
# failed run leaves an obvious trail you can search for and delete by hand.
TAG = "crmclean-livetest"


def _enabled():
    return bool(os.getenv("HUBSPOT_ACCESS_TOKEN")) and os.getenv("HUBSPOT_LIVE_TESTS") == "1"


pytest.importorskip("hubspot")
if not _enabled():                          # pragma: no cover - guard, not logic
    pytest.skip(
        "live tests need HUBSPOT_ACCESS_TOKEN and HUBSPOT_LIVE_TESTS=1",
        allow_module_level=True,
    )


def create_contact(properties):
    """Create one contact and return it in the shape the audits expect."""
    from hubspot.crm.contacts import SimplePublicObjectInputForCreate

    created = get_client().crm.contacts.basic_api.create(
        simple_public_object_input_for_create=SimplePublicObjectInputForCreate(
            properties=properties
        )
    )
    return created.to_dict()


@pytest.fixture
def run_id():
    """A unique marker per run, so concurrent runs can't collide."""
    return uuid.uuid4().hex[:8]


@pytest.fixture
def cleanup():
    """Archive whatever the test registers, even if it failed."""
    created = []
    yield created
    for contact_id in created:
        try:
            archive_contact(contact_id)
        except Exception as err:      # noqa: BLE001 - best-effort teardown
            print(f"could not clean up {contact_id}: {err}")


def test_the_token_can_read_contacts():
    # the cheapest possible check that auth and scopes are right, so a failure
    # here tells you it's your token rather than the merge logic
    contacts = fetch_all_contacts(["email"])
    assert isinstance(contacts, list)


def test_merge_actually_merges(run_id, cleanup):
    """The one thing the offline suite cannot prove: that our call shape is right."""
    primary = create_contact({
        "email": f"{TAG}-{run_id}-a@example.com",
        "firstname": "Livetest", "lastname": run_id,
        "company": "Acme", "phone": "555-0100", "lifecyclestage": "lead",
    })
    secondary = create_contact({
        "email": f"{TAG}-{run_id}-b@example.com",
        "firstname": "Livetest", "lastname": run_id,
    })
    cleanup.append(primary["id"])

    clusters = find_duplicates([primary, secondary])
    assert len(clusters) == 1, "the two records should look like duplicates"

    plans = plan_merges(clusters)
    assert plans[0].primary["id"] == primary["id"], "the fuller record should survive"

    outcomes = apply_plans(plans, merge_contacts)
    assert outcomes[0].failures == [], outcomes[0].failures
    assert outcomes[0].merged == [secondary["id"]]

    # the survivor still resolves; the absorbed id no longer stands on its own
    still_there = get_client().crm.contacts.basic_api.get_by_id(primary["id"])
    assert still_there.id == primary["id"]


def test_archive_actually_archives(run_id, cleanup):
    contact = create_contact({
        "email": f"{TAG}-{run_id}-stale@example.com",
        "firstname": "Livetest", "lastname": f"stale-{run_id}",
        "hs_last_activity_date": "2015-01-01T00:00:00Z",
    })
    # no cleanup.append: archiving IS the cleanup here

    flagged = find_stale([contact], inactive_days=90)
    assert len(flagged) == 1

    outcomes = apply_archives(plan_archives(flagged), archive_contact)
    assert outcomes[0].failure is None, outcomes[0].failure
    assert outcomes[0].archived is True

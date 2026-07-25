"""
Stale/inactive record detection.

Takes the most recent timestamp across a contact's activity fields and flags
anything older than a cutoff. A contact with no parseable activity date at all
is flagged too - no evidence of activity is not evidence of activity - but is
reported with days_inactive=None so callers can tell "silent for 400 days" apart
from "we have never heard from this record".
"""

from datetime import UTC, datetime
from typing import NamedTuple

# Mirrors rules.stale.activity_fields in config.example.yaml.
DEFAULT_ACTIVITY_FIELDS = ["hs_last_activity_date", "lastmodifieddate"]
DEFAULT_INACTIVE_DAYS = 90


class StaleContact(NamedTuple):
    """A contact with no recent activity."""
    contact: dict           # the contact itself, so callers don't re-lookup
    last_seen: object       # datetime of most recent activity, or None if unknown
    days_inactive: object   # int days since last_seen, or None if never seen


def parse_timestamp(value):
    """Parse a HubSpot timestamp, or None if it isn't usable.

    HubSpot sends ISO-8601 with a Z suffix; fromisoformat handles that natively
    on 3.11+. Anything unparseable is treated as absent rather than raising - one
    malformed record shouldn't abort the whole audit.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value))
        except (TypeError, ValueError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)    # assume UTC, HubSpot's default
    return parsed


def last_activity(contact, activity_fields=None):
    """The most recent parseable timestamp across a contact's activity fields."""
    # `is None`, not `or` - an explicit empty list means "check nothing".
    if activity_fields is None:
        activity_fields = DEFAULT_ACTIVITY_FIELDS
    props = contact["properties"]
    stamps = [parse_timestamp(props.get(field)) for field in activity_fields]
    usable = [stamp for stamp in stamps if stamp is not None]
    return max(usable) if usable else None


def find_stale(contacts, inactive_days=DEFAULT_INACTIVE_DAYS, activity_fields=None, now=None):
    """Flag contacts with no activity in the last inactive_days. Stalest first.

    now is injectable so tests don't depend on the wall clock.
    """
    if now is None:
        now = datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    flagged = []
    for contact in contacts:
        seen = last_activity(contact, activity_fields)
        if seen is None:
            flagged.append(StaleContact(contact, None, None))   # never heard from
            continue
        days = (now - seen).days
        if days >= inactive_days:
            flagged.append(StaleContact(contact, seen, days))
    # never-seen first, then longest-silent first
    flagged.sort(key=lambda item: (item.days_inactive is not None, -(item.days_inactive or 0)))
    return flagged

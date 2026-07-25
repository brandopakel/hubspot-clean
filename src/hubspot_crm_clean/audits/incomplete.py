"""
Missing/incomplete field detection.

Scores each contact on how many of its required fields are actually filled in,
and flags the ones below a threshold. "Filled in" is stricter than "present":
HubSpot returns unset properties as null, but a field a human cleared comes back
as an empty string, and pasted data often leaves whitespace behind. All three
count as missing.
"""

from typing import NamedTuple

# Mirrors rules.incomplete.required_fields in config.example.yaml.
DEFAULT_REQUIRED_FIELDS = ["email", "company", "lifecyclestage", "phone"]
DEFAULT_MIN_COMPLETENESS = 75


class IncompleteContact(NamedTuple):
    """A contact that failed the completeness bar."""
    contact: dict   # the contact itself, so callers don't re-lookup
    score: float    # percentage of required fields present, 0-100
    missing: list   # which required fields were blank, in the order given


def is_blank(value):
    """True if a property carries no usable value.

    Covers None (HubSpot's null), "" (a field someone cleared), and whitespace
    left behind by a paste. Non-string values (numbers, bools) are never blank.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    return False


def missing_fields(contact, required_fields=None):
    """Which of the required fields are blank on this contact."""
    # `is None`, not `or` - an explicit empty list means "nothing is required",
    # and `or` would silently swap it for the defaults.
    if required_fields is None:
        required_fields = DEFAULT_REQUIRED_FIELDS
    props = contact["properties"]
    return [field for field in required_fields if is_blank(props.get(field))]


def completeness_score(contact, required_fields=None):
    """Percentage of required fields this contact actually fills in, 0-100."""
    # `is None`, not `or` - an explicit empty list means "nothing is required",
    # and `or` would silently swap it for the defaults.
    if required_fields is None:
        required_fields = DEFAULT_REQUIRED_FIELDS
    if not required_fields:
        return 100.0    # nothing required means nothing missing
    filled = len(required_fields) - len(missing_fields(contact, required_fields))
    return filled / len(required_fields) * 100


def find_incomplete(contacts, required_fields=None, min_completeness=DEFAULT_MIN_COMPLETENESS):
    """Flag contacts scoring below min_completeness. Worst first."""
    # `is None`, not `or` - an explicit empty list means "nothing is required",
    # and `or` would silently swap it for the defaults.
    if required_fields is None:
        required_fields = DEFAULT_REQUIRED_FIELDS
    flagged = []
    for contact in contacts:
        missing = missing_fields(contact, required_fields)
        if not missing:
            continue    # complete, nothing to score
        score = (len(required_fields) - len(missing)) / len(required_fields) * 100
        if score < min_completeness:
            flagged.append(IncompleteContact(contact, score, missing))
    flagged.sort(key=lambda item: (item.score, item.contact["id"]))
    return flagged

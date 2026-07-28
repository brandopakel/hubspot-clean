"""
Fix planning and execution: merging duplicates, archiving stale records.

Planning is pure. It takes what the audits already found and decides what should
happen to each record. Nothing in here talks to HubSpot - the apply_* functions
take the write as a callable, which is what lets the whole module be tested
without an API key and, more importantly, what lets the dry run and the real run
share one code path. A preview that runs different code from the thing it
previews is not a preview.

This is the only part of the project that can change your CRM, so the defaults
are arranged so that doing nothing is safe: planning is free, and the caller has
to hand over a working write function before a single record moves.
"""

from datetime import UTC, datetime
from typing import NamedTuple

from hubspot_crm_clean.audits.incomplete import completeness_score
from hubspot_crm_clean.audits.stale import parse_timestamp

# Sorts last, so a record with no createdate never wins the "oldest" tie-break
# just because its date is missing. Timezone-aware to match parse_timestamp,
# which always returns aware datetimes - mixing the two raises on comparison.
UNKNOWN_DATE = datetime.max.replace(tzinfo=UTC)

# Why a record was picked to survive, in the order the rules are applied.
MOST_COMPLETE = "most complete"
OLDEST = "oldest"
LOWEST_ID = "lowest id"
CHOSEN = "you chose"        # --interactive overrode the rules


class MergePlan(NamedTuple):
    """One cluster, resolved into a survivor and the records folded into it."""
    primary: dict       # survives, and keeps its own values on conflict
    absorbed: list      # merged into primary, in cluster order
    reason: str         # which rule picked the primary
    domain: str         # carried from the cluster, for reporting
    confidence: float   # carried from the cluster: how sure we are it IS a duplicate


class MergeOutcome(NamedTuple):
    """What actually happened when a plan was applied."""
    plan: MergePlan
    merged: list        # ids folded in successfully
    failures: list      # (id, message) for the ones that didn't


def created(contact):
    """When the record was created, or UNKNOWN_DATE if we can't tell."""
    stamp = parse_timestamp(contact["properties"].get("createdate"))
    return UNKNOWN_DATE if stamp is None else stamp


def id_key(contact):
    """Sort key for a contact id.

    HubSpot ids are numeric strings, and sorting them as text puts "10" before
    "9". Numeric ids sort numerically; anything else sorts after them as text, so
    the order is total no matter what the portal returns.
    """
    raw = str(contact["id"])
    return (0, int(raw), "") if raw.isdigit() else (1, 0, raw)


def choose_primary(members, required_fields=None):
    """Which record should survive, and which rule decided it.

    Most complete first: HubSpot keeps the primary's value wherever two records
    disagree on a property, so the fullest record loses the least. Ties go to the
    oldest, which is the record other systems have had the longest to reference,
    and finally to the lowest id so the choice is deterministic rather than
    dependent on whatever order the API happened to return.
    """
    scored = [(completeness_score(member, required_fields), member) for member in members]
    best = max(score for score, _ in scored)
    top = [member for score, member in scored if score == best]
    if len(top) == 1:
        return top[0], MOST_COMPLETE

    ranked = sorted(top, key=lambda member: (created(member), id_key(member)))
    winner, rest = ranked[0], ranked[1:]
    if rest and created(winner) < min(created(member) for member in rest):
        return winner, OLDEST
    return winner, LOWEST_ID


def plan_merges(clusters, required_fields=None, limit=None):
    """Turn duplicate clusters into merge plans, highest confidence first.

    `limit` caps how many clusters are planned - the point of it is a cautious
    first run against a real portal, so it truncates the plan itself rather than
    filtering later. Clusters arrive already sorted by confidence, so a limited
    run acts on the ones we are surest about.
    """
    plans = []
    for cluster in clusters:
        primary, reason = choose_primary(cluster.members, required_fields)
        absorbed = [m for m in cluster.members if m["id"] != primary["id"]]
        if not absorbed:
            continue    # a cluster of one isn't a merge; find_duplicates shouldn't emit these
        plans.append(MergePlan(primary, absorbed, reason, cluster.domain, cluster.confidence))
    return plans[:limit] if limit is not None else plans


def merge_count(plans):
    """How many records would be folded in. Not the same as len(plans) - a
    cluster of three is one plan but two merges."""
    return sum(len(plan.absorbed) for plan in plans)


def apply_plans(plans, merge_fn, on_progress=None):
    """Execute the plans, returning what happened to each.

    merge_fn(primary_id, absorbed_id) performs one write. A failure is recorded
    and the run continues: stopping halfway through would leave the portal in a
    state nobody planned, and the caller needs the whole picture to know what to
    retry. Only the caller can decide the exit code, so nothing is raised here.
    """
    outcomes = []
    for plan in plans:
        merged, failures = [], []
        for contact in plan.absorbed:
            try:
                merge_fn(plan.primary["id"], contact["id"])
                merged.append(contact["id"])
            except Exception as err:      # noqa: BLE001 - one bad record must not end the run
                failures.append((contact["id"], str(err)))
            if on_progress:
                on_progress(1)
        outcomes.append(MergeOutcome(plan, merged, failures))
    return outcomes


def failure_count(outcomes):
    """How many individual merges failed."""
    return sum(len(outcome.failures) for outcome in outcomes)


def repoint(plan, primary_id):
    """Rebuild a plan around a different survivor.

    What --interactive does once you pick a record: the cluster is unchanged, but
    a different member keeps its values and the rest are folded into it instead.
    The reason becomes CHOSEN, so the report records that a human overrode the
    rules rather than that the rules produced this.
    """
    members = [plan.primary, *plan.absorbed]
    primary = next(m for m in members if m["id"] == primary_id)
    absorbed = [m for m in members if m["id"] != primary_id]
    return plan._replace(primary=primary, absorbed=absorbed, reason=CHOSEN)


# --------------------------------------------------------------------------
# Archiving stale records
#
# Simpler than merging: there's no survivor to choose, just a list of records to
# take out of the active CRM. Unlike a merge, an archive is recoverable from
# HubSpot's recycle bin for 90 days - which is why this one is allowed to run
# from a --from-file snapshot and merging is not.
# --------------------------------------------------------------------------

class ArchivePlan(NamedTuple):
    """One record proposed for archiving."""
    contact: dict
    days_inactive: object   # int, or None for a record we have never heard from


class ArchiveOutcome(NamedTuple):
    """What actually happened to one archive plan."""
    plan: ArchivePlan
    archived: bool
    failure: object         # str, or None


def plan_archives(flagged, limit=None):
    """Turn find_stale results into archive plans, stalest first.

    `limit` truncates rather than filters, and find_stale already sorts by how
    long a record has been silent - so a limited run archives the deadest
    records rather than an arbitrary slice.
    """
    plans = [ArchivePlan(item.contact, item.days_inactive) for item in flagged]
    return plans[:limit] if limit is not None else plans


def apply_archives(plans, archive_fn, on_progress=None):
    """Execute the plans, returning what happened to each.

    Same contract as apply_plans: a failure is recorded, the run continues, and
    nothing is raised - the caller owns the exit code.
    """
    outcomes = []
    for plan in plans:
        try:
            archive_fn(plan.contact["id"])
            outcomes.append(ArchiveOutcome(plan, archived=True, failure=None))
        except Exception as err:      # noqa: BLE001 - one bad record must not end the run
            outcomes.append(ArchiveOutcome(plan, archived=False, failure=str(err)))
        if on_progress:
            on_progress(1)
    return outcomes


def archive_failures(outcomes):
    """How many archives failed."""
    return sum(1 for outcome in outcomes if outcome.failure is not None)

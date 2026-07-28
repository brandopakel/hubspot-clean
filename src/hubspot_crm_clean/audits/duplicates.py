"""
Fuzzy duplicate detection.

Pipeline:
1. normalize_email  - reduce a messy address to a canonical local@domain.tld
2. email_groups     - bucket contacts by normalized domain
3. find_duplicates  - fuzzy match names inside each bucket, then merge the
                      matched pairs into clusters with union-find

Names are scored with rapidfuzz's token_sort_ratio, which is order-insensitive,
so a first/last name swap still matches. Anything that can't be parsed into a
confident address is skipped rather than guessed at - a fabricated address would
silently merge two different people.
"""

import re
from collections import defaultdict
from itertools import combinations
from typing import NamedTuple

from rapidfuzz import fuzz, utils

# Mirrors rules.duplicates.match_threshold in config.example.yaml.
DEFAULT_THRESHOLD = 85

DOMAIN_TYPOS = {"googlemail.com": "gmail.com",
                "gmial.com": "gmail.com",
                "ggmai.com": "gmail.com",
                "hotmial.com": "hotmail.com",
                "yahooo.com": "yahoo.com",}

_ANGLE = re.compile(r"<([^<>]*)>")  # grabs what's INSIDE the brackets

def double_strip(raw):
    """Ensure no nested <>, mailto:, or display name survives."""
    if not raw:
        return None
    e = raw.lower().strip()
    match = _ANGLE.search(e)    # "jane doe <jane@acme.com>" -> "jane@acme.com"
    if match:                   # strip() only reaches the ends; this reaches the middle
        e = match.group(1)
    for _ in range(2):
        e = e.strip("<>").removeprefix("mailto:")
    e = e.replace(" ", "").replace("\u200b", "")   # NBSP / zero-width paste junk
    return e.strip()

def normalize_email(raw_email):
    """Normalize an email address, or return None if there isn't a usable one."""
    email = double_strip(raw_email)
    if not email:   # double_strip gives None for falsy input, "" for input that empties out
        return None
    if any(ch.isspace() for ch in email):   # internal whitespace = malformed, don't guess
        return None
    if email.count("@") != 1:   # 0 or 2+ is not an address we trust
        return None
    local, domain = email.rsplit("@",1) # rsplit - split("@",1) breaks on "a@b@c"
    domain = domain.strip(".")  # strip not rstrip - rstrip can't reach a LEADING dot
    domain = DOMAIN_TYPOS.get(domain, domain)
    if not local or "." not in domain:  # guard BOTH halves - "jane@" used to survive
        return None
    local = local.split("+", 1)[0]  # +tag is ignored by most providers
    if domain == "gmail.com":
        local = local.replace(".", "")  # dots insignificant on Gmail ONLY
    return f"{local}@{domain}" if local else None

def email_groups(contacts):
    """Group contacts by email domain."""
    groups = defaultdict(list)
    for contact in contacts:
        email = normalize_email(contact["properties"].get("email"))
        if not email:
            continue # skip contacts we can't bucket
        domain = email.rsplit("@",1)[1] # normalize_email already lowercased it
        groups[domain].append(contact)
    return groups

def full_name(contact):
    """Return full name of contact, or "" if we have neither part."""
    props = contact["properties"]
    first = props.get("firstname") or "" # None safety barrier
    last = props.get("lastname") or "" # --^
    return f"{first} {last}".strip() # strip -> a nameless contact gives "" not " "

class Cluster(NamedTuple):
    """A group of contacts that all look like the same person."""
    domain: str
    members: list   # the contact dicts themselves, so callers don't re-lookup
    confidence: float   # weakest pairwise score holding the cluster together

def _find(parent, contact_id):
    """Walk up to the root of this contact's group, flattening as we go."""
    while parent[contact_id] != contact_id:
        parent[contact_id] = parent[parent[contact_id]] # path compression
        contact_id = parent[contact_id]
    return contact_id

def _union(parent, id_a, id_b):
    """Merge the two groups that id_a and id_b belong to."""
    root_a, root_b = _find(parent, id_a), _find(parent, id_b)
    if root_a != root_b:
        parent[root_b] = root_a # hang one root off the other

def pair_count(contacts):
    """How many comparisons find_duplicates will make. Lets callers size a progress bar."""
    return sum(len(b) * (len(b) - 1) // 2 for b in email_groups(contacts).values())

def find_duplicates(contacts, threshold=DEFAULT_THRESHOLD, on_progress=None):
    """Detect duplicate contacts, merged into clusters. Highest confidence first.

    on_progress, if given, is called with 1 per comparison — see pair_count() for the
    total. Kept as a plain callback so this module never imports a UI library.
    """
    groups = email_groups(contacts)     # <-- email_groups called in here
    clusters = []
    for domain, bucket in groups.items():       # <-- outer loop: one bucket at a time
        parent = {c["id"]: c["id"] for c in bucket} # everyone starts in their own group
        edge_scores = defaultdict(list)
        for a, b in combinations(bucket,2): # bucket = groups[domain]
            if on_progress:
                on_progress(1)  # before the skips below, so the bar always reaches 100%
            name_a = full_name(a) # <- one contact in, name string out
            name_b = full_name(b) # <- the other contact
            if not name_a or not name_b:
                continue # no name = nothing to compare; "" vs "" scores 100
            score = fuzz.token_sort_ratio(
                name_a,
                name_b,
                processor=utils.default_process,
            )
            if score >= threshold:
                _union(parent, a["id"], b["id"])
                edge_scores[a["id"]].append(score) # re-rooted after all unions below
        members = defaultdict(list)
        for contact in bucket:  # bucket order preserved inside each cluster
            members[_find(parent, contact["id"])].append(contact)
        scores = defaultdict(list)
        for id_a, found in edge_scores.items(): # roots are only final once unions are done
            scores[_find(parent, id_a)].extend(found)
        for root, group in members.items():
            if len(group) < 2:
                continue # a group of one isn't a duplicate
            clusters.append(Cluster(domain, group, min(scores[root])))
    clusters.sort(key=lambda c: (-c.confidence, c.domain))
    return clusters

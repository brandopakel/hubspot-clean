"""
Fuzzy duplicate detection. Week 2 — write this yourself.

Plan from the project doc:
- Group contacts by email domain
- Fuzzy match on full name within each group (threshold: 85+)
- Return clusters of likely duplicates with confidence scores

thefuzz gives you fuzz.ratio() / fuzz.token_sort_ratio() — worth trying
both and seeing which behaves better on messy real-world names.
"""

from rapidfuzz import fuzz, utils
from collections import defaultdict
from itertools import combinations

def email_groups(contacts):
    """Group contacts by email domain."""
    groups = defaultdict(list)
    for contact in contacts:
        email = contact["properties"].get("email")
        if not email or "@" not in email:
            continue # skip contacts we can't bucket
        domain = email.split("@",1)[1].lower() # make sure cases are normalized lower
        groups[domain].append(contact)
    return groups

def full_name(contact):
    """Return full name of contact."""
    props = contact["properties"]
    first = props.get("firstname") or "" # None safety barrier
    last = props.get("lastname") or "" # --^
    return f"{first} {last}"

def find_duplicates(contacts, threshold=85):
    """Detect duplicate contacts."""
    groups = email_groups(contacts)     # <-- email_groups called in here
    duplicates = defaultdict(list)
    for domain, bucket in groups.items():       # <-- outer loop: one bucket at a time
        for a, b in combinations(bucket,2): # bucket = groups[domain]
            score = fuzz.token_sort_ratio(
                full_name(a), # <- one contact in, name string out
                full_name(b), # <- the other contact
                processor=utils.default_process,
            )
            if score >= threshold:
                duplicates[domain].append((full_name(a), a["id"], full_name(b), b["id"],score))
    return duplicates

"""
Fuzzy duplicate detection. Week 2 — write this yourself.

Plan from the project doc:
- Group contacts by email domain
- Fuzzy match on full name within each group (threshold: 85+)
- Return clusters of likely duplicates with confidence scores

thefuzz gives you fuzz.ratio() / fuzz.token_sort_ratio() — worth trying
both and seeing which behaves better on messy real-world names.
"""

# TODO: implement duplicate detection

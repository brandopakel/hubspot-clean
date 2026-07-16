"""
HubSpot API connection and data fetching.

This is Week 1 core logic — write this yourself. Notes to guide you:

- Read HUBSPOT_API_KEY from the environment (use python-dotenv to load .env
  in dev; don't commit the real .env).
- The `hubspot-api-client` SDK exposes a Client object, e.g.:
      from hubspot import HubSpot
      client = HubSpot(access_token=api_key)
- Contacts are paginated. Look at client.crm.contacts.basic_api.get_page()
  and note the `paging.next.after` cursor in the response — you'll need to
  loop until it's None.
- Decide what a "contact" looks like as it flows through your app. You'll
  probably want to normalize the SDK's response objects into plain dicts
  (or a small dataclass) so the rest of the app (audits, reports) doesn't
  need to know about HubSpot SDK internals. That decision is worth making
  deliberately — it affects how easy Week 2/3 will be.
- Think about what should happen on auth failure (bad/missing key) vs.
  a rate-limit response vs. a network error. Different failure modes,
  probably different handling.

Suggested shape (feel free to change):

    def get_client() -> "HubSpot": ...

    def fetch_all_contacts(properties: list[str] | None = None) -> list[dict]:
        '''Fetch every contact, handling pagination, return normalized dicts.'''
        ...
"""

# TODO: implement HubSpot client setup and fetch_all_contacts()

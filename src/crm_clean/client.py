import os
from dotenv import load_dotenv
from hubspot import HubSpot

load_dotenv()

def get_client():
    api_key = os.getenv('HUBSPOT_ACCESS_TOKEN')
    if api_key is None:
        raise ValueError("HUBSPOT_ACCESS_TOKEN environment variable is not set")
    client = HubSpot(access_token=api_key)
    return client

def fetch_all_contacts(properties: list[str] | None = None) -> list[dict]:
    '''Fetch every contact, handling pagination, return normalized dicts.'''
    ct = get_client()
    after = None
    all_contacts = []
    while True:
        response = ct.crm.contacts.basic_api.get_page(limit=100, after=after, properties=properties)
        all_contacts.extend(response.results)
        if response.paging is None or response.paging.next is None:
            break
        after = response.paging.next.after
    norm_contacts = normalize_contacts(all_contacts)
    return norm_contacts

def normalize_contacts(contacts: list) -> list[dict]:
    '''Normalize contacts into a list of dicts.'''
    normalized_contacts = [contact.to_dict() for contact in contacts]
    return normalized_contacts

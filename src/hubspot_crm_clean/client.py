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

def fetch_all_contacts(properties, on_page=None) -> list[dict]:
    '''Fetch every contact, handling pagination, return normalized dicts.

    on_page, if given, is called with the running total after each page. Lets a caller
    drive a progress display without this module importing a UI library.
    '''
    ct = get_client()
    after = None
    all_contacts = []
    while True:
        response = ct.crm.contacts.basic_api.get_page(limit=100, after=after, properties=properties)
        all_contacts.extend(response.results)
        if on_page:
            on_page(len(all_contacts))
        if response.paging is None or response.paging.next is None:
            break
        after = response.paging.next.after
    norm_contacts = normalize_contacts(all_contacts)
    return norm_contacts

def all_property_names(object_type: str = "contacts") -> list[str]:
    '''Every property name defined on an object type, including custom ones.

    The objects API has no "all properties" wildcard — you have to enumerate them
    here and pass the full list explicitly.
    '''
    ct = get_client()
    response = ct.crm.properties.core_api.get_all(object_type)
    return [prop.name for prop in response.results]


def normalize_contacts(contacts: list) -> list[dict]:
    '''Normalize contacts into a list of dicts.'''
    normalized_contacts = [contact.to_dict() for contact in contacts]
    return normalized_contacts

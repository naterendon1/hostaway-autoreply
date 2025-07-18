# utils.py

import os
import requests
import logging

HOSTAWAY_CLIENT_ID = os.getenv("HOSTAWAY_CLIENT_ID")
HOSTAWAY_CLIENT_SECRET = os.getenv("HOSTAWAY_CLIENT_SECRET")
HOSTAWAY_API_BASE = "https://api.hostaway.com/v1"

def get_hostaway_access_token() -> str:
    url = f"{HOSTAWAY_API_BASE}/accessTokens"
    data = {
        "grant_type": "client_credentials",
        "client_id": HOSTAWAY_CLIENT_ID,
        "client_secret": HOSTAWAY_CLIENT_SECRET,
        "scope": "general"
    }
    try:
        r = requests.post(url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as e:
        logging.error(f"❌ Token error: {e}")
        return None

def fetch_hostaway_resource(resource: str, resource_id: int):
    token = get_hostaway_access_token()
    if not token:
        return None
    url = f"{HOSTAWAY_API_BASE}/{resource}/{resource_id}"
    try:
        r = requests.get(url, headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logging.error(f"❌ Fetch {resource} error: {e}")
        return None

def send_reply_to_hostaway(conversation_id: str, reply_text: str, communication_type: str = "email") -> bool:
    token = get_hostaway_access_token()
    if not token:
        return False
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}/messages"
    payload = {
        "body": reply_text,
        "isIncoming": 0,
        "communicationType": communication_type
    }
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json"
    }
    try:
        r = requests.post(url, headers=headers, json=payload)
        r.raise_for_status()
        logging.info(f"✅ Sent to Hostaway: {r.text}")
        return True
    except Exception as e:
        logging.error(f"❌ Send error: {e}")
        return False

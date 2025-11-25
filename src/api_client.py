"""
API Client for Hostaway Integration
-----------------------------------
Handles:
- Sending replies to guest conversations via Hostaway API
- Fetching reservation, listing, and conversation data
"""

import os
import logging
import requests

HOSTAWAY_API_BASE = os.getenv("HOSTAWAY_API_BASE", "https://api.hostaway.com/v1")
HOSTAWAY_ACCESS_TOKEN = os.getenv("HOSTAWAY_ACCESS_TOKEN")


# ---------------------------------------------------------------------
# Send Reply
# ---------------------------------------------------------------------
def send_hostaway_reply(conversation_id: int, message: str) -> bool:
    """
    Sends a reply message to a Hostaway guest conversation.
    """
    if not (HOSTAWAY_ACCESS_TOKEN and conversation_id and message):
        logging.warning("[send_hostaway_reply] Missing token, conversation_id, or message.")
        return False

    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}/messages"
    headers = {
        "Authorization": f"Bearer {HOSTAWAY_ACCESS_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"body": message}

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=10)
        if resp.status_code == 200:
            logging.info(f"[Hostaway] Reply sent successfully to conversation {conversation_id}")
            return True
        else:
            logging.error(f"[Hostaway] Failed to send reply (status={resp.status_code}, resp={resp.text})")
            return False
    except Exception as e:
        logging.error(f"[Hostaway] Error sending reply: {e}")
        return False


# ---------------------------------------------------------------------
# Fetchers (for message_handler)
# ---------------------------------------------------------------------
def fetch_hostaway_reservation(reservation_id: int):
    """Fetch reservation details by ID from Hostaway API."""
    if not reservation_id:
        return {}
    url = f"{HOSTAWAY_API_BASE}/reservations/{reservation_id}"
    headers = {"Authorization": f"Bearer {HOSTAWAY_ACCESS_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        logging.error(f"[api_client] fetch_hostaway_reservation failed: {e}")
        return {}


def fetch_hostaway_listing(listing_id: int):
    """Fetch listing details by ID."""
    if not listing_id:
        return {}
    url = f"{HOSTAWAY_API_BASE}/listings/{listing_id}"
    headers = {"Authorization": f"Bearer {HOSTAWAY_ACCESS_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        logging.error(f"[api_client] fetch_hostaway_listing failed: {e}")
        return {}


def fetch_hostaway_conversation(conversation_id: int):
    """Fetch conversation thread by ID."""
    if not conversation_id:
        return {}
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}"
    headers = {"Authorization": f"Bearer {HOSTAWAY_ACCESS_TOKEN}"}
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        logging.error(f"[api_client] fetch_hostaway_conversation failed: {e}")
        return {}

def fetch_conversation_messages(conversation_id: int, limit: int = 50) -> list:
    """
    Fetch all messages from a Hostaway conversation.
    
    Returns list of messages sorted by date (oldest first).
    """
    url = f"{HOSTAWAY_API_BASE}/conversations/{conversation_id}/messages"
    headers = {"Authorization": f"Bearer {HOSTAWAY_ACCESS_TOKEN}"}
    
    try:
        response = requests.get(
            url, 
            headers=headers,
            params={"limit": limit, "includeScheduledMessages": 0},
            timeout=10
        )
        
        if response.status_code == 200:
            data = response.json()
            messages = data.get("result", [])
            
            # Sort by insertedOn (oldest first)
            messages.sort(key=lambda m: m.get("insertedOn", ""))
            
            return messages
        else:
            logging.error(f"Failed to fetch messages: {response.status_code}")
            return []
    except Exception as e:
        logging.error(f"Error fetching messages: {e}")
        return []

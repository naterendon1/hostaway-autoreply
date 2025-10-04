# src/api_client.py
import requests
from typing import Dict, Any, Optional, List
from src import config

class HostawayAPI:
    """Wrapper for Hostaway API calls."""

    def __init__(self, base_url: str = config.HOSTAWAY_API_BASE, token: str = config.HOSTAWAY_ACCESS_TOKEN):
        self.base_url = base_url
        self.headers = {
            "Authorization": f"Bearer {token}",
            "Cache-Control": "no-cache",
            "Content-Type": "application/json",
        }

    def get_listing(self, listing_id: int) -> Dict[str, Any]:
        url = f"{self.base_url}/listings/{listing_id}"
        res = requests.get(url, headers=self.headers)
        res.raise_for_status()
        return res.json()

    def get_reservation(self, reservation_id: int) -> Dict[str, Any]:
        url = f"{self.base_url}/reservations/{reservation_id}"
        res = requests.get(url, headers=self.headers)
        res.raise_for_status()
        return res.json()

    def get_conversation_messages(self, reservation_id: int) -> Dict[str, Any]:
        url = f"{self.base_url}/conversations?reservationId={reservation_id}"
        res = requests.get(url, headers=self.headers)
        res.raise_for_status()
        return res.json()

    def send_message(self, reservation_id: int, message: str) -> Dict[str, Any]:
        url = f"{self.base_url}/conversations/{reservation_id}/messages"
        payload = {"message": message}
        res = requests.post(url, json=payload, headers=self.headers)
        res.raise_for_status()
        return res.json()

    def get_guest_charges(self, reservation_id: int) -> Dict[str, Any]:
        url = f"{self.base_url}/guestPayments/charges?reservationId={reservation_id}"
        res = requests.get(url, headers=self.headers)
        res.raise_for_status()
        return res.json()

    def get_extras(self, reservation_id: Optional[int] = None) -> Dict[str, Any]:
        url = f"{self.base_url}/expenses"
        params = {"reservationId": reservation_id} if reservation_id else {}
        res = requests.get(url, headers=self.headers, params=params)
        res.raise_for_status()
        return res.json()

    def setup_unified_webhook(self, url: str) -> Dict[str, Any]:
        """Registers a unified webhook for Hostaway (messages, reservations, etc.)."""
        webhook_url = f"{self.base_url}/webhooks/unifiedWebhooks"
        payload = {
            "isEnabled": 1,
            "url": url,
        }
        res = requests.post(webhook_url, json=payload, headers=self.headers)
        res.raise_for_status()
        return res.json()


class GoogleAPI:
    """Wrapper for Google Places & Distance Matrix."""

    def __init__(self, api_key: str = config.GOOGLE_PLACES_API_KEY):
        self.api_key = api_key

    def search_places(self, query: str, location: Optional[str] = None, radius: int = 5000) -> Dict[str, Any]:
        """Find nearby places using Google Places API."""
        url = "https://maps.googleapis.com/maps/api/place/textsearch/json"
        params = {"query": query, "key": self.api_key, "radius": radius}
        if location:
            params["location"] = location
        res = requests.get(url, params=params)
        res.raise_for_status()
        return res.json()

    def get_directions(self, origin: str, destination: str) -> Dict[str, Any]:
        """Get travel directions via Google Distance Matrix API."""
        url = "https://maps.googleapis.com/maps/api/distancematrix/json"
        params = {
            "origins": origin,
            "destinations": destination,
            "key": config.GOOGLE_DISTANCE_MATRIX_API_KEY,
        }
        res = requests.get(url, params=params)
        res.raise_for_status()
        return res.json()


# --- Singletons for reuse ---
hostaway_client = HostawayAPI()
google_client = GoogleAPI()

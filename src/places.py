# file: src/places.py
"""
Google Places Integration for Hostaway AutoReply
------------------------------------------------
Handles:
- Detecting when guests ask for local recommendations
- Fetching nearby places using Google Places API
- Building formatted recommendations
"""

import os
import logging
import requests
from typing import List, Dict, Optional

GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")
GOOGLE_PLACES_BASE_URL = "https://maps.googleapis.com/maps/api/place"


def should_fetch_local_recs(message: str) -> bool:
    """
    Determine if a guest message is asking for local recommendations.

    Args:
        message: The guest's message

    Returns:
        True if message appears to be asking for recommendations
    """
    if not message:
        return False

    message_lower = message.lower()

    # Keywords that indicate the guest wants recommendations
    keywords = [
        "recommend", "suggestion", "nearby", "close to", "near",
        "restaurant", "coffee", "food", "eat", "drink",
        "things to do", "activities", "attraction", "visit",
        "grocery", "store", "shop", "market",
        "bar", "pub", "nightlife", "entertainment"
    ]

    return any(keyword in message_lower for keyword in keywords)


def build_local_recs(
    lat: Optional[float],
    lng: Optional[float],
    guest_message: str,
    radius: int = 1000
) -> List[Dict[str, str]]:
    """
    Fetch and format nearby place recommendations using Google Places API.

    Args:
        lat: Property latitude
        lng: Property longitude
        guest_message: Guest's message (to determine what type of places to search)
        radius: Search radius in meters (default 1000m = ~0.6 miles)

    Returns:
        List of nearby places with name and type
    """
    if not GOOGLE_PLACES_API_KEY:
        logging.warning("[places] Google Places API key not configured")
        return []

    if not lat or not lng:
        logging.warning("[places] Missing latitude/longitude for property")
        return []

    # Determine place type based on guest message
    place_type = _determine_place_type(guest_message)

    try:
        url = f"{GOOGLE_PLACES_BASE_URL}/nearbysearch/json"
        params = {
            "location": f"{lat},{lng}",
            "radius": radius,
            "type": place_type,
            "key": GOOGLE_PLACES_API_KEY,
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("status") != "OK":
            logging.warning(f"[places] Google Places API error: {data.get('status')}")
            return []

        # Extract and format results
        results = []
        for place in data.get("results", [])[:5]:  # Limit to top 5
            results.append({
                "name": place.get("name", "Unknown"),
                "type": place.get("types", ["place"])[0].replace("_", " ").title(),
                "rating": place.get("rating", "N/A"),
                "vicinity": place.get("vicinity", "")
            })

        logging.info(f"[places] Found {len(results)} nearby places of type '{place_type}'")
        return results

    except Exception as e:
        logging.error(f"[places] Error fetching recommendations: {e}")
        return []


def _determine_place_type(message: str) -> str:
    """
    Determine the type of place to search for based on message content.

    Args:
        message: Guest's message

    Returns:
        Google Places API place type
    """
    message_lower = message.lower()

    # Map keywords to Google Places types
    type_map = {
        "restaurant": ["restaurant", "food", "eat", "dinner", "lunch"],
        "cafe": ["coffee", "cafe", "breakfast"],
        "bar": ["bar", "pub", "drink", "nightlife"],
        "supermarket": ["grocery", "supermarket", "market"],
        "store": ["shop", "store", "shopping"],
        "tourist_attraction": ["attraction", "visit", "see", "things to do"],
        "park": ["park", "outdoor", "nature"],
    }

    for place_type, keywords in type_map.items():
        if any(keyword in message_lower for keyword in keywords):
            return place_type

    # Default to general point of interest
    return "point_of_interest"


def get_distance_matrix(
    origin_lat: float,
    origin_lng: float,
    dest_lat: float,
    dest_lng: float
) -> Optional[Dict[str, str]]:
    """
    Get distance and travel time between two locations.

    Args:
        origin_lat: Origin latitude
        origin_lng: Origin longitude
        dest_lat: Destination latitude
        dest_lng: Destination longitude

    Returns:
        Dictionary with distance and duration, or None if error
    """
    if not GOOGLE_PLACES_API_KEY:
        return None

    try:
        url = "https://maps.googleapis.com/maps/api/distancematrix/json"
        params = {
            "origins": f"{origin_lat},{origin_lng}",
            "destinations": f"{dest_lat},{dest_lng}",
            "key": GOOGLE_PLACES_API_KEY,
        }

        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()

        if data.get("status") != "OK":
            return None

        element = data["rows"][0]["elements"][0]
        if element.get("status") != "OK":
            return None

        return {
            "distance": element["distance"]["text"],
            "duration": element["duration"]["text"]
        }

    except Exception as e:
        logging.error(f"[places] Error fetching distance matrix: {e}")
        return None

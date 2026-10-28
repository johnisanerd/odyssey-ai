import os
import requests
from typing import List
from datetime import datetime, timedelta
from src.graph.state import TransitOption, TravelMode
from src.tools.flights.base import FlightProvider
from src.tools.flights.airport_resolver import get_airport_code

import json

import threading

_SKYSCANNER_LOCATION_NAME = "skyscanner_location_cache.json"
_location_cache_lock = threading.Lock()

def get_cache_path(filename: str) -> str:
    project_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
    cache_dir = os.path.join(project_root, "cache")
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, filename)

def _load_skyscanner_location_cache() -> dict:
    """Loads Skyscanner location cache from disk."""
    cache_file = get_cache_path(_SKYSCANNER_LOCATION_NAME)
    with _location_cache_lock:
        if os.path.exists(cache_file):
            try:
                with open(cache_file, "r") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

def _save_skyscanner_location_cache(cache: dict):
    """Saves Skyscanner location cache from disk."""
    cache_file = get_cache_path(_SKYSCANNER_LOCATION_NAME)
    with _location_cache_lock:
        try:
            with open(cache_file, "w") as f:
                json.dump(cache, f, indent=2)
        except Exception:
            pass

def resolve_skyscanner_location(city_name: str, token: str) -> tuple:
    """
    Resolves Skyscanner SkyId and EntityId for a city.
    Uses IATA codes directly for both to bypass searchAirport Apify calls.
    """
    # 1. Try static DB lookup first (Fast, O(1))
    from src.tools.flights.airport_resolver import _lookup_static_india_airports
    static_matches = _lookup_static_india_airports(city_name)
    if static_matches:
        code = static_matches[0].iata_code.upper()
        return (code, code)
        
    # 2. Use airport resolver (Geocoding / Google Search / LLM)
    code = get_airport_code(city_name)
    return (code, code)


class SkyscannerFlightProvider(FlightProvider):
    """
    Flight provider that runs Google Flights searches synchronously using Apify actor johnvc~Google-Flights-Data-Scraper-Flight-and-Price-Search.
    Kept under the 'skyscanner' provider name so existing FLIGHT_PROVIDER settings keep working.
    """
    def __init__(self, token: str):
        self.token = token
        
    def search_flights(self, origin: str, destination: str, start_date: str) -> List[TransitOption]:
        if not self.token:
            print("[Skyscanner] Warning: API_TOKEN is missing. Returning empty flight list.")
            return []
            
        origin_sky, origin_entity = resolve_skyscanner_location(origin, self.token)
        dest_sky, dest_entity = resolve_skyscanner_location(destination, self.token)
        
        url = f"https://api.apify.com/v2/actors/johnvc~Google-Flights-Data-Scraper-Flight-and-Price-Search/run-sync-get-dataset-items?token={self.token}"
        
        payload = {
            "departure_id": origin_sky,
            "arrival_id": dest_sky,
            "outbound_date": start_date,
            "currency": "INR",
            "hl": "en",
            "gl": "in",
            "max_pages": 1
        }
        
        try:
            print(f"[Skyscanner] Searching flights from {origin} ({origin_sky}) to {destination} ({dest_sky}) on {start_date}...")
            response = requests.post(url, json=payload, timeout=45)
            response.raise_for_status()
            items = response.json()
            
            # The actor stores one dataset item per results page, with flat flight rows in "all_flights"
            if isinstance(items, dict):
                items = [items]
            results = []
            for item in items or []:
                if isinstance(item, dict):
                    results.extend(item.get("all_flights") or [])
            
            # If no results found, let's log the raw response structure for debugging
            if not results and items:
                print(f"[Skyscanner] Debug info - raw items type: {type(items)}")
                print(f"[Skyscanner] First element type: {type(items[0]) if isinstance(items, list) and len(items) > 0 else 'N/A'}")
                print(f"[Skyscanner] Preview: {str(items)[:600]}")

            transit_options = []

            for idx, result in enumerate(results[:2]):
                amount = result.get("price")
                duration = result.get("duration_minutes")
                if not amount or not duration:
                    continue

                origin_code = result.get("departure_airport") or origin_sky
                dest_code = result.get("arrival_airport") or dest_sky

                transit_options.append(
                    TransitOption(
                        id=f"flight_skyscanner_{idx+1}",
                        origin=f"{origin} ({origin_code})",
                        destination=f"{destination} ({dest_code})",
                        departure_time=result.get("departure_time") or "12:00",
                        arrival_time=result.get("arrival_time") or "18:00",
                        mode=TravelMode.FLIGHT,
                        duration_minutes=int(duration),
                        estimated_price=int(round(float(amount))),
                        carrier=result.get("airlines") or "Airline"
                    )
                )

            return transit_options
        except Exception as e:
            print(f"[Skyscanner] Flight search query failed: {e}")
            return []

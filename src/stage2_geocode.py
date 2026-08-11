# -*- coding: utf-8 -*-
"""
Stage 2 -- geocoding.

Every unique normalized address is resolved to a coordinate, cached forever,
and validated. The validation is the point of this stage: Google answers an
unresolvable Bnei Brak address with the *city centroid* and a 200 OK. Left
unfiltered, that silently stacks dozens of unrelated customers onto one
phantom building and quietly destroys the route with no error anywhere.

So APPROXIMATE is rejected outright, GEOMETRIC_CENTER is flagged, anything
landing on the city centroid is flagged regardless of its reported type, and
anything outside the bounding box is flagged. Flagged results go to a review
artifact for a human decision -- never auto-discarded, never replaced with a
substitute coordinate.

    python -m src.stage2_geocode                    Google (production)
    python -m src.stage2_geocode --provider nominatim   demo only, no key

Cache is keyed by provider + normalized address, so a second run makes zero
API requests.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from src.env import load_dotenv

GOOGLE_URL = "https://maps.googleapis.com/maps/api/geocode/json"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

# Google statuses that are worth retrying rather than recording as a failure.
RETRYABLE_STATUSES = {"OVER_QUERY_LIMIT", "UNKNOWN_ERROR"}


# --------------------------------------------------------------------------
# Geometry helpers
# --------------------------------------------------------------------------

def haversine_meters(a: tuple[float, float], b: tuple[float, float]) -> float:
    radius = 6_371_000.0
    lat1, lng1 = math.radians(a[0]), math.radians(a[1])
    lat2, lng2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlng = lat2 - lat1, lng2 - lng1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlng / 2) ** 2
    return 2 * radius * math.asin(math.sqrt(h))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def load_cache() -> dict:
    if config.GEOCODE_CACHE.exists():
        return json.loads(config.GEOCODE_CACHE.read_text(encoding="utf-8"))
    return {}


def save_cache(cache: dict) -> None:
    config.CACHE_DIRECTORY.mkdir(parents=True, exist_ok=True)
    config.GEOCODE_CACHE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def cache_key(provider: str, address: str) -> str:
    return f"{provider}|{address}"


# --------------------------------------------------------------------------
# Providers
# --------------------------------------------------------------------------

class GoogleProvider:
    """Production geocoder."""

    name = "google"

    def __init__(self) -> None:
        load_dotenv()
        self.api_key = os.environ.get(config.GOOGLE_API_KEY_ENV_NAME, "").strip()
        if not self.api_key:
            raise SystemExit(
                f"{config.GOOGLE_API_KEY_ENV_NAME} is empty in .env. Add the key "
                f"there, or use --provider nominatim for a keyless demo run."
            )

    def request(self, address: str) -> dict:
        query = f"{address}, {config.GEOCODE_CITY_SUFFIX}"
        # The key is passed as a parameter and never echoed into any artifact.
        response = requests.get(
            GOOGLE_URL,
            params={
                "address": query,
                "key": self.api_key,
                "language": config.GEOCODE_LANGUAGE,
                "region": config.GEOCODE_REGION,
                "components": "|".join(
                    f"{k}:{v}" for k, v in config.GEOCODE_COMPONENTS.items()
                ),
            },
            timeout=config.GEOCODE_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()

        status = payload.get("status", "UNKNOWN_ERROR")
        if status in RETRYABLE_STATUSES:
            raise TransientGeocodeError(status)
        if status != "OK" or not payload.get("results"):
            return {"status": status, "provider": self.name}

        top = payload["results"][0]
        geometry = top.get("geometry", {})
        location = geometry.get("location", {})
        return {
            "status": "OK",
            "provider": self.name,
            "lat": location.get("lat"),
            "lng": location.get("lng"),
            "formatted_address": top.get("formatted_address"),
            "location_type": geometry.get("location_type"),
            "partial_match": bool(top.get("partial_match", False)),
            "place_id": top.get("place_id"),
            "types": top.get("types", []),
            "result_count": len(payload["results"]),
        }


class NominatimProvider:
    """
    Keyless OSM geocoder, for demo runs only.

    Nominatim has no location_type field, so one is derived from what OSM
    actually matched. The mapping is deliberately strict: a result that did
    not match a house number is not treated as a rooftop hit.
    """

    name = "nominatim"

    def request(self, address: str) -> dict:
        query = f"{address}, {config.GEOCODE_CITY_SUFFIX}, ישראל"
        response = requests.get(
            NOMINATIM_URL,
            params={
                "q": query,
                "format": "jsonv2",
                "addressdetails": 1,
                "limit": 1,
                "countrycodes": "il",
                "accept-language": "he",
            },
            headers={"User-Agent": "bnei-brak-route-demo/1.0 (offline demo run)"},
            timeout=config.GEOCODE_REQUEST_TIMEOUT_SECONDS,
        )
        if response.status_code in (429, 503):
            raise TransientGeocodeError(f"HTTP {response.status_code}")
        response.raise_for_status()
        payload = response.json()

        if not payload:
            return {"status": "ZERO_RESULTS", "provider": self.name}

        top = payload[0]
        details = top.get("address", {})
        category = top.get("category") or top.get("class")

        if details.get("house_number"):
            location_type = "ROOFTOP"
        elif category == "highway" or top.get("type") in ("residential", "road"):
            location_type = "GEOMETRIC_CENTER"
        else:
            location_type = "APPROXIMATE"

        return {
            "status": "OK",
            "provider": self.name,
            "lat": float(top["lat"]),
            "lng": float(top["lon"]),
            "formatted_address": top.get("display_name"),
            "location_type": location_type,
            # No house number returned means OSM matched something broader
            # than the address that was asked for.
            "partial_match": not bool(details.get("house_number")),
            "place_id": top.get("place_id"),
            "types": [category, top.get("type")],
            "result_count": len(payload),
        }


class TransientGeocodeError(RuntimeError):
    """Retryable condition: rate limit, transient upstream failure."""


def build_provider(name: str):
    if name == "google":
        return GoogleProvider()
    if name == "nominatim":
        print("!! DEMO GEOCODER (Nominatim/OSM). Not for real customer data. !!\n")
        return NominatimProvider()
    raise SystemExit(f"Unknown provider: {name}")


# --------------------------------------------------------------------------
# Retry wrapper
# --------------------------------------------------------------------------

def geocode_with_retry(provider, address: str) -> dict:
    delay = config.GEOCODE_BACKOFF_BASE_SECONDS

    for attempt in range(1, config.GEOCODE_MAX_RETRIES + 1):
        try:
            result = provider.request(address)
            result["fetched_at"] = now_iso()
            return result
        except (TransientGeocodeError, requests.RequestException) as error:
            if attempt == config.GEOCODE_MAX_RETRIES:
                return {
                    "status": "REQUEST_FAILED",
                    "provider": provider.name,
                    "error": f"{type(error).__name__}: {error}",
                    "attempts": attempt,
                    "fetched_at": now_iso(),
                }
            # Full jitter, so a burst of retries does not resynchronize.
            sleep_for = min(delay, config.GEOCODE_BACKOFF_MAX_SECONDS)
            time.sleep(random.uniform(0, sleep_for))
            delay *= 2

    raise AssertionError("unreachable")


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------

def validate(result: dict) -> dict:
    """Attach validation verdict and reasons. Never mutates coordinates."""
    reasons: list[str] = []

    if result.get("status") != "OK":
        return {**result, "validation": "failed", "reasons": [result.get("status", "?")]}

    location_type = result.get("location_type")
    lat, lng = result.get("lat"), result.get("lng")

    if lat is None or lng is None:
        return {**result, "validation": "failed", "reasons": ["missing_coordinates"]}

    if location_type in config.GEOCODE_REJECTED_LOCATION_TYPES:
        reasons.append("location_type_approximate")
    if location_type in config.GEOCODE_SUSPICIOUS_LOCATION_TYPES:
        reasons.append("location_type_geometric_center")
    if result.get("partial_match"):
        reasons.append("partial_match")

    box = config.BNEI_BRAK_BBOX
    if not (box["min_lat"] <= lat <= box["max_lat"]
            and box["min_lng"] <= lng <= box["max_lng"]):
        reasons.append("outside_bounding_box")

    distance_to_centroid = haversine_meters((lat, lng), config.BNEI_BRAK_CENTROID)
    if distance_to_centroid <= config.CENTROID_SUSPICION_RADIUS_METERS:
        # The classic silent failure: a "successful" geocode that is really
        # the city centroid standing in for an address Google could not find.
        reasons.append("on_city_centroid")

    if "location_type_approximate" in reasons or "on_city_centroid" in reasons:
        verdict = "rejected"
    elif reasons:
        verdict = "review"
    else:
        verdict = "valid"

    return {**result, "validation": verdict, "reasons": reasons,
            "meters_from_centroid": round(distance_to_centroid, 1)}


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------

def unique_addresses(records: list[dict]) -> list[str]:
    seen: dict[str, None] = {}
    for record in records:
        if record["geocodable"]:
            seen.setdefault(record["parsed"]["normalized_address"], None)
    return list(seen)


def build_report(results: dict, stats: dict) -> str:
    verdicts = {"valid": 0, "review": 0, "rejected": 0, "failed": 0}
    location_types: dict[str, int] = {}
    partial = 0
    outside = 0

    for result in results.values():
        verdicts[result["validation"]] = verdicts.get(result["validation"], 0) + 1
        key = result.get("location_type") or result.get("status", "?")
        location_types[key] = location_types.get(key, 0) + 1
        if result.get("partial_match"):
            partial += 1
        if "outside_bounding_box" in result.get("reasons", []):
            outside += 1

    lines = [
        "=" * 64,
        "שלב 2 - גיאוקודינג",
        "=" * 64,
        f"ספק                      : {stats['provider']}",
        f"סה\"כ כתובות ייחודיות     : {len(results)}",
        f"גיאוקוד תקין             : {verdicts.get('valid', 0)}",
        f"דורש בדיקה ידנית         : {verdicts.get('review', 0)}",
        f"נדחה (לא שמיש)           : {verdicts.get('rejected', 0)}",
        f"נכשל                     : {verdicts.get('failed', 0)}",
        f"APPROXIMATE              : {location_types.get('APPROXIMATE', 0)}",
        f"GEOMETRIC_CENTER         : {location_types.get('GEOMETRIC_CENTER', 0)}",
        f"ROOFTOP                  : {location_types.get('ROOFTOP', 0)}",
        f"RANGE_INTERPOLATED       : {location_types.get('RANGE_INTERPOLATED', 0)}",
        f"התאמה חלקית              : {partial}",
        f"מחוץ לגבולות בני ברק     : {outside}",
        f"פגיעות במטמון            : {stats['cache_hits']}",
        f"בקשות API בפועל          : {stats['api_calls']}",
        "",
    ]

    problems = [(a, r) for a, r in results.items() if r["validation"] != "valid"]
    if problems:
        lines.append("כתובות לבדיקה:")
        for address, result in problems:
            lines.append(
                f"   {address:<28} [{result['validation']}] "
                f"{', '.join(result.get('reasons', []))}"
            )
    lines.append("=" * 64)
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", default="google",
                        choices=("google", "nominatim"))
    args = parser.parse_args()

    if not config.STAGE1_PARSED.exists():
        raise SystemExit("Run stage 1 first.")

    payload = json.loads(config.STAGE1_PARSED.read_text(encoding="utf-8"))
    addresses = unique_addresses(payload["records"])

    provider = build_provider(args.provider)
    cache = load_cache()
    results: dict[str, dict] = {}
    stats = {"provider": args.provider, "cache_hits": 0, "api_calls": 0}

    for position, address in enumerate(addresses, start=1):
        key = cache_key(args.provider, address)
        if key in cache:
            results[address] = cache[key]
            stats["cache_hits"] += 1
            continue

        raw = geocode_with_retry(provider, address)
        validated = validate(raw)
        cache[key] = validated
        results[address] = validated
        stats["api_calls"] += 1
        print(f"  [{position}/{len(addresses)}] {address} -> "
              f"{validated['validation']} ({validated.get('location_type', '-')})")
        # Save incrementally so an interrupted run never re-pays for work.
        save_cache(cache)
        time.sleep(config.GEOCODE_MIN_INTERVAL_SECONDS)

    save_cache(cache)
    config.OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)

    usable = {a: r for a, r in results.items() if r["validation"] in ("valid", "review")}
    failures = {a: r for a, r in results.items() if r["validation"] == "failed"}
    review = {a: r for a, r in results.items() if r["validation"] in ("review", "rejected")}

    config.STAGE2_RESULTS.write_text(
        json.dumps({"provider": args.provider, "results": usable},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    config.STAGE2_FAILURES.write_text(
        json.dumps(failures, ensure_ascii=False, indent=2), encoding="utf-8")
    config.STAGE2_REVIEW.write_text(
        json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")

    report = build_report(results, stats)
    config.STAGE2_REPORT.write_text(report, encoding="utf-8")
    print("\n" + report)


if __name__ == "__main__":
    main()

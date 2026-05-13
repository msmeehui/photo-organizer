#!/usr/bin/env python3
"""Reverse-geocode GPS coordinates in a scan JSONL using OpenStreetMap Nominatim.

Reads a JSONL produced by scan_photos.py, looks up city/country for each
unique GPS coord (rounded to ~1 km to maximise cache hits), and writes an
enriched JSONL with a `location` field added to records that have GPS.

Cache is persisted to <output_dir>/geocode_cache.json so re-runs are free.

Nominatim's free service requires:
  - A descriptive User-Agent header (we set one)
  - Max 1 request per second (we throttle)
  - No bulk/heavy use (this script is for personal photo libraries, which fits)

If your collection has many unique locations, consider using a paid geocoding
service instead — but for typical personal collections, Nominatim works fine.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Optional

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = "photo-organizer-skill/0.1 (personal photo library tool)"
RATE_LIMIT_SECONDS = 1.1  # Slightly above 1.0 to be safe


def _round_coord(lat: float, lon: float) -> tuple[float, float]:
    # ~1 km resolution at temperate latitudes — plenty fine for "what city is this"
    return (round(lat, 2), round(lon, 2))


def _load_cache(path: Path) -> dict[str, dict[str, Any]]:
    if path.exists():
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(path: Path, cache: dict[str, dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _lookup(lat: float, lon: float) -> Optional[dict[str, Any]]:
    try:
        import requests
    except ImportError:
        print(
            "error: 'requests' is not installed. Install with: pip install requests",
            file=sys.stderr,
        )
        sys.exit(2)

    try:
        r = requests.get(
            NOMINATIM_URL,
            params={"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 12},
            headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  geocode error for ({lat}, {lon}): {e}", file=sys.stderr)
        return None

    addr = data.get("address", {})
    city = (
        addr.get("city")
        or addr.get("town")
        or addr.get("village")
        or addr.get("hamlet")
        or addr.get("municipality")
        or addr.get("county")
        or addr.get("state")
    )
    country = addr.get("country")
    return {
        "city": city,
        "country": country,
        "display_name": data.get("display_name"),
    }


def _compose_label(loc: dict[str, Any]) -> str:
    parts = [loc.get("city"), loc.get("country")]
    return ", ".join(p for p in parts if p) or "Unknown location"


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="Input JSONL from scan_photos.py")
    p.add_argument("--output", "-o", required=True, help="Output JSONL path")
    p.add_argument(
        "--cache",
        default=None,
        help="Path to geocode cache JSON (default: alongside output)",
    )
    args = p.parse_args()

    input_path = Path(args.input)
    output_path = Path(args.output)
    cache_path = Path(args.cache) if args.cache else output_path.parent / "geocode_cache.json"

    cache = _load_cache(cache_path)

    # Load all records
    records = []
    with open(input_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    # Find unique coords needing lookup
    needed: set[tuple[float, float]] = set()
    for rec in records:
        if rec.get("gps_lat") is not None and rec.get("gps_lon") is not None:
            key = _round_coord(rec["gps_lat"], rec["gps_lon"])
            cache_key = f"{key[0]},{key[1]}"
            if cache_key not in cache:
                needed.add(key)

    print(
        f"{len(records)} records, {len(needed)} new GPS lookups needed "
        f"(cache hits: {sum(1 for r in records if r.get('gps_lat') is not None) - len(needed)})",
        file=sys.stderr,
    )

    # Look them up, with rate limiting
    last_request = 0.0
    for i, (lat, lon) in enumerate(sorted(needed), 1):
        elapsed = time.time() - last_request
        if elapsed < RATE_LIMIT_SECONDS:
            time.sleep(RATE_LIMIT_SECONDS - elapsed)
        loc = _lookup(lat, lon)
        last_request = time.time()
        cache_key = f"{lat},{lon}"
        cache[cache_key] = loc or {"city": None, "country": None, "display_name": None}
        if i % 10 == 0 or i == len(needed):
            print(f"  geocoded {i}/{len(needed)}", file=sys.stderr)
            _save_cache(cache_path, cache)

    _save_cache(cache_path, cache)

    # Write enriched output
    with open(output_path, "w", encoding="utf-8") as out:
        for rec in records:
            if rec.get("gps_lat") is not None and rec.get("gps_lon") is not None:
                key = _round_coord(rec["gps_lat"], rec["gps_lon"])
                cache_key = f"{key[0]},{key[1]}"
                loc = cache.get(cache_key)
                if loc and loc.get("city") or loc and loc.get("country"):
                    rec["location"] = {
                        "city": loc.get("city"),
                        "country": loc.get("country"),
                        "label": _compose_label(loc),
                    }
            out.write(json.dumps(rec) + "\n")

    print(f"wrote {len(records)} records to {output_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

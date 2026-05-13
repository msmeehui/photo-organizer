#!/usr/bin/env python3
"""Group photos into events.

Reads a JSONL of photo records (post-geocoding) and writes a JSONL of events.

Two-tier strategy (this is the heart of the script — read the rest of the file
through this lens):

  1. Folder-respect (default ON, --no-respect-folders to disable). For each
     photo, walk up its parent folders until we hit an "informative" name
     (not in a skip list of generic container folders, not a bare year).
     All photos sharing the same nearest informative folder become one event.
     This respects whatever organisation the user has already done — if they
     put photos in `fishing/`, that's one event regardless of how many days
     it spans.

  2. Time-gap fallback. Photos that aren't inside any informative folder fall
     through to time/distance clustering: a new event starts when consecutive
     photos are more than --gap-hours apart in time OR --gap-km apart in
     space. This handles loose photos in DCIM/Pictures/etc.

Safety hatch: even with folder-respect on, if a single folder's photos span
more than --folder-max-span-days, that folder is sub-clustered by time gap.
This catches dumping-ground folders like "Phone backup" that contain years of
unrelated photos.

The output is a JSONL where each line is one event:
  {
    "event_id": 0,
    "start": "2024-03-14T10:22:05",
    "end":   "2024-03-16T19:48:11",
    "count": 47,
    "dominant_location": {...},
    "parent_folder_hints": ["Vienna_trip", ...],
    "photo_paths": ["/path/a.jpg", ...],
    "live_photo_pair_count": 3,
    "event_folder": "/abs/path/to/the/folder/that/grouped/these"   (or null)
  }

The model is expected to use parent_folder_hints, dominant_location, and a
visual sample of photo_paths to choose a final folder name.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional


# Folders that are containers, not events. Names are case-folded for matching.
SKIP_FOLDERS = {
    "dcim", "pictures", "photos", "camera", "downloads", "desktop",
    "documents", "icloud", "icloud photos", "mobile uploads",
    "album", "albums", "image", "images", "media", "screenshots",
    "whatsapp", "whatsapp images", "telegram", "telegram images",
    "phone backup", "iphone", "android",
}


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _parse_ts(s: str) -> Optional[datetime]:
    """Parse ISO8601 and return a naive datetime (timezone stripped)."""
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except (ValueError, AttributeError):
        return None


def _is_informative_folder_name(name: str) -> bool:
    n = name.strip().lower()
    if not n:
        return False
    if n in SKIP_FOLDERS:
        return False
    if n.isdigit() and len(n) == 4:  # bare year like "2024"
        return False
    if len(n) < 2:
        return False
    return True


def _find_event_folder(photo_path: Path, source_roots: list[Path]) -> Optional[Path]:
    """Walk up from the photo's parent looking for the first informative folder.

    Stops (returns None) if we reach a source root without finding one — source
    roots are the user's chosen scan targets, never themselves event folders.
    Also returns None if we reach the filesystem root.
    """
    resolved_roots = {sr.resolve() for sr in source_roots}
    current = photo_path.parent.resolve()
    while True:
        if current in resolved_roots:
            return None
        if _is_informative_folder_name(current.name):
            return current
        if current.parent == current:  # filesystem root
            return None
        current = current.parent


def _dominant_location(photos: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    labels = Counter()
    by_label: dict[str, dict[str, Any]] = {}
    for p in photos:
        loc = p.get("location")
        if loc and loc.get("label"):
            labels[loc["label"]] += 1
            by_label[loc["label"]] = loc
    if not labels:
        return None
    most_common_label, _ = labels.most_common(1)[0]
    return by_label[most_common_label]


def _parent_folder_hints(photos: list[dict[str, Any]]) -> list[str]:
    """Return up to 5 most-common informative parent folder names from the photos."""
    counts = Counter()
    for p in photos:
        for folder in p.get("parent_folders", [])[:2]:
            f = folder.strip()
            if _is_informative_folder_name(f):
                counts[f] += 1
    return [name for name, _ in counts.most_common(5)]


def _time_gap_groups(
    records: list[dict[str, Any]], gap_hours: float, gap_km: float
) -> list[list[dict[str, Any]]]:
    """Pure time/distance gap clustering. Returns groups of records.

    Photos with no parseable timestamp form a single trailing 'undated' group.
    """
    dated: list[tuple[datetime, dict[str, Any]]] = []
    undated: list[dict[str, Any]] = []
    for r in records:
        ts = _parse_ts(r.get("timestamp", ""))
        if ts is None:
            undated.append(r)
        else:
            dated.append((ts, r))

    dated.sort(key=lambda x: x[0])

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    last_ts: Optional[datetime] = None
    last_gps: Optional[tuple[float, float]] = None
    gap = timedelta(hours=gap_hours)

    for ts, r in dated:
        split = False
        if last_ts is not None and (ts - last_ts) > gap:
            split = True
        if not split and last_gps is not None:
            lat = r.get("gps_lat")
            lon = r.get("gps_lon")
            if lat is not None and lon is not None:
                if _haversine_km(last_gps[0], last_gps[1], lat, lon) > gap_km:
                    split = True

        if split and current:
            groups.append(current)
            current = []

        current.append(r)
        last_ts = ts
        if r.get("gps_lat") is not None and r.get("gps_lon") is not None:
            last_gps = (r["gps_lat"], r["gps_lon"])

    if current:
        groups.append(current)
    if undated:
        groups.append(undated)

    return groups


def _build_event(event_id: int, photos: list[dict[str, Any]], event_folder: Optional[str]) -> dict[str, Any]:
    timestamps = [_parse_ts(p.get("timestamp", "")) for p in photos]
    timestamps = [t for t in timestamps if t]
    live_pairs = {p.get("live_photo_pair") for p in photos if p.get("live_photo_pair")}
    return {
        "event_id": event_id,
        "start": min(timestamps).isoformat() if timestamps else None,
        "end": max(timestamps).isoformat() if timestamps else None,
        "count": len(photos),
        "dominant_location": _dominant_location(photos),
        "parent_folder_hints": _parent_folder_hints(photos),
        "photo_paths": [p["path"] for p in photos],
        "live_photo_pair_count": len(live_pairs),
        "is_undated": not timestamps,
        "event_folder": event_folder,
    }


def _span_days(records: list[dict[str, Any]]) -> Optional[int]:
    timestamps = [_parse_ts(r.get("timestamp", "")) for r in records]
    timestamps = [t for t in timestamps if t]
    if not timestamps:
        return None
    return (max(timestamps) - min(timestamps)).days


def cluster(
    records: list[dict[str, Any]],
    gap_hours: float,
    gap_km: float,
    respect_folders: bool = True,
    folder_max_span_days: int = 180,
    source_roots: Optional[list[Path]] = None,
) -> list[dict[str, Any]]:
    source_roots = source_roots or []

    if not respect_folders:
        groups = _time_gap_groups(records, gap_hours, gap_km)
        return [_build_event(i, photos, None) for i, photos in enumerate(groups)]

    # Bucket by nearest informative folder
    by_folder: dict[Path, list[dict[str, Any]]] = defaultdict(list)
    unstructured: list[dict[str, Any]] = []
    for r in records:
        ev_folder = _find_event_folder(Path(r["path"]), source_roots)
        if ev_folder is not None:
            by_folder[ev_folder].append(r)
        else:
            unstructured.append(r)

    out: list[dict[str, Any]] = []
    next_id = 0

    # Process folder-grouped photos in deterministic order
    for folder_path in sorted(by_folder.keys(), key=lambda p: str(p).lower()):
        recs = by_folder[folder_path]
        span = _span_days(recs)
        if span is not None and span > folder_max_span_days:
            # Folder spans too long — sub-cluster by time gap
            sub_groups = _time_gap_groups(recs, gap_hours, gap_km)
            for sg in sub_groups:
                out.append(_build_event(next_id, sg, str(folder_path)))
                next_id += 1
        else:
            out.append(_build_event(next_id, recs, str(folder_path)))
            next_id += 1

    # Unstructured photos go through pure time-gap clustering
    if unstructured:
        for sg in _time_gap_groups(unstructured, gap_hours, gap_km):
            out.append(_build_event(next_id, sg, None))
            next_id += 1

    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="Input JSONL (post-geocoding)")
    p.add_argument("--output", "-o", required=True, help="Output JSONL of events")
    p.add_argument(
        "--source-root",
        action="append",
        default=[],
        help="Source root folder(s) — never treated as event folders. Pass once per root.",
    )
    p.add_argument(
        "--gap-hours",
        type=float,
        default=168.0,
        help="Time gap that splits events in time-gap clustering (default 168h = 1 week)",
    )
    p.add_argument(
        "--gap-km",
        type=float,
        default=50.0,
        help="Distance gap that splits events in time-gap clustering (default 50km)",
    )
    p.add_argument(
        "--no-respect-folders",
        dest="respect_folders",
        action="store_false",
        help="Disable folder-as-event grouping; use pure time-gap clustering only",
    )
    p.add_argument(
        "--folder-max-span-days",
        type=int,
        default=180,
        help="Folder-as-event safety hatch: sub-cluster by time gap if photos in a folder span more than this many days (default 180 = ~6 months)",
    )
    args = p.parse_args()

    records = []
    with open(args.input, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    source_roots = [Path(s).expanduser().resolve() for s in args.source_root]

    events = cluster(
        records,
        gap_hours=args.gap_hours,
        gap_km=args.gap_km,
        respect_folders=args.respect_folders,
        folder_max_span_days=args.folder_max_span_days,
        source_roots=source_roots,
    )

    with open(args.output, "w", encoding="utf-8") as out:
        for ev in events:
            out.write(json.dumps(ev) + "\n")

    mode = "folder-respect" if args.respect_folders else "time-gap only"
    print(
        f"clustered {len(records)} photos into {len(events)} events "
        f"(mode={mode}, gap_hours={args.gap_hours}, folder_max_span_days={args.folder_max_span_days})",
        file=sys.stderr,
    )
    for ev in events[:15]:
        loc = ev.get("dominant_location") or {}
        folder = ev.get("event_folder")
        folder_label = Path(folder).name if folder else "(unstructured)"
        print(
            f"  event {ev['event_id']:>4}: {ev['count']:>4} photos, "
            f"{ev.get('start', 'undated')[:10] if ev.get('start') else 'undated':<10} → "
            f"{ev.get('end', '')[:10] if ev.get('end') else '':<10}  "
            f"[{folder_label}]  {loc.get('label', '(no location)')}",
            file=sys.stderr,
        )
    if len(events) > 15:
        print(f"  ... and {len(events) - 15} more", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

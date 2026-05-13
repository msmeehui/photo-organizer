#!/usr/bin/env python3
"""Build a move plan from clustered events plus naming decisions.

Inputs:
  events.jsonl    — output of cluster_events.py
  naming.json     — { "events": [{"event_id": 0, "folder_name": "..."}, ...] }
                    Folder names should NOT include the "YYYY/" prefix; the
                    script puts the year folder on automatically based on the
                    event's start date. If you provide a name like
                    "2024-03 - Vienna, Austria - City trip", that's perfect.

Output:
  move_plan.json  — full source→destination list with these fields:
                    {
                      "destination_root": "/abs/path",
                      "moves": [{"source": "...", "destination": "..."}, ...],
                      "summary": {"events": N, "files": M},
                      "tree": "...pretty-printed text tree..."
                    }

Side effect:
  Prints the text tree to stdout so the user can review.

Behaviours:
  - Live Photo pairs (HEIC + MOV with the same basename) are kept together.
  - Filename collisions within a destination folder get a " (2)", " (3)" suffix.
  - Events with no naming decision get a placeholder name and are flagged.
  - Undated events go into "Undated/" at the destination root.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


SAFE_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _sanitise(name: str) -> str:
    name = SAFE_NAME_RE.sub("_", name).strip().rstrip(". ")
    return name or "Untitled"


def _year_from_start(start_iso: Optional[str]) -> Optional[str]:
    if not start_iso:
        return None
    try:
        return datetime.fromisoformat(start_iso.replace("Z", "+00:00")).strftime("%Y")
    except ValueError:
        return None


def _resolve_collision(used: set[str], desired: str) -> str:
    if desired not in used:
        used.add(desired)
        return desired
    stem, dot, ext = desired.rpartition(".")
    if not dot:
        stem, ext = desired, ""
    n = 2
    while True:
        candidate = f"{stem} ({n}){'.' + ext if ext else ''}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        n += 1


def build_plan(
    events: list[dict[str, Any]],
    naming: dict[int, str],
    dest_root: Path,
) -> tuple[list[dict[str, str]], dict[str, list[str]]]:
    """Returns (moves, tree_dict) where tree_dict[folder_path] = [filenames...]"""
    moves: list[dict[str, str]] = []
    tree: dict[str, list[str]] = defaultdict(list)
    used_per_dir: dict[str, set[str]] = defaultdict(set)

    for ev in events:
        event_id = ev["event_id"]
        is_undated = ev.get("is_undated", False)

        if is_undated:
            folder_relpath = "Undated"
        else:
            year = _year_from_start(ev.get("start")) or "Unknown-Year"
            base_name = naming.get(event_id)
            if not base_name:
                # Fall back to a placeholder so the user can see it in the tree
                start = ev.get("start", "")[:7] if ev.get("start") else "????-??"
                loc = (ev.get("dominant_location") or {}).get("label", "Unknown location")
                base_name = f"{start} - {loc} - UNNAMED"
            folder_relpath = f"{year}/{_sanitise(base_name)}"

        folder_abs = dest_root / folder_relpath

        for src in ev["photo_paths"]:
            src_path = Path(src)
            desired = src_path.name
            final = _resolve_collision(used_per_dir[folder_relpath], desired)
            dest_path = folder_abs / final
            moves.append({"source": str(src_path), "destination": str(dest_path)})
            tree[folder_relpath].append(final)

    return moves, tree


def _format_tree(tree: dict[str, list[str]], max_files_per_folder: int = 5) -> str:
    """Pretty-print the proposed structure as a text tree."""
    lines = []
    # Sort folders by path so years group naturally
    for folder in sorted(tree.keys()):
        files = tree[folder]
        lines.append(f"{folder}/  ({len(files)} files)")
        for f in files[:max_files_per_folder]:
            lines.append(f"    {f}")
        if len(files) > max_files_per_folder:
            lines.append(f"    ... and {len(files) - max_files_per_folder} more")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("events", help="Events JSONL from cluster_events.py")
    p.add_argument("naming", help="Naming decisions JSON (see docstring)")
    p.add_argument("--dest", required=True, help="Destination root folder")
    p.add_argument("--output", "-o", required=True, help="Output move plan JSON")
    p.add_argument("--max-files-shown", type=int, default=5, help="Files per folder shown in preview tree")
    p.add_argument(
        "--dedup",
        help="Path to dedup.json from dedup.py. Non-keep paths from exact-duplicate "
             "and (optionally) perceptual groups will be excluded from the plan.",
    )
    p.add_argument(
        "--dedup-include",
        choices=("exact", "exact+perceptual"),
        default="exact",
        help="Which dedup categories to enforce (default: exact only — perceptual is "
             "more aggressive and may skip photos you want to keep).",
    )
    args = p.parse_args()

    events = []
    with open(args.events, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                events.append(json.loads(line))

    with open(args.naming, encoding="utf-8") as f:
        naming_doc = json.load(f)
    naming = {item["event_id"]: item["folder_name"] for item in naming_doc.get("events", [])}

    # Build set of paths to skip from dedup
    skipped_dupes: set[str] = set()
    if args.dedup:
        with open(args.dedup, encoding="utf-8") as f:
            dedup_doc = json.load(f)
        groups = list(dedup_doc.get("exact_duplicate_groups", []))
        if args.dedup_include == "exact+perceptual":
            groups.extend(dedup_doc.get("perceptual_duplicate_groups", []))
        for g in groups:
            keep = g.get("keep")
            for p_ in g.get("paths", []):
                if p_ != keep:
                    skipped_dupes.add(p_)
        # Filter events' photo_paths
        for ev in events:
            ev["photo_paths"] = [p_ for p_ in ev["photo_paths"] if p_ not in skipped_dupes]

    dest_root = Path(args.dest).expanduser().resolve()

    moves, tree = build_plan(events, naming, dest_root)
    tree_text = _format_tree(tree, args.max_files_shown)

    plan = {
        "destination_root": str(dest_root),
        "moves": moves,
        "summary": {
            "events": len(events),
            "files": len(moves),
            "skipped_duplicates": len(skipped_dupes),
        },
        "tree": tree_text,
        "skipped_duplicate_paths": sorted(skipped_dupes),
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(plan, f, indent=2)

    # Print tree for the user
    print(f"Proposed structure ({len(moves)} files into {len(tree)} folders under {dest_root}):\n")
    print(tree_text)
    if skipped_dupes:
        print(f"\nSkipped {len(skipped_dupes)} duplicate file(s) (see skipped_duplicate_paths in plan).")
    print(f"\nMove plan written to {args.output}")
    print("Nothing has been moved yet. Run execute_moves.py to apply the plan.")

    # Warn about unnamed events
    unnamed = [ev for ev in events if not ev.get("is_undated") and ev["event_id"] not in naming]
    if unnamed:
        print(f"\nWarning: {len(unnamed)} event(s) have no name and got 'UNNAMED' placeholders.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())

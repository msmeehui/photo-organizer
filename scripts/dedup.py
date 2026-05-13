#!/usr/bin/env python3
"""Detect duplicate photos in a scan JSONL.

Three kinds of duplicates are reported:

  1. Exact duplicates — same MD5 hash of file bytes. Most reliable: these are
     literally the same file in two places.
  2. Same-filename duplicates — same filename in different folders. Usually but
     not always genuine duplicates (e.g. "DSC_0012.JPG" from two different
     cameras would falsely match). Used for triage, not deletion.
  3. Perceptual duplicates — visually similar images (different filenames,
     different bytes — e.g. resized/recompressed for sharing). Requires the
     `imagehash` library; gracefully skipped if not installed.

For each group, a `keep` recommendation is chosen heuristically:
  - Prefer the path inside an "informative" folder (not DCIM/Pictures/etc)
  - Among informative folders, prefer the one with more nesting (more specific)
  - Tie-break by largest file size (fuller-resolution version)

Output JSON shape:
  {
    "exact_duplicate_groups":   [{"paths": [...], "keep": "...", "type": "exact"}, ...],
    "same_filename_groups":     [{"paths": [...], "keep": "...", "type": "same_filename"}, ...],
    "perceptual_duplicate_groups": [{"paths": [...], "keep": "...", "type": "perceptual"}, ...],
    "summary": {"total_files_scanned": N, "exact_duplicate_groups": N,
                "redundant_files_if_dedup": N, "same_filename_groups": N,
                "perceptual_groups": N}
  }

The skill (or the user) decides what to do with the duplicates. The default
recommendation is to skip the non-keep paths during the move/copy step, but you
can also collect them in a `Duplicates/` folder for review.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional


# Reused from cluster_events — folders that aren't real event names.
_SKIP_FOLDERS = {
    "dcim", "pictures", "photos", "camera", "downloads", "desktop",
    "documents", "icloud", "icloud photos", "mobile uploads",
    "album", "albums", "image", "images", "media", "screenshots",
    "whatsapp", "whatsapp images", "telegram", "telegram images",
    "phone backup", "iphone", "android",
    "backup", "backup picasa webalbum", "share online",  # common dupe-source folder names
}


def _is_informative(name: str) -> bool:
    n = name.strip().lower()
    if not n or len(n) < 2:
        return False
    if n in _SKIP_FOLDERS:
        return False
    if n.isdigit() and len(n) == 4:  # bare year
        return False
    return True


def _md5_file(path: Path, chunk_size: int = 65536) -> Optional[str]:
    h = hashlib.md5()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError as e:
        print(f"  read error {path}: {e}", file=sys.stderr)
        return None


def _keep_recommendation(records_by_path: dict[str, dict[str, Any]], paths: list[str]) -> str:
    """Pick the path to keep from a group of duplicates.

    Heuristic: highest score wins, where the score is
      (informative folder depth, file size, lexical path order for tiebreak).

    "Informative folder depth" counts how many of the path's parent folders
    have names that aren't generic containers — so a photo in
    `2018/Vienna trip/photo.jpg` scores higher than one in `Backup/photo.jpg`.
    """
    def score(path: str) -> tuple:
        rec = records_by_path.get(path, {})
        size = rec.get("size_bytes", 0)
        parents = rec.get("parent_folders", [])
        informative_count = sum(1 for n in parents if _is_informative(n))
        return (informative_count, size, -len(path))
    return max(paths, key=score)


def _try_perceptual(records: list[dict[str, Any]]) -> list[list[str]]:
    """Group images by perceptual hash. Returns groups of >= 2 paths each.

    Uses average-hash with a Hamming distance threshold; this catches resized
    copies, mild recompression, and small crops. Skipped silently if the
    `imagehash` library isn't installed.
    """
    try:
        import imagehash
        from PIL import Image
    except ImportError:
        print("note: imagehash not installed; skipping perceptual dedup. "
              "Install with: pip install imagehash", file=sys.stderr)
        return []

    HASH_BITS = 64  # default for average_hash
    HAMMING_THRESHOLD = 4  # ~6% difference; tolerant of resize/recompress

    hashes: list[tuple] = []  # (imagehash, path)
    for r in records:
        ext = r.get("ext", "")
        if ext not in {"jpg", "jpeg", "png", "heic", "heif", "webp", "tiff", "tif", "bmp", "gif"}:
            continue
        try:
            with Image.open(r["path"]) as img:
                h = imagehash.average_hash(img)
            hashes.append((h, r["path"]))
        except Exception as e:
            print(f"  phash error {r['path']}: {e}", file=sys.stderr)

    # O(n^2) — fine for typical personal libraries up to a few thousand photos.
    # For larger collections, would want a BK-tree or LSH index.
    visited: set[str] = set()
    groups: list[list[str]] = []
    for i, (h1, p1) in enumerate(hashes):
        if p1 in visited:
            continue
        group = [p1]
        visited.add(p1)
        for j in range(i + 1, len(hashes)):
            h2, p2 = hashes[j]
            if p2 in visited:
                continue
            if (h1 - h2) <= HAMMING_THRESHOLD:
                group.append(p2)
                visited.add(p2)
        if len(group) > 1:
            groups.append(group)
    return groups


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("input", help="scan JSONL")
    p.add_argument("--output", "-o", required=True, help="Output dedup JSON")
    p.add_argument(
        "--no-content-hash",
        action="store_true",
        help="Skip MD5 hashing — much faster but only finds same-filename dupes",
    )
    p.add_argument(
        "--perceptual",
        action="store_true",
        help="Also do perceptual-hash dedup (catches resized/recompressed copies). "
             "Slower; requires imagehash library.",
    )
    args = p.parse_args()

    records = []
    with open(args.input, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                records.append(json.loads(line))

    by_path = {r["path"]: r for r in records}

    # 1. Same-filename groups (cheap)
    by_filename: dict[str, list[str]] = defaultdict(list)
    for r in records:
        by_filename[r["filename"]].append(r["path"])
    same_filename_groups = [paths for paths in by_filename.values() if len(paths) > 1]

    # 2. Exact-byte duplicates: group by size first (cheap), then hash colliders
    exact_groups: list[list[str]] = []
    if not args.no_content_hash:
        by_size: dict[int, list[str]] = defaultdict(list)
        for r in records:
            by_size[r["size_bytes"]].append(r["path"])
        size_collisions = [paths for paths in by_size.values() if len(paths) > 1]
        if size_collisions:
            print(f"hashing {sum(len(p) for p in size_collisions)} files in {len(size_collisions)} size-collision groups...", file=sys.stderr)
        for paths in size_collisions:
            by_hash: dict[str, list[str]] = defaultdict(list)
            for path in paths:
                h = _md5_file(Path(path))
                if h:
                    by_hash[h].append(path)
            for group in by_hash.values():
                if len(group) > 1:
                    exact_groups.append(group)

    # 3. Perceptual-hash duplicates (optional, expensive)
    perceptual_groups: list[list[str]] = []
    if args.perceptual:
        # Skip files we already know are exact duplicates — no point pHashing them
        already_in_exact = {p for g in exact_groups for p in g}
        candidates = [r for r in records if r["path"] not in already_in_exact]
        perceptual_groups = _try_perceptual(candidates)

    output = {
        "exact_duplicate_groups": [
            {"paths": g, "keep": _keep_recommendation(by_path, g), "type": "exact"}
            for g in exact_groups
        ],
        "same_filename_groups": [
            {"paths": g, "keep": _keep_recommendation(by_path, g), "type": "same_filename"}
            for g in same_filename_groups
        ],
        "perceptual_duplicate_groups": [
            {"paths": g, "keep": _keep_recommendation(by_path, g), "type": "perceptual"}
            for g in perceptual_groups
        ],
        "summary": {
            "total_files_scanned": len(records),
            "exact_duplicate_groups": len(exact_groups),
            "redundant_files_if_dedup": sum(len(g) - 1 for g in exact_groups),
            "same_filename_groups": len(same_filename_groups),
            "perceptual_groups": len(perceptual_groups),
        },
    }

    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2)

    s = output["summary"]
    print(f"scanned {s['total_files_scanned']} files", file=sys.stderr)
    print(f"  exact-duplicate groups: {s['exact_duplicate_groups']} (could remove {s['redundant_files_if_dedup']} redundant files)", file=sys.stderr)
    print(f"  same-filename groups:   {s['same_filename_groups']} (overlap with exact-duplicate groups is normal)", file=sys.stderr)
    if args.perceptual:
        print(f"  perceptual groups:      {s['perceptual_groups']}", file=sys.stderr)
    print(f"wrote {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())

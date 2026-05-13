#!/usr/bin/env python3
"""Reverse a run by reading its undo log and moving (or deleting) files back.

Reads the undo log written by execute_moves.py and, for each entry:
  - If the original run was a "move", moves files back to their source paths.
  - If the original run was a "copy", deletes the destination copies and
    leaves the originals where they were.

Refuses to overwrite anything: if a source path now exists at the original
location (e.g., the user added a new file there), the conflict is reported
and that entry is skipped. After completion, the undo log is renamed with
a ".applied" suffix so it can't be accidentally re-applied.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("undo_log", help="Path to .photo-organizer-undo-*.json")
    p.add_argument("--dry-run", action="store_true", help="Print actions but don't touch files")
    args = p.parse_args()

    log_path = Path(args.undo_log)
    with open(log_path, encoding="utf-8") as f:
        log = json.load(f)

    action = log.get("action", "move")
    entries = log.get("entries", [])

    print(f"undoing {len(entries)} {action}(s) from {log_path}")

    conflicts = 0
    missing = 0
    done = 0

    # Reverse order so we restore the most-recently-moved first
    for entry in reversed(entries):
        src = Path(entry["source"])  # original location
        dst = Path(entry["destination"])  # current location

        if action == "copy":
            # Just delete the copy, don't touch the original
            if not dst.exists():
                missing += 1
                continue
            if args.dry_run:
                print(f"  would delete {dst}")
            else:
                dst.unlink()
            done += 1
            continue

        # move case
        if not dst.exists():
            missing += 1
            print(f"  missing (already moved/deleted?): {dst}", file=sys.stderr)
            continue
        if src.exists():
            conflicts += 1
            print(f"  conflict (original location now occupied): {src}", file=sys.stderr)
            continue
        if args.dry_run:
            print(f"  would move {dst} → {src}")
        else:
            src.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(src))
        done += 1

    if not args.dry_run and done > 0 and conflicts == 0 and missing == 0:
        # Mark log as applied
        applied = log_path.with_suffix(log_path.suffix + ".applied")
        log_path.rename(applied)
        print(f"undo complete; log renamed to {applied}")
    else:
        print(
            f"undo finished: {done} reversed, {conflicts} conflict(s), {missing} missing. "
            f"Log left in place at {log_path}."
        )

    return 0 if conflicts == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

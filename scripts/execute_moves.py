#!/usr/bin/env python3
"""Execute a move plan, with full undo log.

Reads move_plan.json (output of plan_moves.py) and either moves or copies
each file to its destination. Writes an undo log next to the destination
root so the entire run can be reversed in one command.

Usage:
  python execute_moves.py move_plan.json            # move (default)
  python execute_moves.py move_plan.json --copy     # copy instead of move

Safety properties:
  - Refuses to overwrite an existing destination file (error and abort early).
    If you want to re-run after a partial run, delete the destination tree
    first or use a fresh destination root.
  - Creates parent folders as needed.
  - Writes undo log incrementally (one entry per move) so an interrupted
    run can still be partially undone.
  - Handles cross-filesystem moves (uses shutil.move, which falls back to
    copy+delete when rename across filesystems fails).
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("plan", help="Move plan JSON from plan_moves.py")
    p.add_argument("--copy", action="store_true", help="Copy instead of move")
    p.add_argument("--undo-log", default=None, help="Path for undo log (default: under destination root)")
    p.add_argument("--dry-run", action="store_true", help="Print actions but don't touch files")
    args = p.parse_args()

    with open(args.plan, encoding="utf-8") as f:
        plan = json.load(f)

    dest_root = Path(plan["destination_root"])
    moves = plan["moves"]

    # Pre-flight: check no destinations already exist
    conflicts = []
    for m in moves:
        if Path(m["destination"]).exists():
            conflicts.append(m["destination"])
    if conflicts:
        print(
            f"error: {len(conflicts)} destination file(s) already exist. Aborting before any changes.",
            file=sys.stderr,
        )
        for c in conflicts[:5]:
            print(f"  {c}", file=sys.stderr)
        if len(conflicts) > 5:
            print(f"  ... and {len(conflicts) - 5} more", file=sys.stderr)
        return 2

    # Pre-flight: check all sources exist
    missing = [m["source"] for m in moves if not Path(m["source"]).exists()]
    if missing:
        print(f"error: {len(missing)} source file(s) missing. Aborting.", file=sys.stderr)
        for s in missing[:5]:
            print(f"  {s}", file=sys.stderr)
        return 2

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    if args.undo_log:
        log_path = Path(args.undo_log)
    else:
        dest_root.mkdir(parents=True, exist_ok=True)
        log_path = dest_root / f".photo-organizer-undo-{timestamp}.json"

    action = "copy" if args.copy else "move"
    action_past = "copied" if args.copy else "moved"
    action_ger = "copying" if args.copy else "moving"
    if args.dry_run:
        print(f"dry-run: would {action} {len(moves)} file(s) and write log to {log_path}")
        return 0

    log_entries: list[dict[str, str]] = []
    log_meta = {
        "created_at": datetime.now().isoformat(),
        "action": action,
        "destination_root": str(dest_root),
        "entries": log_entries,
    }

    def _flush_log():
        tmp = log_path.with_suffix(log_path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(log_meta, f, indent=2)
        tmp.replace(log_path)

    done = 0
    try:
        for m in moves:
            src = Path(m["source"])
            dst = Path(m["destination"])
            dst.parent.mkdir(parents=True, exist_ok=True)
            if args.copy:
                shutil.copy2(src, dst)
            else:
                shutil.move(str(src), str(dst))
            log_entries.append({"source": str(src), "destination": str(dst)})
            done += 1
            if done % 50 == 0:
                _flush_log()
                print(f"  {action_past} {done}/{len(moves)}", file=sys.stderr)
        _flush_log()
    except Exception as e:
        _flush_log()
        print(f"error while {action_ger} after {done} file(s): {e}", file=sys.stderr)
        print(f"partial undo log written to {log_path}", file=sys.stderr)
        return 1

    print(f"{action_past} {done} file(s). Undo log: {log_path}")
    print(f"To reverse: python scripts/undo.py '{log_path}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())

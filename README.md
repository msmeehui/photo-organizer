# photo-organizer

A Claude skill for turning a chaotic photo collection into a tidy, browsable folder structure of the form:

```
2024/
  2024-03 - Vienna, Austria - City trip with Mary/
  2024-11 - La Roche, Belgium - Fishing weekend with Peter and James/
2025/
  2025-06 - Lisbon, Portugal - Family vacation/
```

This is an MVP sketch — the foundation on which a real product (a desktop app, eventually) can be built.

## What it does

- Walks one or more source folders and reads metadata (EXIF date, GPS, camera, dimensions) from JPEG, HEIC, PNG, and other common formats.
- Detects iPhone Live Photos (HEIC + MOV pairs with the same basename) and keeps them together.
- Reverse-geocodes GPS coordinates into city/country names via OpenStreetMap.
- Clusters photos into events using time gaps and location distance.
- Asks the model (Claude) to look at sample photos from each event and propose a folder name.
- Builds a **dry-run preview** of the proposed structure for the user to approve or edit.
- Executes moves only after approval, with a full undo log.

## What it does NOT do (yet)

- Duplicate detection (planned).
- Face recognition / person-name learning (planned for the next iteration).
- Distinguishing photos from screenshots/memes/saved images (planned).
- Quality assessment / blurry-shot pruning (planned).

## Install

```bash
pip install -r requirements.txt
```

`pillow-heif` is optional but strongly recommended — without it, HEIC files are scanned by name only (no EXIF date or GPS). Install it if you have iPhone photos.

### A note on running this inside Cowork vs. on your Mac

If you invoke this skill from inside Cowork, the geocoding step will likely fail with proxy/connection errors — the Cowork sandbox blocks outbound HTTPS to most domains, including `nominatim.openstreetmap.org`. The script handles this gracefully (events just end up without location labels), and the model can interpret GPS coordinates itself as a workaround, but the cleanest setup for serious use is to run the scripts directly in Terminal on your Mac. There's no sandbox there, no proxy, and Nominatim works fine.

## How to use it (as a Claude skill)

Drop the `photo-organizer/` folder into your Claude skills directory and invoke it by asking Claude to organize your photos. Example prompts that should trigger it:

- "Help me organize the photos in `~/Pictures/iPhone backup`"
- "My photo collection is a mess across three drives, can you sort it out?"
- "I want to clean up my photos folder into year/month/event folders"

Claude will walk through scan → geocode → cluster → name → preview → execute, asking for input at the right moments and never touching files until you approve the preview.

## How to use it manually (without Claude)

You can also run the scripts directly if you want full control:

```bash
# 1. Scan
python scripts/scan_photos.py ~/Pictures/iPhone --output /tmp/scan.jsonl

# 2. Geocode (optional but recommended)
python scripts/geocode.py /tmp/scan.jsonl --output /tmp/scan_geo.jsonl

# 3. Cluster into events
#    Default: folder-as-event grouping; named subfolders become single events,
#    loose photos fall back to time-gap clustering (default 1 week).
python scripts/cluster_events.py /tmp/scan_geo.jsonl \
    --source-root ~/Pictures/iPhone \
    --output /tmp/events.jsonl
# To use the older pure time-gap mode instead:
#   python scripts/cluster_events.py ... --no-respect-folders --gap-hours 168

# 4. Name the events yourself by writing /tmp/naming.json:
#    {"events": [{"event_id": 0, "folder_name": "2024-03 - Vienna, Austria - City trip"}]}

# 5. Build the move plan (preview)
python scripts/plan_moves.py /tmp/events.jsonl /tmp/naming.json \
    --dest ~/Pictures/Organized --output /tmp/plan.json

# 6. If you like the preview, execute
python scripts/execute_moves.py /tmp/plan.json
# or, to keep originals untouched:
python scripts/execute_moves.py /tmp/plan.json --copy

# 7. To undo:
python scripts/undo.py ~/Pictures/Organized/.photo-organizer-undo-*.json
```

## Architecture

The skill separates two concerns:

- **Plumbing** (Python scripts): walking files, parsing EXIF, clustering, moving files. Deterministic, fast, cheap.
- **Judgement** (Claude, via SKILL.md): looking at photo content, choosing folder names, asking the user the right clarifying questions.

This split keeps the model from burning tokens on tasks a 20-line script can do, and keeps the scripts from trying to make subjective calls they're bad at.

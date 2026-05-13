---
name: photo-organizer
description: Organize a messy photo library into folders like YYYY/YYYY-MM - Location - Activity/. Use whenever the user wants to sort, clean up, or consolidate their photos. Always shows a dry-run first.
---

# Photo Organizer

A skill for turning a messy photo collection into a structured archive of the form:

```
2024/
  2024-03 - Vienna, Austria - City trip with Mary/
  2024-11 - La Roche, Belgium - Fishing weekend with Peter and James/
2025/
  2025-06 - Lisbon, Portugal - Family vacation/
```

The skill combines deterministic plumbing (Python scripts that read EXIF, cluster events, move files) with judgement that only a model can do (looking at photo content, choosing a sensible activity name, asking the user the right clarifying questions).

## Workflow

Walk the user through these steps in order. Don't skip the preview — the whole point of this skill is that nothing moves until the user has approved the proposed structure.

### 1. Establish source and destination

Ask the user:
- Which folder(s) contain the photos to organize? (Can be multiple.)
- Where should the organized library live? (Can be the same root as the source, or a separate folder.)

If the user wants to organize in place, warn them that the originals will be moved (not copied). Offer the `--copy` mode of `execute_moves.py` if they'd rather keep the originals untouched and build the new structure as a copy.

### 2. Scan the photos

Run `scripts/scan_photos.py` over the source folder(s). This walks the tree, reads metadata from every photo, and writes a JSONL file to the workspace with one line per photo containing: path, timestamp, GPS coords (if any), camera info, dimensions, and any clues from the parent folder name and filename.

```bash
python scripts/scan_photos.py <source_folder> --output /tmp/photo_scan.jsonl
```

If pillow-heif isn't installed, HEIC files will be skipped with a warning. Tell the user to install it (`pip install pillow-heif`) if they have iPhone photos.

Live Photos (HEIC + MOV pairs with the same basename) are detected and tagged so they get moved together later.

**Date precedence:** scan_photos.py uses the following priority order to set each photo's timestamp, with `timestamp_source` recording which one won:
1. **Filename date prefix** (`YYYY-MM-DD - ...`, `YYYY-MM - ...`, or bare `YYYY - ...`) — the user has clearly stated the date by naming the file that way
2. **Parent folder date prefix** — same convention, but at folder level
3. **EXIF DateTimeOriginal** — what the camera recorded
4. **File mtime** — last resort, often wrong (it's the copy/import date for old photos)

The reason filename/folder beat EXIF: old scanned photos commonly have wrong EXIF (the scanner or import software wrote the *scan* date, not the original photo date), but the user's manual filenames or folder names are reliable. When the user has gone to the trouble of putting a date prefix on a file or folder, treat that as the source of truth.

### 3. Detect duplicates (recommended)

Run `scripts/dedup.py` to find files that are duplicates of each other. This catches three patterns:
- **Exact byte-level duplicates** — same MD5 hash. Always safe to skip.
- **Same-filename in different folders** — usually but not always genuine duplicates (e.g. `DSC_0012.JPG` could come from two cameras). Useful for triage, not blind deletion.
- **Perceptual duplicates** (optional, with `--perceptual`) — visually similar images with different bytes/filenames, e.g. resized for sharing. Requires the `imagehash` library.

```bash
python scripts/dedup.py /tmp/photo_scan.jsonl --output /tmp/dedup.json
# Optional, slower, requires `pip install imagehash`:
python scripts/dedup.py /tmp/photo_scan.jsonl --output /tmp/dedup.json --perceptual
```

Each duplicate group has a `keep` recommendation chosen by heuristic (path with the deepest informative folder, then largest file). The `plan_moves.py` step will use this to skip the non-keep paths automatically when you pass `--dedup`.

Show the user a brief summary: "I found N exact-duplicate groups (Y redundant files) and M same-filename groups." If the numbers are large, ask whether they want to skip duplicates entirely (default), put them in a `Duplicates/` folder for review, or include all copies in the move plan. For most cases, the default of skipping the non-keep paths is what they want.

### 4. Reverse-geocode GPS coordinates

Run `scripts/geocode.py` to turn GPS coordinates into human-readable place names (city, country) using OpenStreetMap's Nominatim service. Results are cached locally so re-runs are free.

```bash
python scripts/geocode.py /tmp/photo_scan.jsonl --output /tmp/photo_scan_geo.jsonl
```

This is rate-limited to 1 request per second per Nominatim's usage policy. For a few thousand unique locations it can take a couple of minutes.

**Sandbox/proxy note**: if you're running this skill from inside the Cowork sandbox, outbound calls to `nominatim.openstreetmap.org` are blocked by default and the script will print connection errors for every coord (it continues gracefully — events just end up without location labels). When that happens, tell the user that the events you're about to propose will lack location names, and offer two options: (a) you (the model) can interpret the GPS coords yourself based on geography knowledge and propose approximate location names with the suffix " (approx)", or (b) ask them to re-run the scripts directly in Terminal on their Mac for proper geocoding. On a normal Mac terminal there is no proxy and Nominatim works fine.

### 5. Cluster photos into events

Run `scripts/cluster_events.py` to group photos into events. The default mode is **folder-respect**: photos sharing the same nearest informative parent folder become one event regardless of time gaps, on the principle that user-applied folder organisation is high-quality signal. Photos that aren't inside any informative folder fall back to time-gap clustering (default 168h = 1 week). A safety hatch sub-clusters any folder whose photos span more than 180 days, so dumping-ground folders like "Phone backup" don't all collapse into one event.

```bash
python scripts/cluster_events.py /tmp/photo_scan_geo.jsonl \
    --source-root <source_folder> \
    --output /tmp/events.jsonl
```

Pass `--source-root` once per source folder you scanned in step 2. Source roots are never themselves treated as event folders — only their named subfolders are. If you skip `--source-root`, the script still works but may treat the source root itself as an event folder, grouping all loose photos at the top level into one event.

To disable folder-respect entirely (pure time-gap mode, the older behavior), pass `--no-respect-folders`.

Each event in the output has: id, start/end timestamps, photo count, dominant location, parent folder hints, the list of photo paths, and the `event_folder` (the folder that grouped these photos, or null if they were unstructured).

### 6. Name each event

This is the part where the model adds value. For each event:

- Read the existing parent folder name (from `event_folder` and `parent_folder_hints`) and filenames — these often contain rich clues ("Vienna_trip_March24", "Birthday-Sarah-2023", "Marc en Frans voor de fish and chips winkel.jpg"). Many users already organise and name their photos quite well; respect that signal.
- Consider the dominant location from geocoding.
- For events where the above is enough, propose the folder name immediately, no image read needed. **This should be the common case.** Most events with a sensible folder name and descriptive filenames don't need visual inspection.
- For events where filename + folder + location are insufficient (hash filenames, no folder hint, GPS only), look at a small sample of photos (3-5 representative ones, e.g. first, middle, last) using your vision capabilities to identify what's happening.
- Propose a folder name in the format `YYYY-MM - Location - Activity` where Activity is a short human description. If location or activity are unknown, omit that part rather than fabricating ("`YYYY-MM - Activity`" or "`YYYY-MM - Location`" are fine).

**Token-cost guard for visual inspection.** Image reads are the most expensive part of this workflow — each photo costs roughly 1500 tokens. Before doing any visual inspection, count how many events you'd want to look at. If that number is more than ~20, surface the cost to the user before proceeding:

> "I have 47 events I'd like to inspect visually because their filenames and folder hints aren't enough on their own. That's roughly 150K tokens of image reads. Want me to (a) proceed, (b) look at only the most uncertain events (~10), or (c) skip visual inspection entirely and propose names from metadata only?"

Below ~20 events, just do it without bothering to ask.

When unsure even after looking, ask the user. Examples of good questions:
- "I see 47 photos in your `Vienna_trip` folder from March 2024. The folder name and filenames suggest a city trip — was anyone with you whose name I should include?"
- "These 12 photos have no GPS, no useful filenames, and the file dates are all `2014-11-01` (which looks like a copy/export date). Any context — what year/event were these from?"

Don't ask one question at a time — bundle related questions together. With thousands of photos there will be many events; respect the user's time.

For events where you genuinely have no information beyond date and location, default to `YYYY-MM - Location - Untitled` and let the user rename later.

### 7. Build and present the dry-run preview

Compile your naming decisions into a JSON file like this:

```json
{
  "events": [
    {"event_id": 0, "folder_name": "2024-03 - Vienna, Austria - City trip with Mary"},
    {"event_id": 1, "folder_name": "2024-11 - La Roche, Belgium - Fishing weekend with Peter and James"}
  ]
}
```

Then run:

```bash
python scripts/plan_moves.py /tmp/events.jsonl /tmp/naming.json \
    --dest <destination_folder> \
    --dedup /tmp/dedup.json \
    --output /tmp/move_plan.json
```

The `--dedup` flag is optional but recommended — it makes plan_moves skip the non-keep paths from any duplicate groups, so the dry-run preview shows the deduplicated structure. Use `--dedup-include exact+perceptual` to also enforce perceptual duplicates if you ran dedup with `--perceptual`.

This produces `move_plan.json` (full source→destination list, plus a list of any skipped duplicate paths) and prints a summary tree. Show the tree to the user. Tell them: nothing has been moved yet — they can edit the proposed names by editing `/tmp/naming.json` and re-running `plan_moves.py`, or by telling you what to change and you'll re-run it for them.

### 8. Execute after approval

Once the user is happy:

```bash
python scripts/execute_moves.py /tmp/move_plan.json
```

This moves the files and writes an undo log to the destination folder (`.photo-organizer-undo-YYYYMMDD-HHMMSS.json`). Tell the user:
- Where the undo log was written
- That they can reverse the entire run with `python scripts/undo.py <undo_log_path>`
- The total number of files moved

If the user asked for `--copy` mode in step 1, pass `--copy` here too.

## Edge cases worth handling

**Photos with no timestamp at all.** Some screenshots, downloaded images, and scans have no EXIF date and the file mtime is meaningless. `scan_photos.py` falls back to mtime but flags these. In step 5, surface these to the user as a separate group: "I have N photos with no reliable date — want to put these in a `Undated/` folder for now?"

**Duplicates.** Handled by step 3 (`dedup.py`). Exact-byte and same-filename duplicates are detected by default; perceptual duplicates (resized/recompressed copies) require `pip install imagehash` and the `--perceptual` flag.

**Existing folder structure that already partly works.** If the source folder already has folders like "2023" or "Vienna trip", surface this in step 5 — those folder names are gold for naming events. Don't blindly destroy a structure the user has already built; treat it as another input signal.

**Mixed content (photos + screenshots + WhatsApp saves).** Out of scope for this MVP. If the user mentions wanting to separate these, note it as a potential future feature.

## Dependencies

The scripts need:
- Python 3.9+
- `Pillow` (almost always preinstalled)
- `pillow-heif` (for iPhone HEIC files — install with `pip install pillow-heif`)
- `requests` (for geocoding — usually preinstalled)
- `imagehash` (only for perceptual dedup with `--perceptual` — install with `pip install imagehash`)

If a dependency is missing, the relevant script prints a clear install instruction and exits cleanly rather than crashing.

## Why this architecture

The plumbing (walking files, parsing EXIF, clustering by time, moving files safely) is deterministic — Python does it faster, more reliably, and more cheaply than the model would. The intelligence (looking at a photo and deciding "this is a fishing trip, not a hike") is where the model earns its keep. Keeping these layers separate means the model isn't burning tokens on tasks that a 20-line script can do, and the scripts aren't trying to make subjective judgements they're bad at.

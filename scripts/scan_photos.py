#!/usr/bin/env python3
"""Walk a folder tree and emit one JSONL line per photo with metadata.

Output fields per photo:
  path             absolute path
  filename         basename
  parent_folders   last 3 folder names above the file (useful naming hints)
  size_bytes       file size
  ext              lowercase extension without the dot
  timestamp        ISO8601 datetime
  timestamp_source "filename" | "folder" | "exif" | "mtime"
                   priority: filename date > folder date > EXIF > mtime.
                   This matters because old scanned photos often have wrong EXIF
                   (the scanner overwrote it with the scan date) but correct
                   filenames or parent folders that the user named manually.
  gps_lat, gps_lon decimal degrees, or null
  camera_make      from EXIF, or null
  camera_model     from EXIF, or null
  width, height    pixels, or null
  live_photo_pair  basename without extension if a HEIC/MOV (or JPG/MOV) Live Photo pair was detected, else null
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

PHOTO_EXTS = {"jpg", "jpeg", "png", "heic", "heif", "tif", "tiff", "webp", "gif", "bmp"}
VIDEO_EXTS = {"mov", "mp4", "m4v"}  # included so Live Photo .MOV companions are picked up


def _try_register_heif() -> bool:
    try:
        import pillow_heif  # type: ignore
        pillow_heif.register_heif_opener()
        return True
    except Exception:
        return False


# Regex patterns for date extraction from filenames and folder names.
# Anchored to the start so we only match dates used as a prefix (the user's
# convention). Trailing context required so we don't grab "2024" from "20241".
_DATE_DAY = re.compile(r"^(\d{4})[-_](\d{1,2})[-_](\d{1,2})(?!\d)")
_DATE_MONTH = re.compile(r"^(\d{4})[-_](\d{1,2})(?!\d)")
# Bare-year (lowest precedence): year followed by any non-digit or end-of-string.
# Defaults to mid-year (June 15) since we know nothing more specific. Useful for
# old scanned photos in folders named "1977/" with filenames like
# "1977 - Marc en Karin.jpeg" — we get the right year folder in the destination
# without making up a fake month.
_DATE_YEAR = re.compile(r"^(\d{4})(?:\D|$)")


def _try_parse_date_prefix(s: str) -> Optional[tuple[datetime, bool]]:
    """Look for a date prefix in `s`. Returns (datetime, is_day_precision) or None.

    Tries day-precision first, then month-precision, then bare year as a last
    resort. Bare year and month-precision both use day=15 as a midpoint so
    events sort sensibly when we only know the year/month.
    """
    m = _DATE_DAY.match(s)
    if m:
        try:
            y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31:
                return datetime(y, mo, d), True
        except ValueError:
            pass
    m = _DATE_MONTH.match(s)
    if m:
        try:
            y, mo = int(m.group(1)), int(m.group(2))
            if 1900 <= y <= 2100 and 1 <= mo <= 12:
                return datetime(y, mo, 15), False
        except ValueError:
            pass
    m = _DATE_YEAR.match(s)
    if m:
        try:
            y = int(m.group(1))
            if 1900 <= y <= 2100:
                return datetime(y, 6, 15), False
        except ValueError:
            pass
    return None


def _extract_authoritative_date(filename: str, parent_folders: list[str]) -> Optional[tuple[datetime, str]]:
    """Try filename first, then walk up parent folders. Returns (datetime, source)
    where source is 'filename' or 'folder', or None if nothing matched.

    Walking up from immediate parent means a date in the photo's own folder takes
    precedence over a date further up — useful when both exist (e.g.
    `Trips/2024-Vienna/2024-03-14/IMG.jpg` would prefer 2024-03-14 over Vienna).
    """
    result = _try_parse_date_prefix(filename)
    if result is not None:
        return result[0], "filename"
    for folder in parent_folders[:3]:
        result = _try_parse_date_prefix(folder)
        if result is not None:
            return result[0], "folder"
    return None


def _parse_exif_datetime(s: str) -> Optional[datetime]:
    # EXIF datetimes look like "2024:03:14 18:22:05"
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y:%m:%d %H:%M:%S.%f"):
        try:
            return datetime.strptime(s.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def _gps_to_decimal(coord: tuple, ref: str) -> Optional[float]:
    try:
        d, m, s = coord
        # Pillow returns IFDRational; cast to float
        deg = float(d) + float(m) / 60.0 + float(s) / 3600.0
        if ref in ("S", "W"):
            deg = -deg
        return deg
    except Exception:
        return None


def _read_image_metadata(path: Path) -> dict[str, Any]:
    """Best-effort EXIF read. Returns partial dict; missing fields are absent."""
    out: dict[str, Any] = {}
    try:
        from PIL import Image, ExifTags
    except ImportError:
        return out

    try:
        with Image.open(path) as img:
            out["width"], out["height"] = img.size
            exif = img.getexif() if hasattr(img, "getexif") else None
            if not exif:
                return out

            tag_map = {ExifTags.TAGS.get(k, k): v for k, v in exif.items()}

            # Camera
            if "Make" in tag_map:
                out["camera_make"] = str(tag_map["Make"]).strip("\x00 ")
            if "Model" in tag_map:
                out["camera_model"] = str(tag_map["Model"]).strip("\x00 ")

            # Datetime
            dt_str = tag_map.get("DateTimeOriginal") or tag_map.get("DateTime")
            if dt_str:
                dt = _parse_exif_datetime(str(dt_str))
                if dt:
                    out["timestamp"] = dt.isoformat()
                    out["timestamp_source"] = "exif"

            # GPS — lives in a sub-IFD
            gps_ifd_id = next(
                (k for k, v in ExifTags.TAGS.items() if v == "GPSInfo"), None
            )
            if gps_ifd_id and gps_ifd_id in exif:
                try:
                    gps_data = exif.get_ifd(gps_ifd_id)
                    gps_tags = {ExifTags.GPSTAGS.get(k, k): v for k, v in gps_data.items()}
                    lat = gps_tags.get("GPSLatitude")
                    lat_ref = gps_tags.get("GPSLatitudeRef", "N")
                    lon = gps_tags.get("GPSLongitude")
                    lon_ref = gps_tags.get("GPSLongitudeRef", "E")
                    if lat and lon:
                        out["gps_lat"] = _gps_to_decimal(lat, lat_ref)
                        out["gps_lon"] = _gps_to_decimal(lon, lon_ref)
                except Exception:
                    pass

    except Exception as e:
        # Unreadable image — note but don't crash the scan
        out["_read_error"] = str(e)

    return out


def _read_mov_metadata(path: Path) -> dict[str, Any]:
    """Pull a creation timestamp from a .MOV/.MP4 if possible.

    QuickTime files store creation_time in the moov/mvhd atom. We try to read
    it via a tiny atom parse so we don't pull in ffmpeg as a dependency.
    """
    out: dict[str, Any] = {}
    try:
        with open(path, "rb") as f:
            data = f.read(64 * 1024)  # mvhd is usually near the start
        idx = data.find(b"mvhd")
        if idx == -1:
            return out
        # mvhd version (1 byte) + flags (3 bytes) + creation_time (4 or 8 bytes)
        version = data[idx + 4]
        if version == 1:
            ts_bytes = data[idx + 8 : idx + 16]
            ts = int.from_bytes(ts_bytes, "big")
        else:
            ts_bytes = data[idx + 8 : idx + 12]
            ts = int.from_bytes(ts_bytes, "big")
        # QuickTime epoch is 1904-01-01
        unix_ts = ts - 2082844800
        if 0 < unix_ts < 4_000_000_000:
            dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
            out["timestamp"] = dt.isoformat()
            out["timestamp_source"] = "exif"  # close enough — it's container metadata
    except Exception:
        pass
    return out


def _detect_live_photo_pairs(files: list[Path]) -> dict[str, str]:
    """Return {basename: pair_key} for files that are part of a Live Photo pair."""
    by_dir_basename: dict[tuple[Path, str], list[Path]] = {}
    for f in files:
        key = (f.parent, f.stem)
        by_dir_basename.setdefault(key, []).append(f)

    pairs: dict[str, str] = {}
    for (parent, stem), fs in by_dir_basename.items():
        exts = {f.suffix.lower().lstrip(".") for f in fs}
        # Live Photo: a still (HEIC or JPG) + a MOV with the same basename in the same folder
        if exts & {"heic", "heif", "jpg", "jpeg"} and exts & {"mov"}:
            pair_key = str(parent / stem)
            for f in fs:
                pairs[str(f)] = pair_key
    return pairs


def scan_folder(root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip hidden dirs and common junk
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in filenames:
            if name.startswith("."):
                continue
            ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
            if ext in PHOTO_EXTS or ext in VIDEO_EXTS:
                files.append(Path(dirpath) / name)
    return files


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", nargs="+", help="One or more source folders to scan")
    p.add_argument("--output", "-o", required=True, help="Output JSONL path")
    args = p.parse_args()

    have_heif = _try_register_heif()
    if not have_heif:
        print(
            "warning: pillow-heif not installed; HEIC files will be scanned by name "
            "only (no EXIF). Install with: pip install pillow-heif",
            file=sys.stderr,
        )

    all_files: list[Path] = []
    for src in args.source:
        srcp = Path(src).expanduser().resolve()
        if not srcp.is_dir():
            print(f"error: not a directory: {srcp}", file=sys.stderr)
            return 2
        all_files.extend(scan_folder(srcp))

    print(f"scanning {len(all_files)} files...", file=sys.stderr)
    pairs = _detect_live_photo_pairs(all_files)

    written = 0
    skipped_heic = 0
    with open(args.output, "w", encoding="utf-8") as out:
        for f in all_files:
            ext = f.suffix.lower().lstrip(".")
            try:
                stat = f.stat()
            except OSError:
                continue

            record: dict[str, Any] = {
                "path": str(f),
                "filename": f.name,
                "parent_folders": [p.name for p in list(f.parents)[:3]],
                "size_bytes": stat.st_size,
                "ext": ext,
            }

            meta: dict[str, Any] = {}
            if ext in PHOTO_EXTS:
                if ext in {"heic", "heif"} and not have_heif:
                    skipped_heic += 1
                else:
                    meta = _read_image_metadata(f)
            elif ext in VIDEO_EXTS:
                meta = _read_mov_metadata(f)

            record.update(meta)

            # Filename/folder date overrides EXIF if present. Old scanned photos
            # often have wrong EXIF (the scanner or import software overwrote it
            # with the scan/import date) but the user's manual filenames or
            # folder names are correct. When the user has gone to the trouble of
            # naming a file or folder with a date prefix, treat that as the
            # source of truth.
            authoritative = _extract_authoritative_date(f.name, record["parent_folders"])
            if authoritative is not None:
                dt, source = authoritative
                record["timestamp"] = dt.isoformat()
                record["timestamp_source"] = source
            elif "timestamp" not in record:
                # No EXIF either — fall back to file mtime
                dt = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
                record["timestamp"] = dt.isoformat()
                record["timestamp_source"] = "mtime"

            if str(f) in pairs:
                record["live_photo_pair"] = pairs[str(f)]

            out.write(json.dumps(record) + "\n")
            written += 1

    print(
        f"wrote {written} records to {args.output}"
        + (f" (skipped HEIC EXIF on {skipped_heic} files)" if skipped_heic else ""),
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

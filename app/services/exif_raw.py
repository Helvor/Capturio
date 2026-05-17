"""RAW file EXIF reading and JPEG↔RAW filename matching."""
import os
import re
from datetime import datetime
from fractions import Fraction

try:
    import piexif
    HAS_PIEXIF = True
except ImportError:
    HAS_PIEXIF = False

RAW_EXTENSIONS = {
    ".arw", ".nef", ".cr2", ".cr3", ".dng", ".raf", ".orf",
    ".rw2", ".rw1", ".pef", ".srf", ".sr2", ".x3f", ".3fr", ".mef", ".mos",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _decode(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace").strip("\x00")
    return str(v)


def _ratio_to_str(value) -> str | None:
    if not value or not isinstance(value, (tuple, list)):
        return None
    num, den = value
    return None if den == 0 else str(Fraction(num, den))


def _normalize_stem(stem: str) -> str:
    """Lowercase and strip non-alphanumeric for fuzzy matching."""
    return re.sub(r"[^a-z0-9]", "", stem.lower())


# ── Matching ──────────────────────────────────────────────────────────────────

def find_raw_match(jpeg_path: str) -> str | None:
    """Return the best matching RAW file path in the same directory as jpeg_path.

    Matching rule: RAW stem (normalized) must be a prefix of the JPEG stem
    (normalized) and at least 4 chars long.  Longest prefix wins.

    Examples:
      DSC0043-edit.jpg  →  DSC0043.ARW   ✓  ("dsc0043" prefix of "dsc0043edit")
      IMG_1234_v2.jpg   →  IMG_1234.NEF  ✓
      photo.jpg         →  photo.ARW     ✓  (exact match)
    """
    dir_path = os.path.dirname(jpeg_path)
    jpeg_stem = os.path.splitext(os.path.basename(jpeg_path))[0]
    jpeg_norm = _normalize_stem(jpeg_stem)

    best: str | None = None
    best_len = 0

    try:
        for fname in sorted(os.listdir(dir_path)):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in RAW_EXTENSIONS:
                continue
            raw_norm = _normalize_stem(os.path.splitext(fname)[0])
            if len(raw_norm) >= 4 and jpeg_norm.startswith(raw_norm) and len(raw_norm) > best_len:
                best = os.path.join(dir_path, fname)
                best_len = len(raw_norm)
    except (PermissionError, FileNotFoundError):
        pass

    return best


def build_raw_index(search_dir: str) -> dict[str, str]:
    """Walk ``search_dir`` recursively, return ``{normalized_stem: full_path}``.

    If two RAWs share the same normalized stem, the first one found wins.
    """
    index: dict[str, str] = {}
    if not search_dir or not os.path.isdir(search_dir):
        return index
    for root, _, files in os.walk(search_dir):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in RAW_EXTENSIONS:
                continue
            stem = _normalize_stem(os.path.splitext(fname)[0])
            if len(stem) >= 4 and stem not in index:
                index[stem] = os.path.join(root, fname)
    return index


def find_raw_match_in_index(jpeg_path: str, index: dict[str, str]) -> str | None:
    """Find the RAW in ``index`` whose stem is the longest prefix of the JPEG stem."""
    jpeg_norm = _normalize_stem(os.path.splitext(os.path.basename(jpeg_path))[0])
    best: str | None = None
    best_len = 0
    for raw_norm, raw_path in index.items():
        if jpeg_norm.startswith(raw_norm) and len(raw_norm) > best_len:
            best = raw_path
            best_len = len(raw_norm)
    return best


# ── EXIF reading from TIFF-based RAW ─────────────────────────────────────────

def read_exif_from_raw(path: str) -> dict:
    """Read camera metadata from a TIFF-based RAW file (ARW/NEF/DNG/CR2/…).

    Returns a dict with keys: camera, lens, focal_length, shutter_speed,
    aperture, iso, taken_at.  Unknown fields are None.
    """
    result: dict = {
        "camera": None, "lens": None, "focal_length": None,
        "shutter_speed": None, "aperture": None, "iso": None,
        "taken_at": None,
    }

    if not HAS_PIEXIF:
        return result

    try:
        exif_dict = piexif.load(path)
    except Exception:
        return result

    zeroth = exif_dict.get("0th", {})
    exif_ifd = exif_dict.get("Exif", {})

    make = _decode(zeroth.get(piexif.ImageIFD.Make))
    model = _decode(zeroth.get(piexif.ImageIFD.Model))
    if make and model:
        result["camera"] = f"{make.strip()} {model.strip()}"
    elif model:
        result["camera"] = model.strip()

    result["lens"] = _decode(exif_ifd.get(piexif.ExifIFD.LensModel))

    fl = exif_ifd.get(piexif.ExifIFD.FocalLength)
    if fl:
        fl_str = _ratio_to_str(fl)
        result["focal_length"] = f"{fl_str}mm" if fl_str else None

    exp = exif_ifd.get(piexif.ExifIFD.ExposureTime)
    if exp:
        num, den = exp
        if den and num:
            result["shutter_speed"] = f"1/{den // num}s" if num < den else f"{num // den}s"

    fnumber = exif_ifd.get(piexif.ExifIFD.FNumber)
    if fnumber and fnumber[1]:
        result["aperture"] = f"f/{float(Fraction(*fnumber)):.1f}"

    iso = exif_ifd.get(piexif.ExifIFD.ISOSpeedRatings)
    if iso:
        result["iso"] = int(iso) if not isinstance(iso, tuple) else int(iso[0])

    try:
        raw_dt = (
            exif_ifd.get(piexif.ExifIFD.DateTimeOriginal)
            or zeroth.get(piexif.ImageIFD.DateTime)
        )
        if raw_dt:
            dt_str = raw_dt.decode("utf-8") if isinstance(raw_dt, bytes) else raw_dt
            result["taken_at"] = datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass

    return result

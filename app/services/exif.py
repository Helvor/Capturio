import os
from datetime import datetime
from fractions import Fraction

from PIL import Image

try:
    import piexif
    HAS_PIEXIF = True
except ImportError:
    HAS_PIEXIF = False


def _ratio_to_str(value) -> str | None:
    if not value or not isinstance(value, (tuple, list)):
        return None
    num, den = value
    if den == 0:
        return None
    result = Fraction(num, den)
    return str(result)


def _parse_taken_at(exif_dict: dict) -> datetime | None:
    try:
        zeroth = exif_dict.get("0th", {})
        exif = exif_dict.get("Exif", {})
        raw = exif.get(piexif.ExifIFD.DateTimeOriginal) or zeroth.get(piexif.ImageIFD.DateTime)
        if raw:
            dt_str = raw.decode("utf-8") if isinstance(raw, bytes) else raw
            return datetime.strptime(dt_str, "%Y:%m:%d %H:%M:%S")
    except Exception:
        pass
    return None


def _decode(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip("\x00")
    return str(value)


def extract_exif(filepath: str) -> dict:
    result = {
        "raw": {},
        "camera": None,
        "lens": None,
        "focal_length": None,
        "shutter_speed": None,
        "aperture": None,
        "iso": None,
        "width": None,
        "height": None,
        "taken_at": None,
        "file_size_bytes": None,
    }

    try:
        result["file_size_bytes"] = os.path.getsize(filepath)
    except OSError:
        pass

    try:
        with Image.open(filepath) as img:
            result["width"], result["height"] = img.size
    except Exception:
        return result

    if not HAS_PIEXIF:
        return result

    ext = os.path.splitext(filepath)[1].lower()
    if ext not in (".jpg", ".jpeg"):
        return result

    try:
        exif_dict = piexif.load(filepath)
    except Exception:
        return result

    try:
        zeroth = exif_dict.get("0th", {})
        exif = exif_dict.get("Exif", {})
        result["raw"] = {
            "0th": {str(k): str(v) for k, v in zeroth.items()},
            "Exif": {str(k): str(v) for k, v in exif.items()},
        }

        make = _decode(zeroth.get(piexif.ImageIFD.Make))
        model = _decode(zeroth.get(piexif.ImageIFD.Model))
        if make and model:
            result["camera"] = f"{make.strip()} {model.strip()}"
        elif model:
            result["camera"] = model.strip()

        result["lens"] = _decode(exif.get(piexif.ExifIFD.LensModel))

        fl = exif.get(piexif.ExifIFD.FocalLength)
        if fl:
            fl_str = _ratio_to_str(fl)
            result["focal_length"] = f"{fl_str}mm" if fl_str else None

        exp = exif.get(piexif.ExifIFD.ExposureTime)
        if exp:
            num, den = exp
            if den and num:
                if num < den:
                    result["shutter_speed"] = f"1/{den // num}s"
                else:
                    result["shutter_speed"] = f"{num // den}s"

        fnumber = exif.get(piexif.ExifIFD.FNumber)
        if fnumber:
            ap_str = _ratio_to_str(fnumber)
            result["aperture"] = f"f/{float(Fraction(*fnumber)):.1f}" if fnumber[1] else None

        iso = exif.get(piexif.ExifIFD.ISOSpeedRatings)
        if iso:
            result["iso"] = int(iso) if not isinstance(iso, tuple) else int(iso[0])

        result["taken_at"] = _parse_taken_at(exif_dict)
    except Exception:
        pass

    return result

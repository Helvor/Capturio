import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime
from fractions import Fraction

from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.photo import Photo
from app.services.thumbnail import _fix_orientation, MAX_SIZE, QUALITY
from app.config import get_settings

try:
    import piexif
    HAS_PIEXIF = True
except ImportError:
    HAS_PIEXIF = False

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
CONCURRENCY = 4  # parallel photo processing workers


# ── Helpers (mirrored from exif.py to avoid double-open) ──────────────────────

def _decode(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip("\x00")
    return str(value)


def _ratio_to_str(value) -> str | None:
    if not value or not isinstance(value, (tuple, list)):
        return None
    num, den = value
    if den == 0:
        return None
    return str(Fraction(num, den))


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


# ── Single-pass processor ─────────────────────────────────────────────────────

def _process_single(fpath: str, fname: str, photo_id_str: str, thumbs_dir: str) -> dict:
    """Open the photo file once: fix orientation, get dimensions, generate thumbnail, read EXIF."""
    exif: dict = {
        "raw": {}, "camera": None, "lens": None, "focal_length": None,
        "shutter_speed": None, "aperture": None, "iso": None,
        "width": None, "height": None, "taken_at": None, "file_size_bytes": None,
    }

    try:
        exif["file_size_bytes"] = os.path.getsize(fpath)
    except OSError:
        pass

    # Single PIL open — orientation + dimensions + thumbnail
    try:
        img = Image.open(fpath)
        img.load()
        img = _fix_orientation(img)
        exif["width"], exif["height"] = img.size

        os.makedirs(thumbs_dir, exist_ok=True)
        out_path = os.path.join(thumbs_dir, f"{photo_id_str}.webp")
        thumb = img.convert("RGB")
        thumb.thumbnail(MAX_SIZE, Image.LANCZOS)
        thumb.save(out_path, "WEBP", quality=QUALITY, method=6)
    except Exception as e:
        return {"ok": False, "error": str(e), "exif": exif}

    # EXIF metadata via piexif (fast header-only read, JPEG only)
    if HAS_PIEXIF and os.path.splitext(fpath)[1].lower() in (".jpg", ".jpeg"):
        try:
            exif_dict = piexif.load(fpath)
            zeroth = exif_dict.get("0th", {})
            exif_ifd = exif_dict.get("Exif", {})

            exif["raw"] = {
                "0th": {str(k): str(v) for k, v in zeroth.items()},
                "Exif": {str(k): str(v) for k, v in exif_ifd.items()},
            }

            make = _decode(zeroth.get(piexif.ImageIFD.Make))
            model = _decode(zeroth.get(piexif.ImageIFD.Model))
            if make and model:
                exif["camera"] = f"{make.strip()} {model.strip()}"
            elif model:
                exif["camera"] = model.strip()

            exif["lens"] = _decode(exif_ifd.get(piexif.ExifIFD.LensModel))

            fl = exif_ifd.get(piexif.ExifIFD.FocalLength)
            if fl:
                fl_str = _ratio_to_str(fl)
                exif["focal_length"] = f"{fl_str}mm" if fl_str else None

            exp = exif_ifd.get(piexif.ExifIFD.ExposureTime)
            if exp:
                num, den = exp
                if den and num:
                    exif["shutter_speed"] = f"1/{den // num}s" if num < den else f"{num // den}s"

            fnumber = exif_ifd.get(piexif.ExifIFD.FNumber)
            if fnumber and fnumber[1]:
                exif["aperture"] = f"f/{float(Fraction(*fnumber)):.1f}"

            iso = exif_ifd.get(piexif.ExifIFD.ISOSpeedRatings)
            if iso:
                exif["iso"] = int(iso) if not isinstance(iso, tuple) else int(iso[0])

            exif["taken_at"] = _parse_taken_at(exif_dict)
        except Exception:
            pass

    return {"ok": True, "exif": exif}


# ── Folder helpers ────────────────────────────────────────────────────────────

def _count_images(folder: str) -> int:
    try:
        return sum(
            1 for f in os.listdir(folder)
            if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
        )
    except PermissionError:
        return 0


def get_folder_tree(base_dir: str, rel_path: str, known_paths: set) -> list[dict]:
    abs_path = os.path.realpath(os.path.join(base_dir, rel_path))
    base_real = os.path.realpath(base_dir)

    if not abs_path.startswith(base_real):
        return []

    try:
        entries = sorted(os.scandir(abs_path), key=lambda e: e.name.lower())
    except PermissionError:
        return []

    folders = []
    for entry in entries:
        if not entry.is_dir(follow_symlinks=False):
            continue
        entry_real = os.path.realpath(entry.path)
        if not entry_real.startswith(base_real):
            continue
        image_count = _count_images(entry.path)
        imported_count = sum(
            1 for f in ([] if image_count == 0 else os.listdir(entry.path))
            if os.path.join(entry.path, f) in known_paths
        )
        child_rel = os.path.relpath(entry.path, base_real)
        folders.append({
            "name": entry.name,
            "rel_path": child_rel,
            "image_count": image_count,
            "imported_count": imported_count,
            "has_subfolders": any(e.is_dir() for e in os.scandir(entry.path)
                                  if not e.name.startswith(".")),
        })

    return folders


def _collect_files(folder: str, recursive: bool) -> list[tuple[str, str]]:
    files = []
    if recursive:
        for root, _, filenames in os.walk(folder):
            for fname in sorted(filenames):
                if os.path.splitext(fname)[1].lower() in SUPPORTED_EXTENSIONS:
                    files.append((os.path.join(root, fname), fname))
    else:
        try:
            for fname in sorted(os.listdir(folder)):
                if os.path.splitext(fname)[1].lower() not in SUPPORTED_EXTENSIONS:
                    continue
                fpath = os.path.join(folder, fname)
                if os.path.isfile(fpath):
                    files.append((fpath, fname))
        except PermissionError:
            pass
    return files


# ── Ingest stream ─────────────────────────────────────────────────────────────

async def ingest_files_stream(
    files: list[tuple[str, str]],
    db: AsyncSession,
) -> AsyncGenerator[dict, None]:
    """Ingest files concurrently and yield progress dicts."""
    settings = get_settings()

    existing = await db.execute(select(Photo.filepath))
    known_paths = {row[0] for row in existing.all()}

    total = len(files)
    skipped = [(fp, fn) for fp, fn in files if fp in known_paths]
    to_process = [(fp, fn) for fp, fn in files if fp not in known_paths]
    skipped_count = len(skipped)
    new_count = 0
    new_photo_ids: list[uuid.UUID] = []
    current = 0

    # Report already-known files instantly (no I/O needed)
    for fpath, fname in skipped:
        current += 1
        yield {"done": False, "current": current, "total": total,
               "new": 0, "skipped": skipped_count, "filename": fname, "status": "skipped"}

    if not to_process:
        await db.commit()
        yield {"done": True, "total": total, "new": 0,
               "skipped": skipped_count, "photo_ids": []}
        return

    # Concurrent processing: up to CONCURRENCY photos at once
    semaphore = asyncio.Semaphore(CONCURRENCY)
    result_queue: asyncio.Queue = asyncio.Queue()

    async def worker(fpath: str, fname: str) -> None:
        photo_id = uuid.uuid4()
        async with semaphore:
            data = await asyncio.to_thread(
                _process_single, fpath, fname, str(photo_id), settings.thumbs_dir
            )
        data["photo_id"] = photo_id
        data["fpath"] = fpath
        data["fname"] = fname
        await result_queue.put(data)

    tasks = [asyncio.create_task(worker(fp, fn)) for fp, fn in to_process]

    for _ in range(len(to_process)):
        data = await result_queue.get()
        current += 1

        if data["ok"]:
            exif = data["exif"]
            photo = Photo(
                id=data["photo_id"],
                filename=data["fname"],
                filepath=data["fpath"],
                is_published=False,
                download_enabled=True,
                taken_at=exif.get("taken_at"),
                exif_data=exif.get("raw"),
                exif_camera=exif.get("camera"),
                exif_lens=exif.get("lens"),
                exif_focal_length=exif.get("focal_length"),
                exif_shutter_speed=exif.get("shutter_speed"),
                exif_aperture=exif.get("aperture"),
                exif_iso=exif.get("iso"),
                exif_width=exif.get("width"),
                exif_height=exif.get("height"),
                file_size_bytes=exif.get("file_size_bytes"),
            )
            db.add(photo)
            new_photo_ids.append(data["photo_id"])
            new_count += 1
            yield {"done": False, "current": current, "total": total,
                   "new": new_count, "skipped": skipped_count,
                   "filename": data["fname"], "status": "imported"}
        else:
            yield {"done": False, "current": current, "total": total,
                   "new": new_count, "skipped": skipped_count,
                   "filename": data["fname"], "status": "error", "error": data.get("error", "")}

    await asyncio.gather(*tasks)
    await db.commit()
    yield {"done": True, "total": total, "new": new_count,
           "skipped": skipped_count, "photo_ids": [str(p) for p in new_photo_ids]}


# ── Legacy wrappers ───────────────────────────────────────────────────────────

async def _ingest_files(files: list[tuple[str, str]], db: AsyncSession) -> dict:
    result = {}
    async for event in ingest_files_stream(files, db):
        result = event
    photo_ids = [uuid.UUID(p) for p in result.get("photo_ids", [])]
    return {"new": result["new"], "skipped": result["skipped"],
            "errors": [], "photo_ids": photo_ids}


async def scan_folder_flat(folder: str, db: AsyncSession) -> dict:
    files = _collect_files(folder, recursive=False)
    if not files:
        return {"new": 0, "skipped": 0, "errors": [], "photo_ids": []}
    return await _ingest_files(files, db)


async def scan_photos_dir(photos_dir: str, db: AsyncSession) -> dict:
    files = _collect_files(photos_dir, recursive=True)
    return await _ingest_files(files, db)

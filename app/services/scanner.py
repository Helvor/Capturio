import asyncio
import os
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.photo import Photo
from app.services.exif import extract_exif
from app.services.thumbnail import generate_thumbnail
from app.config import get_settings

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _count_images(folder: str) -> int:
    try:
        return sum(
            1 for f in os.listdir(folder)
            if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
        )
    except PermissionError:
        return 0


def get_folder_tree(base_dir: str, rel_path: str, known_paths: set) -> list[dict]:
    """Return immediate subfolders of base_dir/rel_path with image counts and import status."""
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


async def _ingest_files(files: list[tuple[str, str]], db: AsyncSession) -> dict:
    """Ingest a list of (filepath, filename) pairs. Returns result dict."""
    settings = get_settings()
    new_count = 0
    skipped_count = 0
    errors = []
    new_photo_ids = []

    existing = await db.execute(select(Photo.filepath))
    known_paths = {row[0] for row in existing.all()}

    for fpath, fname in files:
        if fpath in known_paths:
            skipped_count += 1
            continue
        try:
            exif = await asyncio.to_thread(extract_exif, fpath)
            photo_id = uuid.uuid4()
            await asyncio.to_thread(generate_thumbnail, str(photo_id), fpath, settings.thumbs_dir)
            photo = Photo(
                id=photo_id,
                filename=fname,
                filepath=fpath,
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
            new_photo_ids.append(photo_id)
            new_count += 1
        except Exception as e:
            errors.append({"file": fpath, "error": str(e)})

    await db.commit()
    return {"new": new_count, "skipped": skipped_count, "errors": errors, "photo_ids": new_photo_ids}


async def scan_folder_flat(folder: str, db: AsyncSession) -> dict:
    """Import only the direct image files of a folder, no subdirectories."""
    files = []
    try:
        for fname in sorted(os.listdir(folder)):
            if os.path.splitext(fname)[1].lower() not in SUPPORTED_EXTENSIONS:
                continue
            fpath = os.path.join(folder, fname)
            if os.path.isfile(fpath):
                files.append((fpath, fname))
    except PermissionError:
        return {"new": 0, "skipped": 0, "errors": [{"file": folder, "error": "Permission denied"}], "photo_ids": []}
    return await _ingest_files(files, db)


async def scan_photos_dir(photos_dir: str, db: AsyncSession) -> dict:
    """Recursively scan a directory and import all images found."""
    files = []
    for root, _, filenames in os.walk(photos_dir):
        for fname in sorted(filenames):
            if os.path.splitext(fname)[1].lower() not in SUPPORTED_EXTENSIONS:
                continue
            files.append((os.path.join(root, fname), fname))
    return await _ingest_files(files, db)

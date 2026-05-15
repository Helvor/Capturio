import os
import uuid
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models.photo import Photo
from app.services.exif import extract_exif
from app.services.thumbnail import generate_thumbnail
from app.config import get_settings

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


async def scan_photos_dir(photos_dir: str, db: AsyncSession) -> dict:
    settings = get_settings()
    new_count = 0
    skipped_count = 0
    errors = []

    existing = await db.execute(select(Photo.filepath))
    known_paths = {row[0] for row in existing.all()}

    for root, _, files in os.walk(photos_dir):
        for fname in sorted(files):
            ext = os.path.splitext(fname)[1].lower()
            if ext not in SUPPORTED_EXTENSIONS:
                continue

            fpath = os.path.join(root, fname)

            if fpath in known_paths:
                skipped_count += 1
                continue

            try:
                exif = extract_exif(fpath)
                photo_id = uuid.uuid4()

                thumb_path = generate_thumbnail(str(photo_id), fpath, settings.thumbs_dir)

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
                new_count += 1
            except Exception as e:
                errors.append({"file": fpath, "error": str(e)})

    await db.commit()
    return {"new": new_count, "skipped": skipped_count, "errors": errors}

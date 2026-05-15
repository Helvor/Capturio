import os
from PIL import Image


MAX_SIZE = (1920, 1920)
QUALITY = 92


def generate_thumbnail(photo_id: str, filepath: str, thumbs_dir: str, force: bool = False) -> str | None:
    os.makedirs(thumbs_dir, exist_ok=True)
    out_path = os.path.join(thumbs_dir, f"{photo_id}.webp")

    if os.path.exists(out_path) and not force:
        return out_path

    try:
        with Image.open(filepath) as img:
            img = img.convert("RGB")
            img.thumbnail(MAX_SIZE, Image.LANCZOS)
            img.save(out_path, "WEBP", quality=QUALITY, method=6)
        return out_path
    except Exception:
        return None

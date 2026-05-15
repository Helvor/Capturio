import os
from PIL import Image

MAX_SIZE = (1920, 1920)
QUALITY = 92

# EXIF Orientation tag → PIL transpose operation
_ORIENTATION_OPS = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def _fix_orientation(img: Image.Image) -> Image.Image:
    try:
        exif = img.getexif()
        orientation = exif.get(0x0112, 1)  # tag 274 = Orientation
        if orientation in _ORIENTATION_OPS:
            return img.transpose(_ORIENTATION_OPS[orientation])
    except Exception:
        pass
    return img


def generate_thumbnail(photo_id: str, filepath: str, thumbs_dir: str, force: bool = False) -> str | None:
    os.makedirs(thumbs_dir, exist_ok=True)
    out_path = os.path.join(thumbs_dir, f"{photo_id}.webp")

    if os.path.exists(out_path) and not force:
        return out_path

    try:
        img = Image.open(filepath)
        img.load()  # force eager load so file handle can close safely
        img = _fix_orientation(img)
        img = img.convert("RGB")
        img.thumbnail(MAX_SIZE, Image.LANCZOS)
        img.save(out_path, "WEBP", quality=QUALITY, method=6)
        return out_path
    except Exception:
        return None

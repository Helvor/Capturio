import time

import markdown as md
from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

ASSET_VERSION = str(int(time.time()))


def _markdown_filter(text: str) -> str:
    return md.markdown(text or "", extensions=["extra"])


def _filesize_filter(size: int | None) -> str:
    if size is None:
        return "—"
    if size < 1024:
        return f"{size} B"
    elif size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / 1024 ** 2:.1f} MB"


templates.env.filters["markdown"] = _markdown_filter
templates.env.filters["filesize"] = _filesize_filter
templates.env.globals["asset_v"] = ASSET_VERSION

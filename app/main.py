import asyncio
import os
import subprocess
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
import markdown as md

from app.config import get_settings
from app.routers import auth, public, admin


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    os.makedirs(settings.thumbs_dir, exist_ok=True)
    os.makedirs(settings.photos_dir, exist_ok=True)

    proc = await asyncio.create_subprocess_exec(
        "alembic", "upgrade", "head",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        print(f"[alembic] WARN: {stderr.decode()}")
    else:
        print(f"[alembic] {stdout.decode().strip()}")

    yield


app = FastAPI(title="Capturio", lifespan=lifespan)

app.mount("/static", StaticFiles(directory="app/static"), name="static")

templates = Jinja2Templates(directory="app/templates")


def markdown_filter(text: str) -> str:
    return md.markdown(text or "", extensions=["extra"])


templates.env.filters["markdown"] = markdown_filter


def _format_filesize(size: int | None) -> str:
    if size is None:
        return "—"
    if size < 1024:
        return f"{size} B"
    elif size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    else:
        return f"{size / 1024 ** 2:.1f} MB"


templates.env.filters["filesize"] = _format_filesize

app.include_router(auth.router)
app.include_router(public.router)
app.include_router(admin.router)

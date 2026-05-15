import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
import app.templates_env  # noqa: F401 — registers Jinja2 filters on shared instance
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

app.include_router(auth.router)
app.include_router(public.router)
app.include_router(admin.router)

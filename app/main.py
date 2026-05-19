import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
import app.templates_env  # noqa: F401 — registers Jinja2 filters on shared instance
from app.routers import auth, public, admin
from app.middleware import AdminIPMiddleware, ProxyResolutionMiddleware, SecurityHeadersMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    os.makedirs(settings.thumbs_dir, exist_ok=True)
    os.makedirs(settings.photos_dir, exist_ok=True)

    if settings.secret_key == "changeme":
        print("[WARN] SECRET_KEY is set to 'changeme' — change it for production!")

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

settings = get_settings()

# Middleware order: Starlette applies from bottom to top, so proxy resolution
# runs first (outermost), then security headers, then IP allowlist.
if settings.admin_ip_allowlist:
    app.add_middleware(AdminIPMiddleware, allowlist=settings.admin_ip_allowlist)

app.add_middleware(SecurityHeadersMiddleware)

if settings.trusted_proxies:
    app.add_middleware(ProxyResolutionMiddleware, trusted_proxies=settings.trusted_proxies)

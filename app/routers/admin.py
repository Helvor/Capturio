import asyncio
import json
import math
import os
import shutil
import time
import uuid

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, delete
import markdown as md

from app.database import get_db, AsyncSessionLocal, engine
from app.models.photo import Photo
from app.models.album import Album, AlbumPhoto
from app.models.post import Post, PostType
from app.models.space import Space
from app.routers.auth import get_current_admin
from app.services.scanner import get_folder_tree, ingest_files_stream, _collect_files
from app.services.exif import extract_exif
from app.services.thumbnail import generate_thumbnail
from app.config import get_settings
from app.templates_env import templates

# ── Background import jobs ─────────────────────────────────────────────────────
# job_id → {folder, status, current, total, new, skipped, started_at, done_at}
_jobs: dict[str, dict] = {}

# ── Background regen job (single slot) ────────────────────────────────────────
_regen_job: dict = {}


async def _run_regen_job():
    from app.database import AsyncSessionLocal
    async with AsyncSessionLocal() as db:
        settings = get_settings()
        photos = (await db.execute(select(Photo.id, Photo.filepath))).all()
        total = len(photos)
        _regen_job.update({"status": "running", "current": 0, "total": total,
                           "errors": 0, "started_at": time.time(), "done_at": None})
        for i, (photo_id, filepath) in enumerate(photos):
            result = await asyncio.to_thread(generate_thumbnail, str(photo_id), filepath, settings.thumbs_dir, True)
            if not result:
                _regen_job["errors"] += 1
            _regen_job["current"] = i + 1
        _regen_job.update({"status": "done", "done_at": time.time()})


# ── Background match-RAW-meta job ─────────────────────────────────────────────
_match_raw_job: dict = {}


async def _run_match_raw_job(album_id: uuid.UUID | None = None, raw_dir: str | None = None):
    from app.database import AsyncSessionLocal
    from app.services.exif_raw import (
        find_raw_match, read_exif_from_raw,
        build_raw_index, find_raw_match_in_index,
    )
    from sqlalchemy import update as sa_update

    _match_raw_job.update({
        "status": "running", "current": 0, "total": 0,
        "matched": 0, "no_raw": 0, "no_exif": 0,
        "scope": "album" if album_id else "all",
        "raw_dir": raw_dir or "(same folder as JPEG)",
        "started_at": time.time(), "done_at": None,
    })

    raw_index: dict[str, str] | None = None
    if raw_dir:
        raw_index = await asyncio.to_thread(build_raw_index, raw_dir)
        _match_raw_job["raw_index_size"] = len(raw_index)

    async with AsyncSessionLocal() as db:
        query = select(Photo.id, Photo.filepath).where(Photo.exif_camera.is_(None))
        if album_id is not None:
            query = query.join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id).where(
                AlbumPhoto.album_id == album_id
            )
        query = query.order_by(Photo.uploaded_at)
        photos = (await db.execute(query)).all()

        _match_raw_job["total"] = len(photos)

        for i, (photo_id, filepath) in enumerate(photos):
            _match_raw_job["current"] = i + 1
            try:
                if raw_index is not None:
                    raw_path = find_raw_match_in_index(filepath, raw_index)
                else:
                    raw_path = await asyncio.to_thread(find_raw_match, filepath)
                if not raw_path:
                    _match_raw_job["no_raw"] += 1
                    continue
                exif = await asyncio.to_thread(read_exif_from_raw, raw_path)
                if not exif.get("camera"):
                    _match_raw_job["no_exif"] += 1
                    continue
                values = {k: v for k, v in {
                    "exif_camera": exif["camera"],
                    "exif_lens": exif["lens"],
                    "exif_focal_length": exif["focal_length"],
                    "exif_shutter_speed": exif["shutter_speed"],
                    "exif_aperture": exif["aperture"],
                    "exif_iso": exif["iso"],
                    "taken_at": exif["taken_at"],
                }.items() if v is not None}
                await db.execute(sa_update(Photo).where(Photo.id == photo_id).values(**values))
                _match_raw_job["matched"] += 1
            except Exception:
                pass

        await db.commit()

    _match_raw_job.update({"status": "done", "done_at": time.time()})


def _resolve_safe_path(raw_dir: str) -> str | None:
    """Resolve ``raw_dir`` against settings.photos_dir, reject if outside.

    Accepts absolute or relative paths.  Returns absolute path or None if
    invalid / outside photos_dir.
    """
    settings = get_settings()
    base = os.path.realpath(settings.photos_dir)
    candidate = raw_dir.strip()
    if not candidate:
        return None
    if not os.path.isabs(candidate):
        candidate = os.path.join(settings.photos_dir, candidate)
    candidate = os.path.realpath(candidate)
    if not candidate.startswith(base):
        return None
    if not os.path.isdir(candidate):
        return None
    return candidate

router = APIRouter(prefix="/admin")

PAGE_SIZE = 20


def _admin_ctx(request: Request, **kwargs):
    return {"request": request, **kwargs}


async def _require_admin(request: Request):
    """Dependency that redirects to login instead of raising 401."""
    try:
        return get_current_admin(request)
    except HTTPException:
        raise HTTPException(status_code=302, headers={"Location": "/auth/login"})


# ── Dashboard ─────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    from sqlalchemy import text
    total_photos = (await db.execute(select(func.count()).select_from(Photo))).scalar()
    published_photos = (await db.execute(select(func.count()).select_from(Photo).where(Photo.is_published == True))).scalar()
    total_albums = (await db.execute(select(func.count()).select_from(Album))).scalar()
    total_posts = (await db.execute(select(func.count()).select_from(Post).where(Post.post_type == PostType.announcement))).scalar()
    db_size_bytes = (await db.execute(text("SELECT pg_database_size(current_database())"))).scalar()
    db_size_mb = round(db_size_bytes / 1024 / 1024, 1) if db_size_bytes else 0

    recent_photos = (await db.execute(
        select(Photo).order_by(Photo.uploaded_at.desc()).limit(10)
    )).scalars().all()

    announcements = (await db.execute(
        select(Post)
        .where(Post.post_type == PostType.announcement, Post.is_published == True)
        .order_by(Post.pinned.desc(), Post.created_at.desc())
        .limit(5)
    )).scalars().all()

    return templates.TemplateResponse("admin/dashboard.html", _admin_ctx(
        request,
        total_photos=total_photos,
        published_photos=published_photos,
        total_albums=total_albums,
        total_posts=total_posts,
        db_size_mb=db_size_mb,
        recent_photos=recent_photos,
        announcements=announcements,
    ))


# ── DB optimize ───────────────────────────────────────────────────────────────

_TABLES = ["photos", "albums", "album_photos", "spaces", "posts", "users"]


@router.post("/db/optimize")
async def db_optimize(request: Request):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    from sqlalchemy import text

    async def stream():
        results = []
        # VACUUM ANALYZE + REINDEX each table (must run outside a transaction)
        for table in _TABLES:
            try:
                async with engine.connect() as conn:
                    await conn.execution_options(isolation_level="AUTOCOMMIT")
                    await conn.execute(text(f"VACUUM ANALYZE {table}"))
                    await conn.execute(text(f"REINDEX TABLE {table}"))
                    row = (await conn.execute(text(
                        f"SELECT n_live_tup, n_dead_tup, "
                        f"pg_size_pretty(pg_total_relation_size('{table}')) AS size "
                        f"FROM pg_stat_user_tables WHERE relname = '{table}'"
                    ))).mappings().fetchone()
                results.append({
                    "table": table,
                    "live": row["n_live_tup"] if row else "?",
                    "dead": row["n_dead_tup"] if row else "?",
                    "size": row["size"] if row else "?",
                    "ok": True,
                })
            except Exception as e:
                results.append({"table": table, "ok": False, "error": str(e)})
            yield f"data: {json.dumps(results[-1])}\n\n"

        # Final DB size
        try:
            async with engine.connect() as conn:
                db_size = (await conn.execute(
                    text("SELECT pg_size_pretty(pg_database_size(current_database()))")
                )).scalar()
        except Exception:
            db_size = "?"
        yield f"data: {json.dumps({'done': True, 'db_size': db_size})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── Folder browser ────────────────────────────────────────────────────────────

@router.get("/api/browse-folders")
async def api_browse_folders(request: Request, path: str = ""):
    """JSON folder browser for the RAW-folder picker. Returns subfolders under
    ``path`` (relative to photos_dir) plus a breadcrumb trail."""
    try:
        get_current_admin(request)
    except HTTPException:
        raise HTTPException(status_code=401)

    settings = get_settings()
    base = os.path.realpath(settings.photos_dir)
    abs_path = os.path.realpath(os.path.join(base, path)) if path else base
    if not abs_path.startswith(base):
        raise HTTPException(status_code=400, detail="Invalid path")
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=404, detail="Not a directory")

    folders = []
    try:
        for entry in sorted(os.scandir(abs_path), key=lambda e: e.name.lower()):
            if entry.is_dir(follow_symlinks=False) and not entry.name.startswith("."):
                rel = os.path.relpath(entry.path, base)
                has_sub = False
                try:
                    has_sub = any(
                        e.is_dir() and not e.name.startswith(".")
                        for e in os.scandir(entry.path)
                    )
                except (PermissionError, FileNotFoundError):
                    pass
                folders.append({"name": entry.name, "rel_path": rel, "has_sub": has_sub})
    except PermissionError:
        pass

    parts = [p for p in path.split(os.sep) if p] if path else []
    breadcrumb = [
        {"name": part, "rel_path": os.path.join(*parts[: i + 1])}
        for i, part in enumerate(parts)
    ]

    return JSONResponse({
        "current_path": path,
        "breadcrumb": breadcrumb,
        "folders": folders,
    })


@router.get("/folders", response_class=HTMLResponse)
async def folders_browser(
    request: Request,
    path: str = "",
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    settings = get_settings()
    existing = await db.execute(select(Photo.filepath))
    known_paths = {row[0] for row in existing.all()}

    folders = get_folder_tree(settings.photos_dir, path, known_paths)
    albums = (await db.execute(select(Album).order_by(Album.sort_order))).scalars().all()

    # breadcrumb
    parts = [p for p in path.split(os.sep) if p] if path else []
    breadcrumb = []
    for i, part in enumerate(parts):
        breadcrumb.append({"name": part, "rel_path": os.path.join(*parts[: i + 1])})

    return templates.TemplateResponse("admin/folders.html", _admin_ctx(
        request,
        folders=folders,
        current_path=path,
        breadcrumb=breadcrumb,
        albums=albums,
        photos_dir=settings.photos_dir,
    ))


async def _run_import_job(job_id: str, files: list, target_album_id: uuid.UUID | None, folder_name: str):
    """Background task: imports files with its own DB session."""
    async with AsyncSessionLocal() as db:
        try:
            async for event in ingest_files_stream(files, db):
                if event.get("done"):
                    new_photo_ids = [uuid.UUID(p) for p in event.get("photo_ids", [])]
                    _jobs[job_id].update({"new": event["new"], "skipped": event["skipped"]})

                    if target_album_id and new_photo_ids:
                        max_pos = (await db.execute(
                            select(func.coalesce(func.max(AlbumPhoto.position), -1))
                            .where(AlbumPhoto.album_id == target_album_id)
                        )).scalar()
                        for i, pid in enumerate(new_photo_ids):
                            db.add(AlbumPhoto(album_id=target_album_id, photo_id=pid,
                                              position=max_pos + 1 + i))
                        await db.commit()
                else:
                    _jobs[job_id].update({
                        "current": event["current"],
                        "total": event["total"],
                        "filename": event.get("filename", ""),
                    })
        except Exception as e:
            _jobs[job_id]["error"] = str(e)
        finally:
            _jobs[job_id]["status"] = "done"
            _jobs[job_id]["done_at"] = time.time()


@router.post("/import-folder")
async def import_folder(
    request: Request,
    folder_path: str = Form(...),
    album_id: str = Form(default=""),
    new_album_title: str = Form(default=""),
    new_album_slug: str = Form(default=""),
    recursive: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    settings = get_settings()
    abs_target = os.path.realpath(os.path.join(settings.photos_dir, folder_path))
    if not abs_target.startswith(os.path.realpath(settings.photos_dir)):
        raise HTTPException(status_code=400, detail="Invalid path")

    # Create album in request context so ID is ready before background task starts
    target_album_id: uuid.UUID | None = None
    if new_album_title and new_album_slug:
        new_album = Album(
            title=new_album_title,
            slug=new_album_slug,
        )
        db.add(new_album)
        await db.commit()
        target_album_id = new_album.id
    elif album_id:
        target_album_id = uuid.UUID(album_id)

    files = _collect_files(abs_target, recursive=(recursive == "on"))
    folder_name = os.path.basename(abs_target)
    parent = os.path.dirname(folder_path) if folder_path else ""

    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "status": "running",
        "folder": folder_name,
        "current": 0,
        "total": len(files),
        "new": 0,
        "skipped": 0,
        "filename": "",
        "started_at": time.time(),
        "done_at": None,
        "parent": parent,
    }

    asyncio.create_task(_run_import_job(job_id, files, target_album_id, folder_name))

    return RedirectResponse(f"/admin/folders?path={parent}", status_code=303)


@router.get("/import-jobs")
async def import_jobs(request: Request):
    try:
        get_current_admin(request)
    except HTTPException:
        raise HTTPException(status_code=401)

    # Clean up completed jobs older than 30s
    now = time.time()
    stale = [jid for jid, j in _jobs.items() if j["status"] == "done" and (now - j["done_at"]) > 30]
    for jid in stale:
        del _jobs[jid]

    return JSONResponse(list(_jobs.values()))


@router.get("/regen-job")
async def regen_job_status(request: Request):
    try:
        get_current_admin(request)
    except HTTPException:
        raise HTTPException(status_code=401)
    return JSONResponse(_regen_job)


# ── Photos ─────────────────────────────────────────────────────────────────────

@router.post("/photos/regen-thumbs-bg")
async def regen_thumbs_bg(request: Request):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    if _regen_job.get("status") == "running":
        return RedirectResponse("/admin/photos", status_code=303)

    asyncio.create_task(_run_regen_job())
    return RedirectResponse("/admin/photos", status_code=303)


@router.post("/photos/match-raw-meta-bg")
async def start_match_raw_meta_bg(
    request: Request,
    raw_dir: str = Form(default=""),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    if _match_raw_job.get("status") == "running":
        return RedirectResponse("/admin/photos", status_code=303)

    resolved_dir: str | None = None
    if raw_dir.strip():
        resolved_dir = _resolve_safe_path(raw_dir)
        if resolved_dir is None:
            return JSONResponse({"ok": False, "error": "Invalid RAW folder path"}, status_code=400)

    asyncio.create_task(_run_match_raw_job(raw_dir=resolved_dir))
    return RedirectResponse("/admin/photos", status_code=303)


@router.post("/albums/{album_id}/match-raw-meta-bg")
async def start_album_match_raw_meta_bg(
    album_id: str,
    request: Request,
    raw_dir: str = Form(default=""),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    if _match_raw_job.get("status") == "running":
        return RedirectResponse(f"/admin/albums/{album_id}/edit", status_code=303)

    resolved_dir: str | None = None
    if raw_dir.strip():
        resolved_dir = _resolve_safe_path(raw_dir)
        if resolved_dir is None:
            return JSONResponse({"ok": False, "error": "Invalid RAW folder path"}, status_code=400)

    try:
        aid = uuid.UUID(album_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid album id")

    asyncio.create_task(_run_match_raw_job(album_id=aid, raw_dir=resolved_dir))
    return RedirectResponse(f"/admin/albums/{album_id}/edit", status_code=303)


@router.get("/match-raw-job")
async def match_raw_job_status(request: Request):
    try:
        get_current_admin(request)
    except HTTPException:
        raise HTTPException(status_code=401)
    return JSONResponse(_match_raw_job)


@router.post("/photos/regen-thumbs")
async def regen_thumbs(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    settings = get_settings()
    photos = (await db.execute(select(Photo.id, Photo.filepath))).all()

    async def stream():
        total = len(photos)
        errors = []
        for i, (photo_id, filepath) in enumerate(photos):
            result = await asyncio.to_thread(generate_thumbnail, str(photo_id), filepath, settings.thumbs_dir, True)
            status = "ok" if result else f"failed: {filepath}"
            if not result:
                errors.append(filepath)
            yield f"data: {json.dumps({'current': i+1, 'total': total, 'status': status})}\n\n"
        yield f"data: {json.dumps({'done': True, 'total': total, 'errors': len(errors)})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.get("/photos", response_class=HTMLResponse)
async def photos_list(
    request: Request,
    page: int = 1,
    per_page: int = 20,
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    per_page = per_page if per_page in (20, 50, 100) else 20
    total = (await db.execute(select(func.count()).select_from(Photo))).scalar()
    total_pages = max(1, math.ceil(total / per_page))
    page = min(page, total_pages)
    photos = (await db.execute(
        select(Photo).order_by(Photo.uploaded_at.desc()).offset((page - 1) * per_page).limit(per_page)
    )).scalars().all()

    return templates.TemplateResponse("admin/photos.html", _admin_ctx(
        request,
        photos=photos,
        page=page,
        per_page=per_page,
        total=total,
        total_pages=total_pages,
    ))


@router.post("/photos/publish-all")
async def publish_all_photos(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    from sqlalchemy import update
    await db.execute(update(Photo).where(Photo.is_published == False).values(is_published=True))
    await db.commit()
    return RedirectResponse("/admin/photos", status_code=303)


@router.post("/photos/upload")
async def upload_photo(
    request: Request,
    files: list[UploadFile] = File(...),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    settings = get_settings()
    uploads_dir = os.path.join(settings.photos_dir, "uploads")
    os.makedirs(uploads_dir, exist_ok=True)

    for upload in files:
        ext = os.path.splitext(upload.filename)[1].lower()
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            continue

        dest = os.path.join(uploads_dir, upload.filename)
        with open(dest, "wb") as f:
            shutil.copyfileobj(upload.file, f)

        existing = (await db.execute(select(Photo).where(Photo.filepath == dest))).scalar_one_or_none()
        if existing:
            continue

        exif = await asyncio.to_thread(extract_exif, dest)
        photo_id = uuid.uuid4()
        await asyncio.to_thread(generate_thumbnail, str(photo_id), dest, settings.thumbs_dir)

        photo = Photo(
            id=photo_id,
            filename=upload.filename,
            filepath=dest,
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

    await db.commit()
    return RedirectResponse("/admin/photos", status_code=303)


@router.get("/photos/{photo_id}/edit", response_class=HTMLResponse)
async def edit_photo_form(photo_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    photo = (await db.execute(select(Photo).where(Photo.id == uuid.UUID(photo_id)))).scalar_one_or_none()
    if not photo:
        raise HTTPException(status_code=404)

    return templates.TemplateResponse("admin/photo_edit.html", _admin_ctx(request, photo=photo))


@router.post("/photos/{photo_id}/edit")
async def edit_photo(
    photo_id: str,
    request: Request,
    title: str = Form(default=""),
    description: str = Form(default=""),
    is_published: str = Form(default=""),
    download_enabled: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    photo = (await db.execute(select(Photo).where(Photo.id == uuid.UUID(photo_id)))).scalar_one_or_none()
    if not photo:
        raise HTTPException(status_code=404)

    photo.title = title or None
    photo.description = description or None
    photo.is_published = is_published == "on"
    photo.download_enabled = download_enabled == "on"
    await db.commit()

    return RedirectResponse("/admin/photos", status_code=303)


@router.post("/photos/{photo_id}/toggle-publish")
async def toggle_publish(photo_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    photo = (await db.execute(select(Photo).where(Photo.id == uuid.UUID(photo_id)))).scalar_one_or_none()
    if photo:
        photo.is_published = not photo.is_published
        await db.commit()

    return RedirectResponse("/admin/photos", status_code=303)


@router.post("/photos/{photo_id}/delete")
async def delete_photo(photo_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    photo = (await db.execute(select(Photo).where(Photo.id == uuid.UUID(photo_id)))).scalar_one_or_none()
    if photo:
        settings = get_settings()
        thumb = os.path.join(settings.thumbs_dir, f"{photo_id}.webp")
        if os.path.exists(thumb):
            os.remove(thumb)
        await db.delete(photo)
        await db.commit()

    return RedirectResponse("/admin/photos", status_code=303)


@router.post("/photos/bulk-action")
async def photos_bulk_action(
    request: Request,
    action: str = Form(...),
    photo_ids: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    from sqlalchemy import update as sa_update
    ids = []
    for pid in photo_ids:
        try:
            ids.append(uuid.UUID(pid))
        except ValueError:
            continue

    if ids:
        if action == "delete":
            await db.execute(delete(Photo).where(Photo.id.in_(ids)))
        elif action == "unpublish":
            await db.execute(sa_update(Photo).where(Photo.id.in_(ids)).values(is_published=False))
        await db.commit()

    return RedirectResponse("/admin/photos", status_code=303)


# ── Albums ─────────────────────────────────────────────────────────────────────

@router.get("/albums", response_class=HTMLResponse)
async def albums_list(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    from sqlalchemy.orm import selectinload
    albums = (await db.execute(
        select(Album).options(selectinload(Album.space)).order_by(Album.sort_order)
    )).scalars().all()
    spaces = (await db.execute(select(Space).order_by(Space.sort_order))).scalars().all()
    return templates.TemplateResponse("admin/albums.html", _admin_ctx(request, albums=albums, spaces=spaces))


@router.post("/albums/create")
async def create_album(
    request: Request,
    title: str = Form(...),
    slug: str = Form(...),
    description: str = Form(default=""),
    space_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    album = Album(
        title=title,
        slug=slug,
        description=description or None,
        space_id=uuid.UUID(space_id) if space_id else None,
    )
    db.add(album)
    await db.commit()
    return RedirectResponse("/admin/albums", status_code=303)


@router.get("/albums/{album_id}/edit", response_class=HTMLResponse)
async def edit_album_form(album_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    album = (await db.execute(select(Album).where(Album.id == uuid.UUID(album_id)))).scalar_one_or_none()
    if not album:
        raise HTTPException(status_code=404)

    album_photos_q = (
        select(Photo, AlbumPhoto.position)
        .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
        .where(AlbumPhoto.album_id == album.id)
        .order_by(AlbumPhoto.position)
    )
    album_photos = [(r[0], r[1]) for r in (await db.execute(album_photos_q)).all()]

    from app.services.album_token import share_token
    from app.config import get_settings as _gs
    share_link = None
    if album.is_private and album.password_hash:
        tok = share_token(_gs().secret_key, album.password_hash)
        share_link = f"/albums/{album.slug}/{tok}"

    spaces = (await db.execute(select(Space).order_by(Space.sort_order))).scalars().all()
    return templates.TemplateResponse("admin/album_edit.html", _admin_ctx(
        request, album=album, album_photos=album_photos,
        share_link=share_link, spaces=spaces,
    ))


@router.post("/albums/{album_id}/edit")
async def edit_album(
    album_id: str,
    request: Request,
    title: str = Form(...),
    slug: str = Form(...),
    description: str = Form(default=""),
    is_published: str = Form(default=""),
    is_private: str = Form(default=""),
    new_password: str = Form(default=""),
    remove_password: str = Form(default=""),
    sort_order: int = Form(default=0),
    cover_photo_id: str = Form(default=""),
    space_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    album = (await db.execute(select(Album).where(Album.id == uuid.UUID(album_id)))).scalar_one_or_none()
    if not album:
        raise HTTPException(status_code=404)

    publishing = is_published == "on"
    album.title = title
    album.slug = slug
    album.description = description or None
    album.is_published = publishing
    album.is_private = is_private == "on"
    album.sort_order = sort_order
    album.cover_photo_id = uuid.UUID(cover_photo_id) if cover_photo_id else None
    new_space_id = uuid.UUID(space_id) if space_id else None
    album.space_id = new_space_id
    if new_space_id:
        space = (await db.execute(select(Space).where(Space.id == new_space_id))).scalar_one_or_none()
        if space and space.is_private:
            album.is_private = True

    if remove_password == "1":
        album.password_hash = None
    elif new_password:
        import bcrypt as _bcrypt
        album.password_hash = await asyncio.to_thread(
            lambda: _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt(rounds=12)).decode()
        )

    if publishing:
        photo_ids_in_album = (await db.execute(
            select(AlbumPhoto.photo_id).where(AlbumPhoto.album_id == album.id)
        )).scalars().all()
        if photo_ids_in_album:
            from sqlalchemy import update
            await db.execute(
                update(Photo).where(Photo.id.in_(photo_ids_in_album)).values(is_published=True)
            )

    await db.commit()
    return RedirectResponse("/admin/albums", status_code=303)


@router.post("/albums/{album_id}/photos/add")
async def add_photos_to_album(
    album_id: str,
    request: Request,
    photo_ids: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    aid = uuid.UUID(album_id)
    album = (await db.execute(select(Album).where(Album.id == aid))).scalar_one_or_none()
    if not album:
        raise HTTPException(status_code=404)

    max_pos_row = await db.execute(
        select(func.coalesce(func.max(AlbumPhoto.position), -1)).where(AlbumPhoto.album_id == aid)
    )
    pos = max_pos_row.scalar() + 1

    added_photo_uuids = []
    for pid in photo_ids:
        try:
            photo_uuid = uuid.UUID(pid)
        except ValueError:
            continue
        existing = (await db.execute(
            select(AlbumPhoto).where(and_(AlbumPhoto.album_id == aid, AlbumPhoto.photo_id == photo_uuid))
        )).scalar_one_or_none()
        if not existing:
            db.add(AlbumPhoto(album_id=aid, photo_id=photo_uuid, position=pos))
            pos += 1
            added_photo_uuids.append(photo_uuid)

    # Auto-publish photos when added to a published album
    if album.is_published and added_photo_uuids:
        from sqlalchemy import update
        await db.execute(
            update(Photo).where(Photo.id.in_(added_photo_uuids)).values(is_published=True)
        )

    await db.commit()
    return RedirectResponse(f"/admin/albums/{album_id}/edit", status_code=303)


@router.post("/albums/{album_id}/photos/{photo_id}/remove")
async def remove_photo_from_album(album_id: str, photo_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    await db.execute(
        delete(AlbumPhoto).where(
            and_(AlbumPhoto.album_id == uuid.UUID(album_id), AlbumPhoto.photo_id == uuid.UUID(photo_id))
        )
    )
    await db.commit()
    return RedirectResponse(f"/admin/albums/{album_id}/edit", status_code=303)


@router.post("/albums/{album_id}/photos/remove-bulk")
async def remove_photos_from_album_bulk(
    album_id: str,
    request: Request,
    photo_ids: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    aid = uuid.UUID(album_id)
    ids = [uuid.UUID(pid) for pid in photo_ids if pid]
    if ids:
        await db.execute(
            delete(AlbumPhoto).where(
                and_(AlbumPhoto.album_id == aid, AlbumPhoto.photo_id.in_(ids))
            )
        )
        await db.commit()
    return RedirectResponse(f"/admin/albums/{album_id}/edit", status_code=303)


@router.post("/albums/{album_id}/photos/{photo_id}/move")
async def move_photo_in_album(
    album_id: str,
    photo_id: str,
    direction: str = Form(...),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    aid = uuid.UUID(album_id)
    pid = uuid.UUID(photo_id)

    rows_q = select(AlbumPhoto).where(AlbumPhoto.album_id == aid).order_by(AlbumPhoto.position)
    rows = (await db.execute(rows_q)).scalars().all()
    idx = next((i for i, r in enumerate(rows) if r.photo_id == pid), None)

    if idx is not None:
        if direction == "up" and idx > 0:
            rows[idx].position, rows[idx - 1].position = rows[idx - 1].position, rows[idx].position
        elif direction == "down" and idx < len(rows) - 1:
            rows[idx].position, rows[idx + 1].position = rows[idx + 1].position, rows[idx].position
        await db.commit()

    return RedirectResponse(f"/admin/albums/{album_id}/edit", status_code=303)


@router.post("/albums/{album_id}/set-cover")
async def set_album_cover(album_id: str, request: Request, photo_id: str = Form(...), db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)
    album = (await db.execute(select(Album).where(Album.id == uuid.UUID(album_id)))).scalar_one_or_none()
    if not album:
        raise HTTPException(status_code=404)
    album.cover_photo_id = uuid.UUID(photo_id)
    await db.commit()
    return RedirectResponse(f"/admin/albums/{album_id}/edit", status_code=303)


@router.post("/albums/{album_id}/regen-thumbs")
async def regen_album_thumbs(album_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    settings = get_settings()
    rows = (await db.execute(
        select(Photo.id, Photo.filepath)
        .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
        .where(AlbumPhoto.album_id == uuid.UUID(album_id))
        .order_by(AlbumPhoto.position)
    )).all()

    async def stream():
        total = len(rows)
        errors = 0
        for i, (photo_id, filepath) in enumerate(rows):
            result = await asyncio.to_thread(generate_thumbnail, str(photo_id), filepath, settings.thumbs_dir, True)
            if not result:
                errors += 1
            yield f"data: {json.dumps({'current': i+1, 'total': total})}\n\n"
        yield f"data: {json.dumps({'done': True, 'total': total, 'errors': errors})}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/albums/{album_id}/delete")
async def delete_album(album_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    album = (await db.execute(select(Album).where(Album.id == uuid.UUID(album_id)))).scalar_one_or_none()
    if album:
        await db.delete(album)
        await db.commit()

    return RedirectResponse("/admin/albums", status_code=303)


# ── Spaces ────────────────────────────────────────────────────────────────────

@router.get("/spaces", response_class=HTMLResponse)
async def spaces_list(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    from sqlalchemy.orm import selectinload
    spaces = (await db.execute(
        select(Space).options(selectinload(Space.albums)).order_by(Space.sort_order)
    )).scalars().all()
    return templates.TemplateResponse("admin/spaces.html", _admin_ctx(request, spaces=spaces))


@router.post("/spaces/create")
async def create_space(
    request: Request,
    title: str = Form(...),
    slug: str = Form(...),
    description: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    space = Space(title=title, slug=slug, description=description or None)
    db.add(space)
    await db.commit()
    return RedirectResponse("/admin/spaces", status_code=303)


@router.get("/spaces/{space_id}/edit", response_class=HTMLResponse)
async def edit_space_form(space_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    from sqlalchemy.orm import selectinload
    sid = uuid.UUID(space_id)
    space = (await db.execute(
        select(Space).where(Space.id == sid).options(selectinload(Space.albums))
    )).scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=404)

    space_album_ids = {a.id for a in space.albums}
    available_albums = (await db.execute(
        select(Album).where(Album.space_id == None).order_by(Album.sort_order)
    )).scalars().all()

    from app.services.album_token import share_token as _share_token
    share_link = None
    if space.is_private and space.password_hash:
        tok = _share_token(get_settings().secret_key, space.password_hash)
        share_link = f"/spaces/{space.slug}/{tok}"

    return templates.TemplateResponse("admin/space_edit.html", _admin_ctx(
        request, space=space, available_albums=available_albums,
        space_album_ids=space_album_ids, share_link=share_link,
    ))


@router.post("/spaces/{space_id}/edit")
async def edit_space(
    space_id: str,
    request: Request,
    title: str = Form(...),
    slug: str = Form(...),
    description: str = Form(default=""),
    is_published: str = Form(default=""),
    is_private: str = Form(default=""),
    new_password: str = Form(default=""),
    remove_password: str = Form(default=""),
    sort_order: int = Form(default=0),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    space = (await db.execute(select(Space).where(Space.id == uuid.UUID(space_id)))).scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=404)

    space.title = title
    space.slug = slug
    space.description = description or None
    space.is_published = is_published == "on"
    becoming_private = is_private == "on"
    space.is_private = becoming_private
    space.sort_order = sort_order

    # If the space is being made private, propagate to all its albums
    if becoming_private:
        from sqlalchemy import update
        await db.execute(
            update(Album).where(Album.space_id == space.id).values(is_private=True)
        )

    if remove_password == "1":
        space.password_hash = None
    elif new_password:
        import bcrypt as _bcrypt
        space.password_hash = await asyncio.to_thread(
            lambda: _bcrypt.hashpw(new_password.encode(), _bcrypt.gensalt(rounds=12)).decode()
        )

    await db.commit()
    return RedirectResponse(f"/admin/spaces/{space_id}/edit?saved=1", status_code=303)


@router.post("/spaces/{space_id}/albums/add")
async def add_albums_to_space(
    space_id: str,
    request: Request,
    album_ids: list[str] = Form(default=[]),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    sid = uuid.UUID(space_id)
    space = (await db.execute(select(Space).where(Space.id == sid))).scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=404)

    for aid_str in album_ids:
        try:
            aid = uuid.UUID(aid_str)
        except ValueError:
            continue
        album = (await db.execute(select(Album).where(Album.id == aid))).scalar_one_or_none()
        if album:
            album.space_id = sid
            if space.is_private:
                album.is_private = True
    await db.commit()
    return RedirectResponse(f"/admin/spaces/{space_id}/edit", status_code=303)


@router.post("/spaces/{space_id}/albums/{album_id}/remove")
async def remove_album_from_space(
    space_id: str, album_id: str, request: Request, db: AsyncSession = Depends(get_db)
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    album = (await db.execute(select(Album).where(
        and_(Album.id == uuid.UUID(album_id), Album.space_id == uuid.UUID(space_id))
    ))).scalar_one_or_none()
    if album:
        album.space_id = None
        await db.commit()
    return RedirectResponse(f"/admin/spaces/{space_id}/edit", status_code=303)


@router.post("/spaces/{space_id}/delete")
async def delete_space(space_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    space = (await db.execute(select(Space).where(Space.id == uuid.UUID(space_id)))).scalar_one_or_none()
    if space:
        await db.delete(space)
        await db.commit()
    return RedirectResponse("/admin/spaces", status_code=303)


# ── Posts ──────────────────────────────────────────────────────────────────────

@router.get("/posts", response_class=HTMLResponse)
async def posts_list(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    posts = (await db.execute(select(Post).order_by(Post.pinned.desc(), Post.created_at.desc()))).scalars().all()
    return templates.TemplateResponse("admin/posts.html", _admin_ctx(request, posts=posts))


@router.get("/posts/create", response_class=HTMLResponse)
async def create_post_form(request: Request, post_type: str = "announcement"):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)
    return templates.TemplateResponse("admin/post_edit.html", _admin_ctx(request, post=None, post_type=post_type))


@router.post("/posts/create")
async def create_post(
    request: Request,
    title: str = Form(...),
    slug: str = Form(...),
    body: str = Form(default=""),
    excerpt: str = Form(default=""),
    post_type: str = Form(...),
    is_published: str = Form(default=""),
    pinned: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    post = Post(
        title=title,
        slug=slug,
        body=body,
        excerpt=excerpt or None,
        post_type=PostType(post_type),
        is_published=is_published == "on",
        pinned=pinned == "on",
    )
    db.add(post)
    await db.commit()
    return RedirectResponse("/admin/posts", status_code=303)


@router.get("/posts/{post_id}/edit", response_class=HTMLResponse)
async def edit_post_form(post_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    post = (await db.execute(select(Post).where(Post.id == uuid.UUID(post_id)))).scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404)

    return templates.TemplateResponse("admin/post_edit.html", _admin_ctx(request, post=post, post_type=post.post_type.value))


@router.post("/posts/{post_id}/edit")
async def edit_post(
    post_id: str,
    request: Request,
    title: str = Form(...),
    slug: str = Form(...),
    body: str = Form(default=""),
    excerpt: str = Form(default=""),
    post_type: str = Form(...),
    is_published: str = Form(default=""),
    pinned: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    post = (await db.execute(select(Post).where(Post.id == uuid.UUID(post_id)))).scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404)

    post.title = title
    post.slug = slug
    post.body = body
    post.excerpt = excerpt or None
    post.post_type = PostType(post_type)
    post.is_published = is_published == "on"
    post.pinned = pinned == "on"
    await db.commit()
    return RedirectResponse("/admin/posts", status_code=303)


@router.get("/api/available-photos")
async def api_available_photos(
    request: Request,
    album_id: str = "",
    page: int = 1,
    q: str = "",
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        raise HTTPException(status_code=401)

    PAGE = 48
    base_q = select(Photo)
    if album_id:
        try:
            aid = uuid.UUID(album_id)
            in_album = select(AlbumPhoto.photo_id).where(AlbumPhoto.album_id == aid)
            base_q = base_q.where(Photo.id.not_in(in_album))
        except ValueError:
            pass
    if q:
        base_q = base_q.where(Photo.filename.ilike(f"%{q}%"))
    base_q = base_q.order_by(Photo.uploaded_at.desc())

    count_q = select(func.count()).select_from(base_q.subquery())
    total = (await db.execute(count_q)).scalar()
    total_pages = max(1, math.ceil(total / PAGE))
    page = min(max(1, page), total_pages)
    photos = (await db.execute(base_q.offset((page - 1) * PAGE).limit(PAGE))).scalars().all()

    return JSONResponse({
        "photos": [{"id": str(p.id), "filename": p.filename, "w": p.exif_width, "h": p.exif_height} for p in photos],
        "page": page, "total_pages": total_pages, "has_more": page < total_pages,
    })


@router.get("/api/folder-files")
async def api_folder_files(
    request: Request,
    path: str = "",
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        raise HTTPException(status_code=401)

    settings = get_settings()
    abs_path = os.path.realpath(os.path.join(settings.photos_dir, path))
    if not abs_path.startswith(os.path.realpath(settings.photos_dir)):
        raise HTTPException(status_code=400)

    existing = {row[0] for row in (await db.execute(select(Photo.filepath))).all()}
    files = _collect_files(abs_path, recursive=False)
    return JSONResponse({
        "files": [{"filename": fname, "imported": fpath in existing} for fpath, fname in files]
    })


@router.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    total = (await db.execute(select(func.count()).select_from(Photo))).scalar() or 0
    published = (await db.execute(select(func.count()).select_from(Photo).where(Photo.is_published == True))).scalar() or 0
    total_size = (await db.execute(select(func.coalesce(func.sum(Photo.file_size_bytes), 0)))).scalar() or 0
    cameras_distinct = (await db.execute(
        select(func.count(func.distinct(Photo.exif_camera))).where(Photo.exif_camera.isnot(None))
    )).scalar() or 0
    lenses_distinct = (await db.execute(
        select(func.count(func.distinct(Photo.exif_lens))).where(Photo.exif_lens.isnot(None))
    )).scalar() or 0

    async def top_values(col, limit=10):
        rows = (await db.execute(
            select(col, func.count().label("n"))
            .where(col.isnot(None))
            .group_by(col).order_by(func.count().desc()).limit(limit)
        )).all()
        return [(str(r[0]), r[1]) for r in rows]

    cameras = await top_values(Photo.exif_camera, 10)
    lenses = await top_values(Photo.exif_lens, 10)
    focal_lengths = await top_values(Photo.exif_focal_length, 15)
    apertures = await top_values(Photo.exif_aperture, 12)

    iso_vals = (await db.execute(select(Photo.exif_iso).where(Photo.exif_iso.isnot(None)))).scalars().all()
    iso_buckets_map = {"<200": 0, "200–399": 0, "400–799": 0, "800–1599": 0, "1600–3199": 0, "≥3200": 0}
    for v in iso_vals:
        if v < 200: iso_buckets_map["<200"] += 1
        elif v < 400: iso_buckets_map["200–399"] += 1
        elif v < 800: iso_buckets_map["400–799"] += 1
        elif v < 1600: iso_buckets_map["800–1599"] += 1
        elif v < 3200: iso_buckets_map["1600–3199"] += 1
        else: iso_buckets_map["≥3200"] += 1
    iso_buckets = [(k, v) for k, v in iso_buckets_map.items()]

    timeline_rows = (await db.execute(
        select(
            func.date_trunc("month", Photo.taken_at).label("month"),
            func.count().label("n")
        )
        .where(Photo.taken_at.isnot(None))
        .group_by("month").order_by("month")
    )).all()
    timeline = [(r.month, r.n) for r in timeline_rows if r.month]

    def fmt_bytes(b):
        if b >= 1_073_741_824: return (f"{b / 1_073_741_824:.1f}", "GB")
        if b >= 1_048_576: return (f"{b / 1_048_576:.0f}", "MB")
        return (f"{b / 1024:.0f}", "KB")

    storage_value, storage_unit = fmt_bytes(total_size)
    avg_size_value, avg_size_unit = fmt_bytes(total_size // total) if total else ("0", "KB")
    pct_published = round((published / total) * 100) if total else 0
    top_camera = cameras[0] if cameras else None
    top_lens = lenses[0] if lenses else None
    busiest = max(timeline, key=lambda r: r[1]) if timeline else None

    ctx = _admin_ctx(request,
        total=total, published=published, draft=total - published,
        pct_published=pct_published,
        storage_value=storage_value, storage_unit=storage_unit,
        avg_size_value=avg_size_value, avg_size_unit=avg_size_unit,
        cameras_distinct=cameras_distinct, lenses_distinct=lenses_distinct,
        top_camera=top_camera, top_lens=top_lens, busiest=busiest,
        cameras=cameras, lenses=lenses,
        focal_lengths=focal_lengths, apertures=apertures,
        iso_buckets=iso_buckets, timeline=timeline,
    )
    return templates.TemplateResponse("admin/stats.html", ctx)


@router.post("/posts/{post_id}/delete")
async def delete_post(post_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    post = (await db.execute(select(Post).where(Post.id == uuid.UUID(post_id)))).scalar_one_or_none()
    if post:
        await db.delete(post)
        await db.commit()

    return RedirectResponse("/admin/posts", status_code=303)

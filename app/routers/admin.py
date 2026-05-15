import asyncio
import os
import math
import uuid
import shutil

import json

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, delete
import markdown as md

from app.database import get_db
from app.models.photo import Photo
from app.models.album import Album, AlbumPhoto
from app.models.post import Post, PostType
from app.routers.auth import get_current_admin
from app.services.scanner import scan_photos_dir, scan_folder_flat, get_folder_tree, ingest_files_stream, _collect_files
from app.services.exif import extract_exif
from app.services.thumbnail import generate_thumbnail
from app.config import get_settings
from app.templates_env import templates

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

    total_photos = (await db.execute(select(func.count()).select_from(Photo))).scalar()
    published_photos = (await db.execute(select(func.count()).select_from(Photo).where(Photo.is_published == True))).scalar()
    total_albums = (await db.execute(select(func.count()).select_from(Album))).scalar()
    total_posts = (await db.execute(select(func.count()).select_from(Post).where(Post.post_type == PostType.announcement))).scalar()

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
        recent_photos=recent_photos,
        announcements=announcements,
    ))


# ── Folder browser ────────────────────────────────────────────────────────────

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

    # Resolve or create album — commit immediately so the ID is stable before streaming
    target_album_id: uuid.UUID | None = None
    if new_album_title and new_album_slug:
        new_album = Album(
            title=new_album_title,
            slug=new_album_slug,
            description=f"Imported from {os.path.basename(abs_target)}",
        )
        db.add(new_album)
        await db.commit()
        target_album_id = new_album.id
    elif album_id:
        target_album_id = uuid.UUID(album_id)

    files = _collect_files(abs_target, recursive=(recursive == "on"))
    parent = os.path.dirname(folder_path) if folder_path else ""

    async def event_stream():
        new_photo_ids = []
        async for event in ingest_files_stream(files, db):
            yield f"data: {json.dumps(event)}\n\n"
            if event.get("done"):
                new_photo_ids = [uuid.UUID(p) for p in event.get("photo_ids", [])]

        if target_album_id and new_photo_ids:
            max_pos = (await db.execute(
                select(func.coalesce(func.max(AlbumPhoto.position), -1))
                .where(AlbumPhoto.album_id == target_album_id)
            )).scalar()
            for i, pid in enumerate(new_photo_ids):
                db.add(AlbumPhoto(album_id=target_album_id, photo_id=pid, position=max_pos + 1 + i))
            await db.commit()

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"X-Folder-Parent": parent})


# ── Photos ─────────────────────────────────────────────────────────────────────

@router.get("/photos", response_class=HTMLResponse)
async def photos_list(
    request: Request,
    page: int = 1,
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    total = (await db.execute(select(func.count()).select_from(Photo))).scalar()
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    photos = (await db.execute(
        select(Photo).order_by(Photo.uploaded_at.desc()).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)
    )).scalars().all()

    return templates.TemplateResponse("admin/photos.html", _admin_ctx(
        request,
        photos=photos,
        page=page,
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


# ── Albums ─────────────────────────────────────────────────────────────────────

@router.get("/albums", response_class=HTMLResponse)
async def albums_list(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    albums = (await db.execute(select(Album).order_by(Album.sort_order))).scalars().all()
    return templates.TemplateResponse("admin/albums.html", _admin_ctx(request, albums=albums))


@router.post("/albums/create")
async def create_album(
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

    album = Album(title=title, slug=slug, description=description or None)
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

    # All published photos + which are in this album
    all_photos = (await db.execute(select(Photo).order_by(Photo.uploaded_at.desc()))).scalars().all()
    album_photos_q = (
        select(Photo, AlbumPhoto.position)
        .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
        .where(AlbumPhoto.album_id == album.id)
        .order_by(AlbumPhoto.position)
    )
    album_photos = [(r[0], r[1]) for r in (await db.execute(album_photos_q)).all()]
    album_photo_ids = {p.id for p, _ in album_photos}

    return templates.TemplateResponse("admin/album_edit.html", _admin_ctx(
        request, album=album, album_photos=album_photos, all_photos=all_photos, album_photo_ids=album_photo_ids
    ))


@router.post("/albums/{album_id}/edit")
async def edit_album(
    album_id: str,
    request: Request,
    title: str = Form(...),
    slug: str = Form(...),
    description: str = Form(default=""),
    is_published: str = Form(default=""),
    sort_order: int = Form(default=0),
    cover_photo_id: str = Form(default=""),
    db: AsyncSession = Depends(get_db),
):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    album = (await db.execute(select(Album).where(Album.id == uuid.UUID(album_id)))).scalar_one_or_none()
    if not album:
        raise HTTPException(status_code=404)

    album.title = title
    album.slug = slug
    album.description = description or None
    album.is_published = is_published == "on"
    album.sort_order = sort_order
    album.cover_photo_id = uuid.UUID(cover_photo_id) if cover_photo_id else None
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
    post.post_type = PostType(post_type)
    post.is_published = is_published == "on"
    post.pinned = pinned == "on"
    await db.commit()
    return RedirectResponse("/admin/posts", status_code=303)


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

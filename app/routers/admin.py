import os
import math
import uuid
import shutil

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, delete
import markdown as md

from app.database import get_db
from app.models.photo import Photo
from app.models.album import Album, AlbumPhoto
from app.models.post import Post, PostType
from app.routers.auth import get_current_admin
from app.services.scanner import scan_photos_dir
from app.services.exif import extract_exif
from app.services.thumbnail import generate_thumbnail
from app.config import get_settings

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory="app/templates")

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

    return templates.TemplateResponse("admin/dashboard.html", _admin_ctx(
        request,
        total_photos=total_photos,
        published_photos=published_photos,
        total_albums=total_albums,
        total_posts=total_posts,
        recent_photos=recent_photos,
    ))


# ── Scan ──────────────────────────────────────────────────────────────────────

@router.post("/scan", response_class=HTMLResponse)
async def scan(request: Request, db: AsyncSession = Depends(get_db)):
    try:
        get_current_admin(request)
    except HTTPException:
        return RedirectResponse("/auth/login", status_code=302)

    settings = get_settings()
    result = await scan_photos_dir(settings.photos_dir, db)
    return RedirectResponse(f"/admin/photos?scan_new={result['new']}&scan_skipped={result['skipped']}", status_code=303)


# ── Photos ─────────────────────────────────────────────────────────────────────

@router.get("/photos", response_class=HTMLResponse)
async def photos_list(
    request: Request,
    page: int = 1,
    scan_new: int | None = None,
    scan_skipped: int | None = None,
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
        scan_new=scan_new,
        scan_skipped=scan_skipped,
    ))


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

        exif = extract_exif(dest)
        photo_id = uuid.uuid4()
        generate_thumbnail(str(photo_id), dest, settings.thumbs_dir)

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
    max_pos_row = await db.execute(
        select(func.coalesce(func.max(AlbumPhoto.position), -1)).where(AlbumPhoto.album_id == aid)
    )
    pos = max_pos_row.scalar() + 1

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

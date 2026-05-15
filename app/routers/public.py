import asyncio
import os
import math

import bcrypt
import markdown as md
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.photo import Photo
from app.models.album import Album, AlbumPhoto
from app.models.post import Post, PostType
from app.config import get_settings
from app.templates_env import templates

ALBUM_COOKIE = "album_access"


def _get_unlocked(request: Request) -> set[str]:
    token = request.cookies.get(ALBUM_COOKIE)
    if not token:
        return set()
    try:
        data = jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
        return set(data.get("u", []))
    except JWTError:
        return set()


def _make_cookie(unlocked: set[str]) -> str:
    return jwt.encode({"u": list(unlocked)}, get_settings().secret_key, algorithm="HS256")

router = APIRouter()

PLACEHOLDER = os.path.join(os.path.dirname(__file__), "..", "static", "placeholder.webp")
PAGE_SIZE = 24


async def _photo_queries(album: str | None, db: AsyncSession):
    """Return (data_query, count_query) for the given album filter."""
    from sqlalchemy import exists as sa_exists, or_

    if album:
        album_obj = (await db.execute(select(Album).where(Album.slug == album))).scalar_one_or_none()
        if album_obj:
            aid = album_obj.id
            data_q = (
                select(Photo)
                .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
                .where(and_(AlbumPhoto.album_id == aid, Photo.is_published == True))
                .order_by(AlbumPhoto.position)
            )
            count_q = (
                select(func.count(Photo.id))
                .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
                .where(and_(AlbumPhoto.album_id == aid, Photo.is_published == True))
            )
            return data_q, count_q

    # Main gallery: exclude photos that belong ONLY to private albums.
    # A photo is visible if it's not in any album, OR it's in at least one
    # public (non-private, published) album.
    in_public_album = sa_exists().where(
        and_(
            AlbumPhoto.photo_id == Photo.id,
            AlbumPhoto.album_id == Album.id,
            Album.is_private == False,
            Album.is_published == True,
        )
    )
    not_in_any_album = ~sa_exists().where(AlbumPhoto.photo_id == Photo.id)
    gallery_filter = and_(
        Photo.is_published == True,
        or_(not_in_any_album, in_public_album),
    )
    data_q = select(Photo).where(gallery_filter).order_by(Photo.uploaded_at.desc())
    count_q = select(func.count(Photo.id)).where(gallery_filter)
    return data_q, count_q


@router.get("/", response_class=HTMLResponse)
async def home(request: Request, album: str | None = None, page: int = 1, db: AsyncSession = Depends(get_db)):
    settings = get_settings()

    ann_q = select(Post).where(
        and_(Post.is_published == True, Post.post_type == PostType.announcement, Post.pinned == True)
    ).order_by(Post.created_at.desc())
    announcements = (await db.execute(ann_q)).scalars().all()

    albums_q = select(Album).where(and_(Album.is_published == True, Album.is_private == False)).order_by(Album.sort_order)
    albums = (await db.execute(albums_q)).scalars().all()

    data_q, count_q = await _photo_queries(album, db)
    total = (await db.execute(count_q)).scalar()
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(page, total_pages)
    photos = (await db.execute(data_q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).scalars().all()

    return templates.TemplateResponse("public/index.html", {
        "request": request,
        "photos": photos,
        "albums": albums,
        "announcements": announcements,
        "current_album": album,
        "page": page,
        "total_pages": total_pages,
        "settings": settings,
    })


@router.get("/api/photos")
async def api_photos(album: str | None = None, page: int = 1, db: AsyncSession = Depends(get_db)):
    data_q, count_q = await _photo_queries(album, db)
    total = (await db.execute(count_q)).scalar()
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(1, page), total_pages)
    photos = (await db.execute(data_q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).scalars().all()
    return JSONResponse({
        "photos": [{"id": str(p.id), "title": p.title or "", "filename": p.filename} for p in photos],
        "page": page,
        "total_pages": total_pages,
        "has_more": page < total_pages,
    })


@router.get("/albums", response_class=HTMLResponse)
async def albums_list(request: Request, db: AsyncSession = Depends(get_db)):
    q = (select(Album)
         .where(and_(Album.is_published == True, Album.is_private == False))
         .order_by(Album.sort_order)
         .options(selectinload(Album.cover_photo)))
    albums = (await db.execute(q)).scalars().all()
    return templates.TemplateResponse("public/albums.html", {"request": request, "albums": albums})


@router.get("/albums/{slug}/unlock", response_class=HTMLResponse)
async def album_unlock_page(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    album = (await db.execute(
        select(Album).where(and_(Album.slug == slug, Album.is_published == True, Album.is_private == True))
    )).scalar_one_or_none()
    if not album:
        raise HTTPException(status_code=404)
    if slug in _get_unlocked(request):
        return RedirectResponse(f"/albums/{slug}", status_code=302)
    return templates.TemplateResponse("public/album_unlock.html", {
        "request": request, "album": album, "error": False,
    })


@router.post("/albums/{slug}/unlock")
async def album_unlock(slug: str, request: Request, password: str = Form(...), db: AsyncSession = Depends(get_db)):
    album = (await db.execute(
        select(Album).where(and_(Album.slug == slug, Album.is_published == True, Album.is_private == True))
    )).scalar_one_or_none()
    if not album:
        raise HTTPException(status_code=404)

    valid = (
        album.password_hash is not None
        and await asyncio.to_thread(bcrypt.checkpw, password.encode(), album.password_hash.encode())
    )
    if not valid:
        return templates.TemplateResponse("public/album_unlock.html", {
            "request": request, "album": album, "error": True,
        }, status_code=401)

    unlocked = _get_unlocked(request)
    unlocked.add(slug)
    resp = RedirectResponse(f"/albums/{slug}", status_code=303)
    resp.set_cookie(ALBUM_COOKIE, _make_cookie(unlocked), httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    return resp


@router.get("/albums/{slug}", response_class=HTMLResponse)
async def album_detail(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    album = (await db.execute(
        select(Album).where(and_(Album.slug == slug, Album.is_published == True))
        .options(selectinload(Album.cover_photo))
    )).scalar_one_or_none()
    if not album:
        raise HTTPException(status_code=404)

    if album.is_private and slug not in _get_unlocked(request):
        return RedirectResponse(f"/albums/{slug}/unlock", status_code=302)

    q = (
        select(Photo)
        .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
        .where(and_(AlbumPhoto.album_id == album.id, Photo.is_published == True))
        .order_by(AlbumPhoto.position)
    )
    photos = (await db.execute(q)).scalars().all()

    return templates.TemplateResponse("public/album.html", {
        "request": request,
        "album": album,
        "photos": photos,
    })


@router.get("/photos/{photo_id}", response_class=HTMLResponse)
async def photo_detail(photo_id: str, request: Request, album: str | None = None, db: AsyncSession = Depends(get_db)):
    from uuid import UUID
    try:
        uid = UUID(photo_id)
    except ValueError:
        raise HTTPException(status_code=404)

    photo = (await db.execute(select(Photo).where(and_(Photo.id == uid, Photo.is_published == True)))).scalar_one_or_none()
    if not photo:
        raise HTTPException(status_code=404)

    prev_photo = next_photo = None
    album_obj = None
    if album:
        album_obj = (await db.execute(select(Album).where(Album.slug == album))).scalar_one_or_none()
        if album_obj:
            q = (
                select(Photo, AlbumPhoto.position)
                .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
                .where(and_(AlbumPhoto.album_id == album_obj.id, Photo.is_published == True))
                .order_by(AlbumPhoto.position)
            )
            rows = (await db.execute(q)).all()
            ordered = [r[0] for r in rows]
            idx = next((i for i, p in enumerate(ordered) if p.id == photo.id), None)
            if idx is not None:
                if idx > 0:
                    prev_photo = ordered[idx - 1]
                if idx < len(ordered) - 1:
                    next_photo = ordered[idx + 1]

    return templates.TemplateResponse("public/photo.html", {
        "request": request,
        "photo": photo,
        "album": album_obj,
        "prev_photo": prev_photo,
        "next_photo": next_photo,
        "album_slug": album,
    })


@router.get("/photos/{photo_id}/download")
async def photo_download(photo_id: str, db: AsyncSession = Depends(get_db)):
    from uuid import UUID
    try:
        uid = UUID(photo_id)
    except ValueError:
        raise HTTPException(status_code=404)

    photo = (await db.execute(select(Photo).where(and_(Photo.id == uid, Photo.is_published == True)))).scalar_one_or_none()
    if not photo or not photo.download_enabled:
        raise HTTPException(status_code=404)

    if not os.path.exists(photo.filepath):
        raise HTTPException(status_code=404)

    return FileResponse(
        path=photo.filepath,
        filename=photo.filename,
        media_type="application/octet-stream",
    )


@router.get("/photos/{photo_id}/thumb")
async def photo_thumb(photo_id: str, db: AsyncSession = Depends(get_db)):
    from uuid import UUID
    settings = get_settings()
    try:
        uid = UUID(photo_id)
    except ValueError:
        raise HTTPException(status_code=404)

    photo = (await db.execute(select(Photo).where(Photo.id == uid))).scalar_one_or_none()
    if not photo:
        raise HTTPException(status_code=404)

    thumb_path = os.path.join(settings.thumbs_dir, f"{photo_id}.webp")
    if os.path.exists(thumb_path):
        return FileResponse(thumb_path, media_type="image/webp")

    # Return placeholder
    placeholder = os.path.join("app", "static", "placeholder.webp")
    if os.path.exists(placeholder):
        return FileResponse(placeholder, media_type="image/webp")

    raise HTTPException(status_code=404)


@router.get("/about", response_class=HTMLResponse)
async def about(request: Request, db: AsyncSession = Depends(get_db)):
    post = (await db.execute(
        select(Post).where(and_(Post.slug == "about", Post.is_published == True, Post.post_type == PostType.page))
    )).scalar_one_or_none()

    body_html = md.markdown(post.body, extensions=["extra"]) if post else ""
    return templates.TemplateResponse("public/about.html", {
        "request": request,
        "post": post,
        "body_html": body_html,
    })


@router.get("/announcements", response_class=HTMLResponse)
async def announcements(request: Request, db: AsyncSession = Depends(get_db)):
    q = (
        select(Post)
        .where(and_(Post.is_published == True, Post.post_type == PostType.announcement))
        .order_by(Post.pinned.desc(), Post.created_at.desc())
    )
    posts = (await db.execute(q)).scalars().all()
    return templates.TemplateResponse("public/announcements.html", {
        "request": request,
        "posts": posts,
    })

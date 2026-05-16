import asyncio
import hmac
import os
import math
import random as _random

import bcrypt
import markdown as md
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, and_, func, literal, String
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.photo import Photo
from app.models.album import Album, AlbumPhoto
from app.models.post import Post, PostType
from app.models.space import Space
from app.config import get_settings
from app.services.album_token import share_token
from app.templates_env import templates

ALBUM_COOKIE = "album_access"
SPACE_COOKIE = "space_access"


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


def _get_unlocked_spaces(request: Request) -> set[str]:
    token = request.cookies.get(SPACE_COOKIE)
    if not token:
        return set()
    try:
        data = jwt.decode(token, get_settings().secret_key, algorithms=["HS256"])
        return set(data.get("u", []))
    except JWTError:
        return set()


def _make_space_cookie(unlocked: set[str]) -> str:
    return jwt.encode({"u": list(unlocked)}, get_settings().secret_key, algorithm="HS256")

router = APIRouter()

PLACEHOLDER = os.path.join(os.path.dirname(__file__), "..", "static", "placeholder.webp")
PAGE_SIZE = 24


async def _photo_queries(album: str | None, db: AsyncSession, seed: str = ""):
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
    order_expr = func.md5(func.cast(Photo.id, String).concat(literal(seed))) if seed else func.md5(func.cast(Photo.id, String))
    data_q = select(Photo).where(gallery_filter).order_by(order_expr)
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

    seed = str(_random.randint(100000, 999999)) if not album else ""
    data_q, count_q = await _photo_queries(album, db, seed)
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
        "gallery_seed": seed,
    })


@router.get("/api/photos")
async def api_photos(album: str | None = None, page: int = 1, seed: str = "", db: AsyncSession = Depends(get_db)):
    data_q, count_q = await _photo_queries(album, db, seed)
    total = (await db.execute(count_q)).scalar()
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(1, page), total_pages)
    photos = (await db.execute(data_q.offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE))).scalars().all()
    return JSONResponse({
        "photos": [{"id": str(p.id), "title": p.title or "", "filename": p.filename, "w": p.exif_width, "h": p.exif_height} for p in photos],
        "page": page,
        "total_pages": total_pages,
        "has_more": page < total_pages,
    })


@router.get("/albums", response_class=HTMLResponse)
async def albums_list(request: Request, db: AsyncSession = Depends(get_db)):
    # Only public (non-private) published spaces
    spaces_raw = (await db.execute(
        select(Space)
        .where(and_(Space.is_published == True, Space.is_private == False))
        .options(selectinload(Space.albums).selectinload(Album.cover_photo))
        .order_by(Space.sort_order)
    )).scalars().all()
    # Build (space, filtered_albums) pairs without mutating ORM relationship
    spaces = [
        (space, [a for a in space.albums if a.is_published and not a.is_private])
        for space in spaces_raw
    ]

    # Published albums not in any space
    q = (select(Album)
         .where(and_(Album.is_published == True, Album.is_private == False, Album.space_id == None))
         .order_by(Album.sort_order)
         .options(selectinload(Album.cover_photo)))
    standalone_albums = (await db.execute(q)).scalars().all()

    # Get earliest photo taken_at per album for display
    from sqlalchemy import label
    all_album_ids = [a.id for _, als in spaces for a in als] + [a.id for a in standalone_albums]
    album_dates = {}
    if all_album_ids:
        rows = (await db.execute(
            select(AlbumPhoto.album_id, func.min(Photo.taken_at).label("earliest"))
            .join(Photo, Photo.id == AlbumPhoto.photo_id)
            .where(AlbumPhoto.album_id.in_(all_album_ids))
            .group_by(AlbumPhoto.album_id)
        )).all()
        album_dates = {str(r.album_id): r.earliest for r in rows}

    return templates.TemplateResponse("public/albums.html", {
        "request": request,
        "spaces": spaces,
        "standalone_albums": standalone_albums,
        "album_dates": album_dates,
    })


@router.get("/spaces/{slug}/{token}", response_class=HTMLResponse)
async def space_access_via_token(slug: str, token: str, request: Request, db: AsyncSession = Depends(get_db)):
    space = (await db.execute(
        select(Space).where(and_(Space.slug == slug, Space.is_published == True, Space.is_private == True))
    )).scalar_one_or_none()
    if not space or not space.password_hash:
        raise HTTPException(status_code=404)

    expected = share_token(get_settings().secret_key, space.password_hash)
    if not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=404)

    unlocked = _get_unlocked_spaces(request)
    unlocked.add(slug)
    resp = RedirectResponse(f"/spaces/{slug}", status_code=303)
    resp.set_cookie(SPACE_COOKIE, _make_space_cookie(unlocked), httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    return resp


@router.get("/spaces/{slug}/unlock", response_class=HTMLResponse)
async def space_unlock_page(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    space = (await db.execute(
        select(Space).where(and_(Space.slug == slug, Space.is_published == True, Space.is_private == True))
    )).scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=404)
    if slug in _get_unlocked_spaces(request):
        return RedirectResponse(f"/spaces/{slug}", status_code=302)
    return templates.TemplateResponse("public/space_unlock.html", {
        "request": request, "space": space, "error": False,
    })


@router.post("/spaces/{slug}/unlock")
async def space_unlock(slug: str, request: Request, password: str = Form(...), db: AsyncSession = Depends(get_db)):
    space = (await db.execute(
        select(Space).where(and_(Space.slug == slug, Space.is_published == True, Space.is_private == True))
    )).scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=404)

    valid = (
        space.password_hash is not None
        and await asyncio.to_thread(bcrypt.checkpw, password.encode(), space.password_hash.encode())
    )
    if not valid:
        return templates.TemplateResponse("public/space_unlock.html", {
            "request": request, "space": space, "error": True,
        }, status_code=401)

    unlocked = _get_unlocked_spaces(request)
    unlocked.add(slug)
    resp = RedirectResponse(f"/spaces/{slug}", status_code=303)
    resp.set_cookie(SPACE_COOKIE, _make_space_cookie(unlocked), httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    return resp


@router.get("/spaces/{slug}", response_class=HTMLResponse)
async def space_detail(slug: str, request: Request, db: AsyncSession = Depends(get_db)):
    space = (await db.execute(
        select(Space)
        .where(and_(Space.slug == slug, Space.is_published == True))
        .options(selectinload(Space.albums).selectinload(Album.cover_photo))
    )).scalar_one_or_none()
    if not space:
        raise HTTPException(status_code=404)

    if space.is_private and slug not in _get_unlocked_spaces(request):
        return RedirectResponse(f"/spaces/{slug}/unlock", status_code=302)

    # All published albums are visible; private albums within an unlocked space
    # are auto-unlocked so clicking them doesn't ask for a second password.
    albums = [a for a in space.albums if a.is_published]
    private_slugs = {a.slug for a in albums if a.is_private}

    unlocked_albums = _get_unlocked(request)
    newly_unlocked = private_slugs - unlocked_albums

    resp = templates.TemplateResponse("public/space.html", {"request": request, "space": space, "albums": albums})
    if newly_unlocked:
        unlocked_albums = unlocked_albums | newly_unlocked
        resp.set_cookie(ALBUM_COOKIE, _make_cookie(unlocked_albums), httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    return resp


@router.get("/albums/{slug}/{token}", response_class=HTMLResponse)
async def album_access_via_token(slug: str, token: str, request: Request, db: AsyncSession = Depends(get_db)):
    album = (await db.execute(
        select(Album).where(and_(Album.slug == slug, Album.is_published == True, Album.is_private == True))
    )).scalar_one_or_none()
    if not album or not album.password_hash:
        raise HTTPException(status_code=404)

    expected = share_token(get_settings().secret_key, album.password_hash)
    if not hmac.compare_digest(expected, token):
        raise HTTPException(status_code=404)

    unlocked = _get_unlocked(request)
    unlocked.add(slug)
    resp = RedirectResponse(f"/albums/{slug}", status_code=303)
    resp.set_cookie(ALBUM_COOKIE, _make_cookie(unlocked), httponly=True, samesite="lax", max_age=30 * 24 * 3600)
    return resp


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


@router.get("/api/albums/{slug}/photos")
async def api_album_photos(slug: str, page: int = 1, db: AsyncSession = Depends(get_db)):
    album = (await db.execute(
        select(Album).where(and_(Album.slug == slug, Album.is_published == True))
    )).scalar_one_or_none()
    if not album:
        raise HTTPException(status_code=404)

    total = (await db.execute(
        select(func.count(Photo.id))
        .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
        .where(and_(AlbumPhoto.album_id == album.id, Photo.is_published == True))
    )).scalar()
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(1, page), total_pages)

    photos = (await db.execute(
        select(Photo)
        .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
        .where(and_(AlbumPhoto.album_id == album.id, Photo.is_published == True))
        .order_by(AlbumPhoto.position)
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    )).scalars().all()

    return JSONResponse({
        "photos": [{"id": str(p.id), "title": p.title or "", "filename": p.filename, "w": p.exif_width, "h": p.exif_height} for p in photos],
        "page": page,
        "total_pages": total_pages,
        "has_more": page < total_pages,
    })


@router.get("/albums/{slug}", response_class=HTMLResponse)
async def album_detail(slug: str, page: int = 1, request: Request = None, db: AsyncSession = Depends(get_db)):
    album = (await db.execute(
        select(Album).where(and_(Album.slug == slug, Album.is_published == True))
        .options(selectinload(Album.cover_photo))
    )).scalar_one_or_none()
    if not album:
        raise HTTPException(status_code=404)

    if album.is_private and slug not in _get_unlocked(request):
        return RedirectResponse(f"/albums/{slug}/unlock", status_code=302)

    total = (await db.execute(
        select(func.count(Photo.id))
        .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
        .where(and_(AlbumPhoto.album_id == album.id, Photo.is_published == True))
    )).scalar()
    total_pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(1, page), total_pages)

    photos = (await db.execute(
        select(Photo)
        .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
        .where(and_(AlbumPhoto.album_id == album.id, Photo.is_published == True))
        .order_by(AlbumPhoto.position)
        .limit(PAGE_SIZE)
    )).scalars().all()

    earliest_taken = (await db.execute(
        select(func.min(Photo.taken_at))
        .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
        .where(AlbumPhoto.album_id == album.id)
    )).scalar()

    return templates.TemplateResponse("public/album.html", {
        "request": request,
        "album": album,
        "photos": photos,
        "total": total,
        "total_pages": total_pages,
        "page": page,
        "earliest_taken": earliest_taken,
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
            # Get only this photo's position, then fetch the immediate neighbours
            pos_row = (await db.execute(
                select(AlbumPhoto.position)
                .where(and_(AlbumPhoto.album_id == album_obj.id, AlbumPhoto.photo_id == photo.id))
            )).scalar_one_or_none()
            if pos_row is not None:
                cur_pos = pos_row
                prev_row = (await db.execute(
                    select(Photo)
                    .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
                    .where(and_(AlbumPhoto.album_id == album_obj.id,
                                AlbumPhoto.position < cur_pos,
                                Photo.is_published == True))
                    .order_by(AlbumPhoto.position.desc())
                    .limit(1)
                )).scalar_one_or_none()
                next_row = (await db.execute(
                    select(Photo)
                    .join(AlbumPhoto, AlbumPhoto.photo_id == Photo.id)
                    .where(and_(AlbumPhoto.album_id == album_obj.id,
                                AlbumPhoto.position > cur_pos,
                                Photo.is_published == True))
                    .order_by(AlbumPhoto.position.asc())
                    .limit(1)
                )).scalar_one_or_none()
                prev_photo = prev_row
                next_photo = next_row

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
        mtime = int(os.path.getmtime(thumb_path))
        return FileResponse(
            thumb_path,
            media_type="image/webp",
            headers={"Cache-Control": "public, max-age=3600", "ETag": f'"{photo_id}-{mtime}"'},
        )

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

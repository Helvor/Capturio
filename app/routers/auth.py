from collections import defaultdict
from datetime import datetime, timedelta, timezone
import time

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
import bcrypt
from jose import JWTError, jwt

from app.config import get_settings

_login_attempts: dict[str, list[float]] = defaultdict(list)
_LOGIN_WINDOW = 300   # 5 minutes
_LOGIN_MAX = 10       # max attempts per window


def _check_rate_limit(ip: str) -> bool:
    now = time.time()
    attempts = [t for t in _login_attempts[ip] if now - t < _LOGIN_WINDOW]
    _login_attempts[ip] = attempts
    if len(attempts) >= _LOGIN_MAX:
        return False
    _login_attempts[ip].append(now)
    return True

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")

COOKIE_NAME = "access_token"


def _create_token(username: str, settings) -> str:
    expire = datetime.now(timezone.utc) + timedelta(days=settings.access_token_expire_days)
    return jwt.encode({"sub": username, "exp": expire}, settings.secret_key, algorithm=settings.algorithm)


def get_current_admin(request: Request):
    settings = get_settings()
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        username: str = payload.get("sub")
        if username != settings.admin_username:
            raise HTTPException(status_code=401, detail="Not authenticated")
        return username
    except JWTError:
        raise HTTPException(status_code=401, detail="Not authenticated")


@router.get("/auth/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("auth/login.html", {"request": request, "error": None})


@router.post("/auth/login")
async def login(
    request: Request,
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
):
    settings = get_settings()
    ip = request.client.host if request.client else "unknown"
    if not _check_rate_limit(ip):
        return HTMLResponse("Too many login attempts. Try again later.", status_code=429)

    valid_user = username == settings.admin_username
    valid_pass = bool(settings.admin_password_hash) and bcrypt.checkpw(
        password.encode("utf-8"), settings.admin_password_hash.encode("utf-8")
    )

    if not valid_user or not valid_pass:
        return templates.TemplateResponse(
            "auth/login.html",
            {"request": request, "error": "Invalid credentials"},
            status_code=401,
        )

    token = _create_token(username, settings)
    redirect = RedirectResponse(url="/admin", status_code=303)
    redirect.set_cookie(
        key=COOKIE_NAME,
        value=token,
        httponly=True,
        samesite="lax",
        secure=bool(settings.trusted_proxies),
        max_age=settings.access_token_expire_days * 86400,
    )
    return redirect


@router.post("/auth/logout")
async def logout():
    redirect = RedirectResponse(url="/auth/login", status_code=303)
    redirect.delete_cookie(COOKIE_NAME)
    return redirect


@router.get("/auth/me")
async def me(current_user: str = Depends(get_current_admin)):
    return {"username": current_user}

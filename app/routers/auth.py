from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from jose import JWTError, jwt
from passlib.context import CryptContext

from app.config import get_settings

router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

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
    valid_user = username == settings.admin_username
    valid_pass = bool(settings.admin_password_hash) and pwd_context.verify(
        password, settings.admin_password_hash
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

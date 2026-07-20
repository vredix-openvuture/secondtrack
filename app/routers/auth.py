from __future__ import annotations

import pyotp
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from ..auth import verify_password
from ..db import get_db, get_setting
from ..i18n import DEFAULT_LANG, make_translator
from ..models import User
from ..templating import templates

router = APIRouter()


def _login_ctx(request: Request, db: Session, **extra) -> dict:
    lang = get_setting(db, "language", DEFAULT_LANG) or DEFAULT_LANG
    data = {
        "request": request,
        "lang": lang,
        "_": make_translator(lang),
        "accent": get_setting(db, "accent", "#6d28d9") or "#6d28d9",
        "error": None,
        "stage": "password",
    }
    data.update(extra)
    return data


@router.get("/login")
async def login_page(request: Request, db: Session = Depends(get_db)):
    if request.session.get("user_id") and not request.session.get("pending_2fa"):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse("login.html", _login_ctx(request, db))


@router.post("/login")
async def login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    user = db.query(User).filter(User.username == username).first()
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            _login_ctx(request, db, error="Wrong username or password."),
            status_code=401,
        )

    if user.totp_enabled and user.totp_secret:
        request.session["pending_2fa"] = user.id
        return templates.TemplateResponse(
            "login.html", _login_ctx(request, db, stage="totp")
        )

    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.post("/login/2fa")
async def login_2fa(
    request: Request,
    code: str = Form(...),
    db: Session = Depends(get_db),
):
    user_id = request.session.get("pending_2fa")
    if not user_id:
        return RedirectResponse("/login", status_code=303)

    user = db.get(User, user_id)
    if not user or not user.totp_secret:
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    totp = pyotp.TOTP(user.totp_secret)
    if not totp.verify(code.strip(), valid_window=1):
        return templates.TemplateResponse(
            "login.html",
            _login_ctx(request, db, error="Invalid code.", stage="totp"),
            status_code=401,
        )

    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)

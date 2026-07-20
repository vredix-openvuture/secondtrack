from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from passlib.hash import bcrypt
from sqlalchemy.orm import Session

from .db import get_db
from .models import User


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.verify(plain, hashed)
    except ValueError:
        return False


def hash_password(plain: str) -> str:
    return bcrypt.hash(plain)


def get_current_user(request: Request, db: Session = Depends(get_db)) -> User | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.get(User, user_id)


class _RedirectToLogin(Exception):
    pass


def require_login(request: Request, db: Session = Depends(get_db)) -> User:
    """Dependency: returns the logged-in user, otherwise redirects to /login
    (or raises 401 for HTMX/partial requests)."""
    user = get_current_user(request, db)
    # A pending 2FA step counts as not-yet-authenticated.
    if user is None or request.session.get("pending_2fa"):
        if request.headers.get("HX-Request"):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="login required"
            )
        raise _RedirectToLogin()
    return user

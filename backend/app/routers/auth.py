"""Authentication: login and session.

Login issues a JWT; all other endpoints validate it via `current_user`.
There is no self-registration — only admins can create accounts.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from ..db import get_db
from ..limiter import limiter
from ..models import User
from ..schemas import ChangePasswordIn, LoginIn, SessionOut, TokenOut, UserOut
from ..security import (
    create_access_token, current_user, hash_password,
    projects_for_user, verify_password,
)
from .admin import project_out

router = APIRouter(tags=["session"])


@router.post("/api/auth/login", response_model=TokenOut)
@limiter.limit("5/5minutes")
def login(request: Request, payload: LoginIn, db: Session = Depends(get_db)):
    """Issue a JWT for valid credentials. No registration — admin creates accounts."""
    user = db.query(User).filter(User.email == str(payload.email)).first()
    if (
        user is None
        or not user.is_active
        or not user.password_hash
        or not verify_password(payload.password, user.password_hash)
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password.",
        )
    return TokenOut(access_token=create_access_token(user.id))


@router.post("/api/auth/change-password", status_code=204)
def change_password(
    payload: ChangePasswordIn,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    """Change the caller's own password. The current password must be supplied."""
    if not user.password_hash or not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password is incorrect.",
        )
    user.password_hash = hash_password(payload.new_password)
    db.commit()


@router.get("/api/auth/session", response_model=SessionOut)
def session(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Return the current session: the signed-in user, admin flag, and accessible projects."""
    projects = projects_for_user(db, user)
    return SessionOut(
        user=UserOut.model_validate(user),
        is_admin=user.is_admin,
        projects=[project_out(db, p) for p in projects],
    )

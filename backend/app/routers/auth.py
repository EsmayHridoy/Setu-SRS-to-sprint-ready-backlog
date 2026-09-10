"""Sign-in and session.

Placeholder authentication: the client lists accounts and picks one. Swapping
in SSO means replacing this router and `current_user` in security.py; nothing
else in the application reads identity directly.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..schemas import SessionOut, UserOut
from ..security import current_user, projects_for_user
from .admin import project_out

router = APIRouter(tags=["session"])


@router.get("/api/auth/accounts", response_model=list[UserOut])
def list_accounts(db: Session = Depends(get_db)):
    """Accounts available to sign in as. Replaced by SSO later."""
    users = db.query(User).filter(User.is_active.is_(True)).order_by(User.name).all()
    return [UserOut.model_validate(u) for u in users]


@router.get("/api/auth/session", response_model=SessionOut)
def session(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Return the current session: the signed-in user, whether they are an
    admin, and the projects their roles grant access to."""
    projects = projects_for_user(db, user)
    return SessionOut(
        user=UserOut.model_validate(user),
        is_admin=user.is_admin,
        projects=[project_out(db, p) for p in projects],
    )

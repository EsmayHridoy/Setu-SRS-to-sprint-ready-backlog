"""Identity and access control.

Authentication uses JWT Bearer tokens. The token is issued by POST /api/auth/login
and must be included in every request as `Authorization: Bearer <token>`.

Access to a project is never taken from the client. It is always recomputed
here as the union of the projects granted to the caller's roles.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import bcrypt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .models import Project, Role, User, role_projects, user_roles

settings = get_settings()

_bearer = HTTPBearer(auto_error=False)


# --- password helpers ---------------------------------------------------------

def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode(), bcrypt.gensalt(12)).decode()


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode(), hashed.encode())


# --- JWT helpers --------------------------------------------------------------

def create_access_token(user_id: str) -> str:
    expire = datetime.utcnow() + timedelta(minutes=settings.jwt_expire_minutes)
    return jwt.encode(
        {"sub": user_id, "exp": expire},
        settings.jwt_secret,
        algorithm=settings.jwt_algorithm,
    )


# --- FastAPI dependencies -----------------------------------------------------

def current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in to continue.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        payload = jwt.decode(
            credentials.credentials,
            settings.jwt_secret,
            algorithms=[settings.jwt_algorithm],
        )
        user_id: str | None = payload.get("sub")
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user = db.get(User, user_id)
    if user is None or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="That account is no longer active.",
        )
    return user


def require_admin(user: User = Depends(current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This area is limited to administrators.",
        )
    return user


def projects_for_user(db: Session, user: User) -> list[Project]:
    """Every project the user can reach, through any of their roles."""
    stmt = (
        select(Project)
        .join(role_projects, role_projects.c.project_id == Project.id)
        .join(Role, Role.id == role_projects.c.role_id)
        .join(user_roles, user_roles.c.role_id == Role.id)
        .where(user_roles.c.user_id == user.id, Project.status == "ACTIVE")
        .distinct()
        .order_by(Project.name)
    )
    return list(db.scalars(stmt).unique())


def authorised_project(
    project_id: str,
    db: Session = Depends(get_db),
    user: User = Depends(current_user),
) -> Project:
    """Load a project only if the caller's roles grant it.

    Returns 404 rather than 403: a user who cannot reach a project
    should not learn that it exists.
    """
    allowed = {p.id for p in projects_for_user(db, user)}
    if project_id not in allowed:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Project not found.",
        )
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                            detail="Project not found.")
    return project

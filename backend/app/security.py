"""Identity and access control.

Authentication is a placeholder: the client sends an `X-User-Id` header picked
from the sign-in list. When SSO arrives, only `current_user` changes — every
permission check below reads from the database and stays exactly as it is.

Access to a project is never taken from the client. It is always recomputed
here as the union of the projects granted to the caller's roles. A user may
hold several roles; they see every project any of those roles grants.
"""
from __future__ import annotations

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import Project, Role, User, role_projects, user_roles


def current_user(
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
    db: Session = Depends(get_db),
) -> User:
    if not x_user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Sign in to continue.",
        )
    user = db.get(User, x_user_id)
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

    Returns 404 rather than 403 on purpose: a user who cannot reach a project
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

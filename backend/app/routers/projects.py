"""Projects visible to the signed-in user."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import User
from ..schemas import ProjectOut
from ..security import current_user, projects_for_user
from .admin import project_out

router = APIRouter(prefix="/api/projects", tags=["projects"])


@router.get("", response_model=list[ProjectOut])
def my_projects(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """The union of every project granted by the caller's roles."""
    return [project_out(db, p) for p in projects_for_user(db, user)]

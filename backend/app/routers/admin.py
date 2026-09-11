"""Administration: roles, projects and user access.

Everything here requires a role with the admin flag. Access tokens are written
encrypted and never read back — responses carry only the last four characters.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from .. import crypto
from ..config import get_settings
from ..db import get_db
from ..models import AppSetting, Artifact, AuditEvent, Project, Role, User
from ..schemas import (
    AppSettingOut, AuditOut, ProjectIn, ProjectOut, ResetPasswordIn,
    RoleIn, RoleOut, UserIn, UserOut,
)
from ..security import hash_password, require_admin

# ---------------------------------------------------------------------------
# Settings managed through the UI. Order determines display order in the form.
# ---------------------------------------------------------------------------
_SETTINGS_META: dict[str, dict] = {
    "gemini_api_key": {
        "label": "Google / Gemini API Key",
        "sensitive": True,
        "attr": "gemini_api_key",
    },
    "gemini_model": {
        "label": "Gemini Model",
        "sensitive": False,
        "attr": "gemini_model",
    },
    "github_pat": {
        "label": "GitHub Personal Access Token",
        "sensitive": True,
        "attr": "github_pat",
    },
    "github_mcp_url": {
        "label": "GitHub MCP URL",
        "sensitive": False,
        "attr": "github_mcp_url",
    },
    "github_mcp_readonly": {
        "label": "GitHub MCP Read-only",
        "sensitive": False,
        "attr": "github_mcp_readonly",
    },
}

router = APIRouter(prefix="/api/admin", tags=["admin"])


def record(db: Session, actor: User, entity_type: str, entity_id: str,
           action: str, detail: str = "") -> None:
    db.add(AuditEvent(entity_type=entity_type, entity_id=entity_id,
                      actor_id=actor.id, actor_name=actor.name,
                      action=action, detail=detail))


def project_out(db: Session, project: Project) -> ProjectOut:
    data = ProjectOut.model_validate(project)
    data.artifact_count = (
        db.query(Artifact).filter(Artifact.project_id == project.id).count()
    )
    return data


def role_out(db: Session, role: Role) -> RoleOut:
    data = RoleOut.model_validate(role)
    data.user_count = len(role.users)
    return data


# --- roles -------------------------------------------------------------------

@router.get("/roles", response_model=list[RoleOut])
def list_roles(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List every role with its granted-project count, for the admin panel."""
    roles = db.query(Role).order_by(Role.name).all()
    return [role_out(db, r) for r in roles]


@router.post("/roles", response_model=RoleOut,
             status_code=status.HTTP_201_CREATED)
def create_role(payload: RoleIn, db: Session = Depends(get_db),
                actor: User = Depends(require_admin)):
    """Create a role, granting it the given projects. Names must be unique."""
    if db.query(Role).filter(Role.name == payload.name).first():
        raise HTTPException(409, f"A role named “{payload.name}” already exists.")
    role = Role(name=payload.name, description=payload.description,
                is_admin=payload.is_admin)
    role.projects = _load_projects(db, payload.project_ids)
    db.add(role)
    db.flush()
    record(db, actor, "role", role.id, "created", role.name)
    db.commit()
    return role_out(db, role)


@router.put("/roles/{role_id}", response_model=RoleOut)
def update_role(role_id: str, payload: RoleIn, db: Session = Depends(get_db),
                actor: User = Depends(require_admin)):
    """Rename a role and reset its description, admin flag and granted projects."""
    role = db.get(Role, role_id)
    if role is None:
        raise HTTPException(404, "Role not found.")
    clash = db.query(Role).filter(Role.name == payload.name,
                                  Role.id != role_id).first()
    if clash:
        raise HTTPException(409, f"A role named “{payload.name}” already exists.")

    role.name = payload.name
    role.description = payload.description
    role.is_admin = payload.is_admin
    role.projects = _load_projects(db, payload.project_ids)
    record(db, actor, "role", role.id, "updated",
           f"{len(role.projects)} project(s) granted")
    db.commit()
    return role_out(db, role)


@router.delete("/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_role(role_id: str, db: Session = Depends(get_db),
                actor: User = Depends(require_admin)):
    """Delete a role, but only once no users are still assigned to it."""
    role = db.get(Role, role_id)
    if role is None:
        raise HTTPException(404, "Role not found.")
    if role.users:
        raise HTTPException(
            409,
            f"“{role.name}” is still assigned to {len(role.users)} user(s). "
            "Remove it from them first.",
        )
    record(db, actor, "role", role.id, "deleted", role.name)
    db.delete(role)
    db.commit()


# --- projects ----------------------------------------------------------------

@router.get("/projects", response_model=list[ProjectOut])
def list_projects(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List every project with its artifact count, for the admin panel."""
    projects = db.query(Project).order_by(Project.name).all()
    return [project_out(db, p) for p in projects]


@router.post("/projects", response_model=ProjectOut,
             status_code=status.HTTP_201_CREATED)
def create_project(payload: ProjectIn, db: Session = Depends(get_db),
                   actor: User = Depends(require_admin)):
    """Create a project and store its (encrypted) repo access token. Names
    must be unique."""
    if db.query(Project).filter(Project.name == payload.name).first():
        raise HTTPException(409, f"A project named “{payload.name}” already exists.")
    project = Project(
        name=payload.name, description=payload.description,
        provider=payload.provider, repo_url=payload.repo_url,
        default_branch=payload.default_branch, status=payload.status,
    )
    _apply_token(project, payload.access_token)
    db.add(project)
    db.flush()
    record(db, actor, "project", project.id, "created", project.name)
    db.commit()
    return project_out(db, project)


@router.put("/projects/{project_id}", response_model=ProjectOut)
def update_project(project_id: str, payload: ProjectIn,
                   db: Session = Depends(get_db),
                   actor: User = Depends(require_admin)):
    """Update a project's details and optionally rotate its access token."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found.")
    clash = db.query(Project).filter(Project.name == payload.name,
                                     Project.id != project_id).first()
    if clash:
        raise HTTPException(409, f"A project named “{payload.name}” already exists.")

    project.name = payload.name
    project.description = payload.description
    project.provider = payload.provider
    project.repo_url = payload.repo_url
    project.default_branch = payload.default_branch
    project.status = payload.status
    changed_token = _apply_token(project, payload.access_token)
    record(db, actor, "project", project.id, "updated",
           "access token replaced" if changed_token else "details updated")
    db.commit()
    return project_out(db, project)


@router.delete("/projects/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_project(project_id: str, db: Session = Depends(get_db),
                   actor: User = Depends(require_admin)):
    """Delete a project and everything cascaded from it (artifacts, grants)."""
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found.")
    record(db, actor, "project", project.id, "deleted", project.name)
    db.delete(project)
    db.commit()


@router.post("/projects/{project_id}/reindex", response_model=ProjectOut)
def reindex(project_id: str, db: Session = Depends(get_db),
            actor: User = Depends(require_admin)):
    """Placeholder for the indexing job.

    The real version reads the repository with the stored token, chunks the
    source, schema and defect history, and writes embeddings. For now it only
    stamps the time so the interface can show index freshness.
    """
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "Project not found.")
    project.indexed_at = datetime.utcnow()
    record(db, actor, "project", project.id, "reindexed", project.name)
    db.commit()
    return project_out(db, project)


# --- users -------------------------------------------------------------------

@router.get("/users", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db), _: User = Depends(require_admin)):
    """List every user, for the admin panel."""
    return db.query(User).order_by(User.name).all()


@router.post("/users", response_model=UserOut,
             status_code=status.HTTP_201_CREATED)
def create_user(payload: UserIn, db: Session = Depends(get_db),
                actor: User = Depends(require_admin)):
    """Create a user and assign their roles. Email addresses must be unique.
    A password is required for the new account to be able to log in."""
    if not payload.password:
        raise HTTPException(400, "A password is required when creating a user.")
    if db.query(User).filter(User.email == str(payload.email)).first():
        raise HTTPException(409, "That email address is already in use.")
    user = User(
        name=payload.name, email=str(payload.email),
        job_title=payload.job_title, is_active=payload.is_active,
        password_hash=hash_password(payload.password),
    )
    user.roles = _load_roles(db, payload.role_ids)
    db.add(user)
    db.flush()
    record(db, actor, "user", user.id, "created", user.email)
    db.commit()
    return user


@router.put("/users/{user_id}", response_model=UserOut)
def update_user(user_id: str, payload: UserIn, db: Session = Depends(get_db),
                actor: User = Depends(require_admin)):
    """Update a user's details and roles, refusing to remove the last admin."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found.")
    clash = db.query(User).filter(User.email == str(payload.email),
                                  User.id != user_id).first()
    if clash:
        raise HTTPException(409, "That email address is already in use.")

    was_admin = user.is_admin
    user.name = payload.name
    user.email = str(payload.email)
    user.job_title = payload.job_title
    user.is_active = payload.is_active
    user.roles = _load_roles(db, payload.role_ids)
    if payload.password:
        user.password_hash = hash_password(payload.password)

    # Do not let an administrator lock every admin out of the panel.
    if was_admin and not user.is_admin and _admin_count(db, exclude=user.id) == 0:
        db.rollback()
        raise HTTPException(
            409,
            "This is the last account with administrator access. "
            "Give another user an admin role first.",
        )

    record(db, actor, "user", user.id, "updated",
           ", ".join(r.name for r in user.roles) or "no roles")
    db.commit()
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: str, db: Session = Depends(get_db),
                actor: User = Depends(require_admin)):
    """Delete a user, refusing to remove the last account with admin access."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found.")
    if user.is_admin and _admin_count(db, exclude=user.id) == 0:
        raise HTTPException(
            409, "This is the last account with administrator access.")
    record(db, actor, "user", user.id, "deleted", user.email)
    db.delete(user)
    db.commit()


@router.post("/users/{user_id}/reset-password", status_code=status.HTTP_204_NO_CONTENT)
def reset_password(user_id: str, payload: ResetPasswordIn,
                   db: Session = Depends(get_db),
                   actor: User = Depends(require_admin)):
    """Reset any user's password. No knowledge of the old password is required."""
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found.")
    user.password_hash = hash_password(payload.new_password)
    record(db, actor, "user", user.id, "password_reset", user.email)
    db.commit()


# --- audit -------------------------------------------------------------------

@router.get("/audit", response_model=list[AuditOut])
def audit_log(limit: int = 100, db: Session = Depends(get_db),
              _: User = Depends(require_admin)):
    """Return the most recent audit events (capped at 500), newest first."""
    return (db.query(AuditEvent)
            .order_by(AuditEvent.occurred_at.desc())
            .limit(min(limit, 500)).all())


# --- app settings ------------------------------------------------------------

@router.get("/app-settings", response_model=list[AppSettingOut])
def get_app_settings(db: Session = Depends(get_db),
                     _: User = Depends(require_admin)):
    """Return all configurable settings with their actual values (admin only)."""
    s = get_settings()
    result = []
    for key, meta in _SETTINGS_META.items():
        row = db.get(AppSetting, key)
        env_val = getattr(s, meta["attr"], "")
        if isinstance(env_val, bool):
            env_val = str(env_val).lower()

        if row and row.value:
            current = crypto.decrypt(row.value) if meta["sensitive"] else row.value
            current = current or ""
        else:
            current = env_val or ""

        result.append(AppSettingOut(
            key=key, label=meta["label"],
            is_sensitive=meta["sensitive"],
            is_set=bool(current),
            value=current,
        ))
    return result


@router.put("/app-settings", status_code=status.HTTP_204_NO_CONTENT)
def update_app_settings(payload: dict[str, str],
                        db: Session = Depends(get_db),
                        actor: User = Depends(require_admin)):
    """Save settings. All values are written as provided."""
    s = get_settings()
    changed: list[str] = []

    for key, value in payload.items():
        if key not in _SETTINGS_META:
            continue
        meta = _SETTINGS_META[key]

        row = db.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, is_sensitive=meta["sensitive"])
            db.add(row)

        stored = crypto.encrypt(value) if meta["sensitive"] else value
        row.value = stored
        row.updated_at = datetime.utcnow()
        changed.append(key)

        # Apply to live settings object immediately so the running process
        # picks up the change without a restart.
        attr = meta["attr"]
        if attr == "github_mcp_readonly":
            setattr(s, attr, value.lower() == "true")
        else:
            setattr(s, attr, value)

    if changed:
        record(db, actor, "app_settings", "global", "updated",
               ", ".join(changed))
        db.commit()


# --- helpers -----------------------------------------------------------------

def _load_projects(db: Session, ids: list[str]) -> list[Project]:
    if not ids:
        return []
    found = db.query(Project).filter(Project.id.in_(ids)).all()
    if len(found) != len(set(ids)):
        raise HTTPException(400, "One or more projects could not be found.")
    return found


def _load_roles(db: Session, ids: list[str]) -> list[Role]:
    if not ids:
        return []
    found = db.query(Role).filter(Role.id.in_(ids)).all()
    if len(found) != len(set(ids)):
        raise HTTPException(400, "One or more roles could not be found.")
    return found


def _admin_count(db: Session, exclude: str | None = None) -> int:
    users = db.query(User).filter(User.is_active.is_(True)).all()
    return sum(1 for u in users if u.is_admin and u.id != exclude)


def _apply_token(project: Project, token: str | None) -> bool:
    """None leaves the stored token alone; "" clears it; anything else replaces it."""
    if token is None:
        return False
    if token == "":
        project.token_encrypted = None
        project.token_last4 = None
        project.token_set_at = None
        return True
    project.token_encrypted = crypto.encrypt(token)
    project.token_last4 = token[-4:]
    project.token_set_at = datetime.utcnow()
    return True

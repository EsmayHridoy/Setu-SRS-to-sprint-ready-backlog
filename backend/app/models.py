"""Database tables.

Naming follows the build specification. Anything the AI layer will need later
(artifacts, citations) already exists here, so wiring the real model in does not
require a migration.
"""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean, Column, DateTime, ForeignKey, Integer, String, Table, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.utcnow()


# --- join tables -------------------------------------------------------------

user_roles = Table(
    "user_roles",
    Base.metadata,
    Column("user_id", String(36), ForeignKey("users.id", ondelete="CASCADE"),
           primary_key=True),
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"),
           primary_key=True),
)

role_projects = Table(
    "role_projects",
    Base.metadata,
    Column("role_id", String(36), ForeignKey("roles.id", ondelete="CASCADE"),
           primary_key=True),
    Column("project_id", String(36), ForeignKey("projects.id", ondelete="CASCADE"),
           primary_key=True),
)


# --- identity ----------------------------------------------------------------

class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    email: Mapped[str] = mapped_column(String(200), unique=True, nullable=False)
    job_title: Mapped[str] = mapped_column(String(120), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    roles = relationship("Role", secondary=user_roles, back_populates="users",
                         lazy="selectin")

    @property
    def is_admin(self) -> bool:
        return any(r.is_admin for r in self.roles)


class Role(Base):
    __tablename__ = "roles"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    # Grants access to the admin panel. Kept as a flag rather than a magic
    # role name so renaming a role can never silently remove admin access.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    users = relationship("User", secondary=user_roles, back_populates="roles",
                         lazy="selectin")
    projects = relationship("Project", secondary=role_projects,
                            back_populates="roles", lazy="selectin")


# --- projects ----------------------------------------------------------------

class Project(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    provider: Mapped[str] = mapped_column(String(20), default="GITHUB")
    repo_url: Mapped[str] = mapped_column(String(500), default="")
    default_branch: Mapped[str] = mapped_column(String(80), default="main")

    # The access token is encrypted at rest and never leaves the server.
    # Only the last four characters are ever returned by the API.
    token_encrypted: Mapped[str | None] = mapped_column(Text, nullable=True)
    token_last4: Mapped[str | None] = mapped_column(String(8), nullable=True)
    token_set_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    status: Mapped[str] = mapped_column(String(20), default="ACTIVE")
    indexed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    roles = relationship("Role", secondary=role_projects,
                         back_populates="projects", lazy="selectin")
    artifacts = relationship("Artifact", back_populates="project",
                             cascade="all, delete-orphan")


class Artifact(Base):
    """One piece of knowledge about an existing system.

    Populated by the indexer later. Seeded with sample rows now so the dummy
    answers can cite something real-looking.
    """
    __tablename__ = "artifacts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(20))          # CODE | SCHEMA | RCA
    source_ref: Mapped[str] = mapped_column(String(400))
    content: Mapped[str] = mapped_column(Text, default="")
    keywords: Mapped[str] = mapped_column(Text, default="")

    project = relationship("Project", back_populates="artifacts")


# --- chat --------------------------------------------------------------------

class Conversation(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(300), default="New conversation")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    messages = relationship("Message", back_populates="conversation",
                            cascade="all, delete-orphan",
                            order_by="Message.created_at")
    project = relationship("Project", lazy="joined")


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"),
        index=True)
    role: Mapped[str] = mapped_column(String(16))          # USER | ASSISTANT
    content: Mapped[str] = mapped_column(Text)
    # Set when the reply came from the placeholder generator rather than a model.
    is_placeholder: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    conversation = relationship("Conversation", back_populates="messages")
    citations = relationship("Citation", back_populates="message",
                             cascade="all, delete-orphan")


class Citation(Base):
    """Evidence attached to an assistant message.

    Every assistant reply that makes a claim about the codebase must carry at
    least one of these. The verifier that checks the quoted text actually
    appears in the artifact goes in analysis/citation_verifier.py later.
    """
    __tablename__ = "citations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    message_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    artifact_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(20))
    source_ref: Mapped[str] = mapped_column(String(400))
    quoted_span: Mapped[str] = mapped_column(Text, default="")
    verified: Mapped[bool] = mapped_column(Boolean, default=False)

    message = relationship("Message", back_populates="citations")


# --- audit -------------------------------------------------------------------

class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(String(60))
    entity_id: Mapped[str] = mapped_column(String(36))
    actor_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    actor_name: Mapped[str] = mapped_column(String(120), default="")
    action: Mapped[str] = mapped_column(String(60))
    detail: Mapped[str] = mapped_column(Text, default="")
    occurred_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    __table_args__ = (UniqueConstraint("id", name="uq_audit_id"),)

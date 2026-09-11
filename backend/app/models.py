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
    password_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
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
    # Set on the assistant reply to a document uploaded in chat: the plan of
    # businesses extracted from it, reviewed and vetted in place in the thread.
    business_plan_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("business_plans.id", ondelete="SET NULL"),
        nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    conversation = relationship("Conversation", back_populates="messages")
    citations = relationship("Citation", back_populates="message",
                             cascade="all, delete-orphan")
    business_plan = relationship("BusinessPlan", lazy="selectin")


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


# --- business plans -----------------------------------------------------------

class BusinessPlan(Base):
    __tablename__ = "business_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    project_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="CASCADE"))
    source_filename: Mapped[str] = mapped_column(String(300), default="")
    status: Mapped[str] = mapped_column(String(20), default="DRAFT")  # DRAFT | CONFIRMED | DONE | DISCARDED
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

    project = relationship("Project", lazy="joined")
    items = relationship("BusinessItem", back_populates="plan",
                         cascade="all, delete-orphan",
                         order_by="BusinessItem.seq_no")


class BusinessItem(Base):
    """One extracted business requirement, vetted against the live repo.

    vetting_status is what makes vetting resumable: a dropped SSE connection
    loses no work because every item's result is written here as soon as
    it's produced, and re-opening the stream just resumes at the first
    PENDING row instead of starting over.
    """
    __tablename__ = "business_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    plan_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("business_plans.id", ondelete="CASCADE"), index=True)
    seq_no: Mapped[int] = mapped_column(Integer)
    description: Mapped[str] = mapped_column(Text)
    # Where in the source document this came from, e.g. "Page 3" or
    # "Section: Refund Policy". Blank for an item the human adds by hand.
    location: Mapped[str] = mapped_column(String(200), default="")

    vetting_status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING | DONE | ERROR

    # Whether the requirement itself is clear enough to vet, or vague/
    # ambiguous enough that the client should be asked to clarify it first.
    is_requirement_clear: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    is_feasible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    already_supported: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # Response fields, in BRAC IT's own Change Request / Story template
    # vocabulary -- verified and gap-filled against the live repository
    # rather than invented from scratch.
    user_story: Mapped[str] = mapped_column(Text, default="")
    actors: Mapped[str] = mapped_column(Text, default="")
    pre_condition: Mapped[str] = mapped_column(Text, default="")
    impacted_areas: Mapped[str] = mapped_column(Text, default="")
    requirements: Mapped[str] = mapped_column(Text, default="")
    acceptance_criteria: Mapped[str] = mapped_column(Text, default="")
    exceptions: Mapped[str] = mapped_column(Text, default="")

    # Superseded by the template-shaped fields above. Kept (unpopulated by
    # new vettings) so historical rows and anything still reading them
    # directly are not broken.
    current_business: Mapped[str] = mapped_column(Text, default="")
    feasibility_notes: Mapped[str] = mapped_column(Text, default="")
    integration_approach: Mapped[str | None] = mapped_column(String(30), nullable=True)
    related_existing_feature: Mapped[str] = mapped_column(Text, default="")
    integration_notes: Mapped[str] = mapped_column(Text, default="")
    impacts_other_features: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    impact_notes: Mapped[str] = mapped_column(Text, default="")
    is_existing_business_change: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    change_feasible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    verdict: Mapped[str] = mapped_column(Text, default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    vetted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    plan = relationship("BusinessPlan", back_populates="items")


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


# --- app settings ------------------------------------------------------------

class AppSetting(Base):
    """Admin-managed key/value configuration (API keys, model names, etc.).

    Sensitive values are stored encrypted via app.crypto and never returned
    in plain text by the API -- only whether they are set is exposed.
    """
    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_now)

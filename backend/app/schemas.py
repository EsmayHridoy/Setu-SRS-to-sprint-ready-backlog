"""API shapes.

These classes are the contract. The React client is generated from the OpenAPI
spec FastAPI builds out of them, so changing one here changes the frontend types
too. Freeze them before parallel work starts.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- roles -------------------------------------------------------------------

class ProjectStub(ORM):
    id: str
    name: str


class RoleStub(ORM):
    id: str
    name: str
    is_admin: bool


class RoleOut(ORM):
    id: str
    name: str
    description: str
    is_admin: bool
    created_at: datetime
    projects: list[ProjectStub] = []
    user_count: int = 0


class RoleIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    description: str = ""
    is_admin: bool = False
    project_ids: list[str] = []


# --- projects ----------------------------------------------------------------

class ProjectOut(ORM):
    id: str
    name: str
    description: str
    provider: str
    repo_url: str
    default_branch: str
    status: str
    indexed_at: datetime | None = None
    created_at: datetime
    # Never the token itself, only enough to recognise it.
    token_last4: str | None = None
    token_set_at: datetime | None = None
    roles: list[RoleStub] = []
    artifact_count: int = 0


class ProjectIn(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    description: str = ""
    provider: str = Field(default="GITHUB", pattern="^(GITHUB|GITLAB|MANUAL)$")
    repo_url: str = ""
    default_branch: str = "main"
    status: str = Field(default="ACTIVE", pattern="^(ACTIVE|DISABLED)$")
    # Write-only. Omit to leave the stored token untouched; send "" to clear it.
    access_token: str | None = None


# --- users -------------------------------------------------------------------

class UserOut(ORM):
    id: str
    name: str
    email: str
    job_title: str
    is_active: bool
    created_at: datetime
    roles: list[RoleStub] = []


class UserIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    job_title: str = ""
    is_active: bool = True
    role_ids: list[str] = []
    password: str | None = None


class LoginIn(BaseModel):
    email: str = Field(min_length=1)
    password: str = Field(min_length=1)


class ChangePasswordIn(BaseModel):
    current_password: str = Field(min_length=1)
    new_password: str = Field(min_length=6)


class ResetPasswordIn(BaseModel):
    new_password: str = Field(min_length=6)


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"


class SessionOut(BaseModel):
    """Who the caller is, and what the interface should show them."""
    user: UserOut
    is_admin: bool
    projects: list[ProjectOut]


# --- chat --------------------------------------------------------------------

class CitationOut(ORM):
    id: str
    kind: str
    source_ref: str
    quoted_span: str
    verified: bool


class MessageOut(ORM):
    id: str
    role: str
    content: str
    is_placeholder: bool
    # The plan extracted from a document uploaded in chat, if this reply
    # presents one; the client fetches it from /api/business-plans/{id}.
    business_plan_id: str | None = None
    created_at: datetime
    citations: list[CitationOut] = []


class ConversationOut(ORM):
    id: str
    title: str
    project_id: str
    project_name: str = ""
    created_at: datetime
    updated_at: datetime


class ConversationDetail(ConversationOut):
    messages: list[MessageOut] = []


class NewConversation(BaseModel):
    project_id: str


class NewMessage(BaseModel):
    # Generous because an extracted PDF/DOCX excerpt may be folded into the
    # message alongside the question. See uploads.MAX_TEXT_CHARS.
    content: str = Field(min_length=1, max_length=60_000)


class ExtractResult(BaseModel):
    """Plain text pulled from an uploaded document, ready to fold into a message."""
    filename: str
    kind: str
    chars: int
    truncated: bool
    text: str


class SendMessageResult(BaseModel):
    conversation: ConversationOut
    user_message: MessageOut
    reply: MessageOut


# --- github agent --------------------------------------------------------------

class AgentPrompt(BaseModel):
    prompt: str = Field(min_length=1, max_length=4_000)


class AgentReply(BaseModel):
    response: str


# --- business plans -----------------------------------------------------------

class BusinessItemOut(ORM):
    id: str
    seq_no: int
    description: str
    location: str
    vetting_status: str

    is_requirement_clear: bool | None = None
    is_feasible: bool | None = None
    already_supported: bool | None = None

    # BRAC IT's own Change Request / Story template vocabulary.
    user_story: str
    actors: str
    pre_condition: str
    impacted_areas: str
    requirements: str
    acceptance_criteria: str
    exceptions: str

    # Superseded fields, kept for backward compatibility.
    current_business: str
    feasibility_notes: str
    integration_approach: str | None = None
    related_existing_feature: str
    integration_notes: str
    impacts_other_features: bool | None = None
    impact_notes: str
    is_existing_business_change: bool | None = None
    change_feasible: bool | None = None

    verdict: str
    error_message: str
    vetted_at: datetime | None = None


class BusinessPlanOut(ORM):
    id: str
    project_id: str
    project_name: str = ""
    source_filename: str
    status: str
    created_at: datetime
    updated_at: datetime


class BusinessPlanDetail(BusinessPlanOut):
    items: list[BusinessItemOut] = []


class BusinessItemEdit(BaseModel):
    description: str = Field(min_length=1, max_length=2_000)
    location: str = ""


class BusinessPlanItemsIn(BaseModel):
    items: list[BusinessItemEdit]


# --- audit -------------------------------------------------------------------

class AuditOut(ORM):
    id: int
    entity_type: str
    entity_id: str
    actor_name: str
    action: str
    detail: str
    occurred_at: datetime


# --- app settings ------------------------------------------------------------

class AppSettingOut(BaseModel):
    key: str
    label: str
    is_sensitive: bool
    is_set: bool
    value: str  # empty for sensitive fields; current value for non-sensitive

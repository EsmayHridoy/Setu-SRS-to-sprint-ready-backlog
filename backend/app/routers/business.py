"""Business plans: upload a document, review the extracted requirements,
confirm, then vet each one against the live GitHub repo one at a time.

A plan is visible to anyone whose roles grant them the project it belongs to
(like Project/Role themselves) -- unlike Conversation, it isn't locked to the
person who uploaded it, since reviewing and confirming a plan is treated as
project-level work rather than a private chat.

Vetting runs inline in the GET .../vet/stream request rather than as a
background job: each item's result is written to the DB the moment it's
produced, so a dropped connection loses no work -- reopening the stream just
replays already-finished items and resumes at the first PENDING one. Because
that streaming generator keeps writing to the DB for the life of the
connection, it opens its own SessionLocal() rather than reusing the
request-scoped `db` dependency, which FastAPI tears down as soon as the
route function returns -- before a StreamingResponse body has actually sent
anything (the same constraint app/routers/chat.py works around by
precomputing everything up front instead).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from urllib.parse import quote

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, UploadFile, status,
)
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.orm import Session

from .. import (
    business_agent, chat_memory, extraction, github_mcp, srs_agent, srs_docx,
)
from ..db import SessionLocal, get_db
from ..models import (
    BusinessItem, BusinessItemComment, BusinessPlan, Message, SrsDocument,
    User,
)
from ..schemas import (
    BusinessItemCommentIn, BusinessItemCommentOut, BusinessItemCommentReply,
    BusinessItemDescriptionIn, BusinessItemOut, BusinessPlanDetail,
    BusinessPlanItemsIn, BusinessPlanOut, SrsDocumentOut,
)
from ..security import authorised_project, current_user, projects_for_user
from ..sse import SSE_HEADERS, sse_event

log = logging.getLogger("setu")

router = APIRouter(prefix="/api/business-plans", tags=["business-plans"])

# Smaller than a chat turn's: it is sent once per item, not once per plan.
VETTING_HISTORY_CHARS = 12_000


def _accessible_plan(db: Session, plan_id: str, user: User) -> BusinessPlan:
    """Load a plan only if the caller's roles grant its project.

    404 rather than 403 on purpose, matching authorised_project: a user who
    cannot reach the project should not learn the plan exists.
    """
    plan = db.get(BusinessPlan, plan_id)
    allowed = {p.id for p in projects_for_user(db, user)}
    if plan is None or plan.project_id not in allowed:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business plan not found.")
    return plan


def _plan_out(plan: BusinessPlan) -> BusinessPlanOut:
    data = BusinessPlanOut.model_validate(plan)
    data.project_name = plan.project.name if plan.project else ""
    return data


def _plan_detail(plan: BusinessPlan) -> BusinessPlanDetail:
    data = BusinessPlanDetail.model_validate(plan)
    data.project_name = plan.project.name if plan.project else ""
    data.items = [BusinessItemOut.model_validate(i) for i in plan.items]
    return data


@router.post("", response_model=BusinessPlanDetail,
             status_code=status.HTTP_201_CREATED)
async def create_plan(project_id: str = Form(...), file: UploadFile = File(...),
                      user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    """Extract a draft list of businesses from an uploaded document."""
    project = authorised_project(project_id, db, user)
    data = await file.read()
    chunks = extraction.extract_with_locations(file.filename, file.content_type, data)
    businesses = await business_agent.extract_businesses(chunks, user_id=user.id)

    plan = BusinessPlan(project_id=project.id, user_id=user.id,
                        source_filename=file.filename or "document")
    db.add(plan)
    db.flush()
    for seq_no, business in enumerate(businesses, start=1):
        db.add(BusinessItem(plan_id=plan.id, seq_no=seq_no,
                            description=business.description,
                            location=business.location))
    db.commit()
    db.refresh(plan)
    return _plan_detail(plan)


@router.get("", response_model=list[BusinessPlanOut])
def list_plans(project_id: str | None = None,
              user: User = Depends(current_user), db: Session = Depends(get_db)):
    """List business plans for projects the caller can access."""
    allowed = {p.id for p in projects_for_user(db, user)}
    if not allowed:
        return []
    query = db.query(BusinessPlan).filter(BusinessPlan.project_id.in_(allowed))
    if project_id:
        query = query.filter(BusinessPlan.project_id == project_id)
    rows = query.order_by(BusinessPlan.created_at.desc()).all()
    return [_plan_out(p) for p in rows]


@router.get("/{plan_id}", response_model=BusinessPlanDetail)
def read_plan(plan_id: str, user: User = Depends(current_user),
             db: Session = Depends(get_db)):
    plan = _accessible_plan(db, plan_id, user)
    return _plan_detail(plan)


@router.patch("/{plan_id}/items", response_model=BusinessPlanDetail)
def edit_items(plan_id: str, payload: BusinessPlanItemsIn,
               user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Replace a draft plan's item list wholesale -- add, edit, delete or
    reorder by sending the full corrected list. Only allowed before confirm.
    """
    plan = _accessible_plan(db, plan_id, user)
    if plan.status != "DRAFT":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This plan has already been confirmed; its items can no longer "
            "be edited.",
        )

    for item in list(plan.items):
        db.delete(item)
    db.flush()
    for seq_no, edit in enumerate(payload.items, start=1):
        db.add(BusinessItem(plan_id=plan.id, seq_no=seq_no,
                            description=edit.description, location=edit.location))

    plan.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(plan)
    return _plan_detail(plan)


@router.patch("/{plan_id}/items/{item_id}", response_model=BusinessItemOut)
def edit_pending_item(plan_id: str, item_id: str,
                      payload: BusinessItemDescriptionIn,
                      user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    """Reword one business of a confirmed plan that is still waiting to be
    vetted -- typically while vetting is paused. Vetted and failed items keep
    their text, so every verdict matches the requirement it was given.
    """
    plan = _accessible_plan(db, plan_id, user)
    item = db.get(BusinessItem, item_id)
    if item is None or item.plan_id != plan.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found.")
    if plan.status != "CONFIRMED" or item.vetting_status != "PENDING":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Only a business that is still waiting to be vetted can be changed.",
        )
    description = payload.description.strip()
    if not description:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Describe the business requirement.")

    item.description = description
    plan.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    return BusinessItemOut.model_validate(item)


@router.post("/{plan_id}/confirm", response_model=BusinessPlanOut)
def confirm_plan(plan_id: str, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    plan = _accessible_plan(db, plan_id, user)
    if plan.status != "DRAFT":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "This plan has already been confirmed.")
    if not plan.items:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "Add at least one business before confirming.")
    try:
        github_mcp.require_configured()
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

    plan.status = "CONFIRMED"
    plan.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(plan)
    return _plan_out(plan)


@router.post("/{plan_id}/discard", response_model=BusinessPlanOut)
def discard_plan(plan_id: str, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    """The "no" answer to a draft: nothing gets vetted. The plan is kept, not
    deleted, so a chat it came from still remembers it was turned down."""
    plan = _accessible_plan(db, plan_id, user)
    if plan.status != "DRAFT":
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Only a plan that hasn't been confirmed can be "
                            "discarded.")
    plan.status = "DISCARDED"
    plan.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(plan)
    return _plan_out(plan)


@router.get("/{plan_id}/vet/stream")
def vet_stream(plan_id: str, user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    """Vet every business one at a time, streaming each verdict as it's
    produced, with `item_status` progress lines while each one is vetted.
    Already-vetted items (from an earlier, interrupted run) are replayed from
    the DB instead of being vetted again.
    """
    plan = _accessible_plan(db, plan_id, user)
    if plan.status not in ("CONFIRMED", "DONE"):
        raise HTTPException(status.HTTP_409_CONFLICT,
                            "Confirm the plan before vetting it.")

    user_id = user.id
    # A plan uploaded in chat is vetted with that chat as background --
    # clarifications the user gave, and the plan's other items. Read once,
    # up front, while the request's session is still open.
    origin = (db.query(Message)
              .filter(Message.business_plan_id == plan.id).first())
    history = (chat_memory.build_history(origin.conversation.messages,
                                         budget=VETTING_HISTORY_CHARS)
               if origin else "")

    async def event_stream():
        session = SessionLocal()
        try:
            current = session.get(BusinessPlan, plan_id)
            for item in current.items:
                # Pick up a waiting item's latest wording -- it can be edited
                # while vetting is paused (see edit_pending_item).
                session.refresh(item)
                if item.vetting_status in ("DONE", "ERROR"):
                    yield sse_event(
                        "item_result",
                        BusinessItemOut.model_validate(item).model_dump(mode="json"),
                    )
                    continue

                yield sse_event("item_start", {
                    "item_id": item.id,
                    "seq_no": item.seq_no,
                    "description": item.description,
                })
                try:
                    async for kind, value in business_agent.vet_business_stream(
                        item.description, user_id=user_id, history=history,
                    ):
                        if kind == business_agent.RESULT:
                            result = value
                        else:
                            # What the agent is doing on this item right now;
                            # shown on its row until the result replaces it.
                            yield sse_event("item_status", {
                                "item_id": item.id, "text": value,
                            })
                except Exception as exc:  # noqa: BLE001 - one bad item shouldn't stop the rest
                    item.vetting_status = "ERROR"
                    item.error_message = str(exc)
                else:
                    item.vetting_status = "DONE"
                    item.is_requirement_clear = result.is_requirement_clear
                    item.is_feasible = result.is_feasible
                    item.already_supported = result.already_supported
                    item.user_story = result.user_story
                    item.actors = result.actors
                    item.pre_condition = result.pre_condition
                    item.impacted_areas = result.impacted_areas
                    item.requirements = result.requirements
                    item.acceptance_criteria = result.acceptance_criteria
                    item.exceptions = result.exceptions
                    item.verdict = result.verdict
                item.vetted_at = datetime.utcnow()
                session.commit()
                yield sse_event(
                    "item_result",
                    BusinessItemOut.model_validate(item).model_dump(mode="json"),
                )

            current.status = "DONE"
            current.updated_at = datetime.utcnow()
            session.commit()
            yield sse_event("done", _plan_out(current).model_dump(mode="json"))
        finally:
            session.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers=SSE_HEADERS)


# --- item discussion and approval ---------------------------------------------
#
# A vetted item's DONE status means "has a verdict", not "final". The BA can
# discuss it here -- asking a clarifying question, or giving new information
# that should revise the verdict -- then explicitly approve it once
# satisfied. Only an approved item is backlog-ready (see _stories() below,
# which only includes approved items in a generated SRS).

# Kept far smaller than a chat turn's or a whole plan's: this is one item's
# own back-and-forth, not the project's history.
DISCUSSION_HISTORY_CHARS = 6_000


def _accessible_item(db: Session, plan_id: str, item_id: str,
                     user: User) -> tuple[BusinessPlan, BusinessItem]:
    plan = _accessible_plan(db, plan_id, user)
    item = db.get(BusinessItem, item_id)
    if item is None or item.plan_id != plan.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Business not found.")
    return plan, item


def _require_vetted(item: BusinessItem) -> None:
    if item.vetting_status != "DONE":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This business has not been vetted yet, so there is no verdict "
            "to discuss or approve.",
        )


def _comment_history(comments: list[BusinessItemComment], *,
                     budget: int = DISCUSSION_HISTORY_CHARS) -> str:
    """The item's discussion so far as a "User: / Assistant:" transcript,
    same shape as chat_memory.build_history() but for one item's own thread
    rather than a conversation's messages."""
    turns: list[str] = []
    used = 0
    for comment in comments:
        label = "User" if comment.role == "USER" else "Assistant"
        turn = f"{label}: {comment.content}"
        used += len(turn)
        if used > budget:
            break
        turns.append(turn)
    return "\n\n".join(turns)


def _vetting_dict(item: BusinessItem) -> dict:
    return {
        "is_requirement_clear": item.is_requirement_clear,
        "is_feasible": item.is_feasible,
        "already_supported": item.already_supported,
        "user_story": item.user_story,
        "actors": item.actors,
        "pre_condition": item.pre_condition,
        "impacted_areas": item.impacted_areas,
        "requirements": item.requirements,
        "acceptance_criteria": item.acceptance_criteria,
        "exceptions": item.exceptions,
        "verdict": item.verdict,
    }


@router.get("/{plan_id}/items/{item_id}/comments",
           response_model=list[BusinessItemCommentOut])
def list_item_comments(plan_id: str, item_id: str,
                       user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    _, item = _accessible_item(db, plan_id, item_id, user)
    return [BusinessItemCommentOut.model_validate(c) for c in item.comments]


@router.post("/{plan_id}/items/{item_id}/comments",
            response_model=BusinessItemCommentReply,
            status_code=status.HTTP_201_CREATED)
async def post_item_comment(plan_id: str, item_id: str,
                            payload: BusinessItemCommentIn,
                            user: User = Depends(current_user),
                            db: Session = Depends(get_db)):
    """Discuss an already-vetted item. The agent decides for itself whether
    this is a clarifying question (answered from the existing verdict, no
    repository round-trip) or new information that revises the verdict --
    see business_agent.discuss_item_stream. Either way this never sets
    is_approved: only the BA's own explicit approve call does that, so an
    approved item can't quietly change under it via a stray comment.

    Locked once approved: the BA's sign-off is meant to be the last word on
    an item, not a state a follow-up comment can silently move past.
    """
    plan, item = _accessible_item(db, plan_id, item_id, user)
    _require_vetted(item)
    if item.is_approved:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "This business is already approved. Its discussion is closed -- "
            "there is nothing to re-vet once the BA has signed off.",
        )

    content = payload.content.strip()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Write a comment first.")

    history = _comment_history(list(item.comments))
    current = _vetting_dict(item)

    result = None
    try:
        async for kind, value in business_agent.discuss_item_stream(
            item.description, current, comment=content, history=history,
            user_id=user.id,
        ):
            if kind == business_agent.RESULT:
                result = value
    except RuntimeError as exc:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surface a clean error, not a raw 500
        log.warning("discuss_item_stream failed: %s", exc)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The agent could not respond right now. Please try again.",
        ) from exc
    if result is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "The agent could not respond right now. Please try again.",
        )

    user_comment = BusinessItemComment(business_item_id=item.id, role="USER",
                                       content=content)
    db.add(user_comment)

    if result.changed:
        item.is_requirement_clear = result.is_requirement_clear
        item.is_feasible = result.is_feasible
        item.already_supported = result.already_supported
        item.user_story = result.user_story
        item.actors = result.actors
        item.pre_condition = result.pre_condition
        item.impacted_areas = result.impacted_areas
        item.requirements = result.requirements
        item.acceptance_criteria = result.acceptance_criteria
        item.exceptions = result.exceptions
        item.verdict = result.verdict
        item.vetted_at = datetime.utcnow()

    assistant_comment = BusinessItemComment(
        business_item_id=item.id, role="ASSISTANT", content=result.reply,
        changed_verdict=result.changed,
    )
    db.add(assistant_comment)

    plan.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(user_comment)
    db.refresh(assistant_comment)
    db.refresh(item)

    return BusinessItemCommentReply(
        comment=BusinessItemCommentOut.model_validate(user_comment),
        reply=BusinessItemCommentOut.model_validate(assistant_comment),
        item=BusinessItemOut.model_validate(item),
    )


@router.post("/{plan_id}/items/{item_id}/approve", response_model=BusinessItemOut)
def approve_item(plan_id: str, item_id: str, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    """The BA's explicit sign-off on an item's current verdict. Idempotent --
    approving an already-approved item just returns it unchanged, since a
    double click or a retried request should not be an error.
    """
    plan, item = _accessible_item(db, plan_id, item_id, user)
    _require_vetted(item)

    if not item.is_approved:
        item.is_approved = True
        plan.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(item)
    return BusinessItemOut.model_validate(item)


# --- SRS generation ----------------------------------------------------------
#
# The step after a plan is vetted: the user may hand over their own SRS format
# (a .docx) and get it back with every vetted story written into the section it
# belongs in. DOCX only -- unlike the extraction endpoints, which also accept
# PDF, there is no way to write structured content back into a PDF.
#
# Both the uploaded format and the generated file live in the srs_documents
# row rather than on disk: the container's filesystem does not survive a
# redeploy, and the download may well come minutes after the upload.

_DOCX_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)

# A title long enough to identify the story in the document's own outline.
_TITLE_CHARS = 110

# Content-Disposition is sent as a latin-1 header, so the plain `filename=`
# form carries an ASCII-only version and `filename*=` the real one.
_NON_ASCII = re.compile(r"[^\x20-\x7e]")
_QUOTE_UNSAFE = re.compile(r'["\\]')


def _story_title(item: BusinessItem) -> str:
    """A short name for the story, for the heading it gets in the document.

    The user story's first sentence where there is one -- it reads as a title
    already ("As a teller, I want to reverse a posted transaction") -- falling
    back to the extracted description for an item whose vetting left it blank.
    """
    source = (item.user_story or item.description or "").strip()
    first = re.split(r"(?<=[.!?])\s|\n", source, maxsplit=1)[0].strip()
    title = " ".join((first or source).split())
    if len(title) > _TITLE_CHARS:
        title = title[:_TITLE_CHARS].rsplit(" ", 1)[0] + "…"
    return title or f"Business {item.seq_no}"


def _stories(plan: BusinessPlan) -> list[srs_docx.Story]:
    """The plan's approved items, in plan order.

    Items that failed vetting are left out: an ERROR row has no story fields
    to write, and putting its raw description in the SRS would pass off
    un-vetted text as specified. A DONE-but-not-yet-approved item is left
    out too -- it has a verdict, but the BA has not signed off on it, and an
    SRS is meant to carry only backlog-ready stories.
    """
    stories = []
    for item in plan.items:
        if item.vetting_status != "DONE" or not item.is_approved:
            continue
        fields = tuple(
            (key, getattr(item, key) or "")
            for key, _ in srs_docx.STORY_FIELDS
            if (getattr(item, key) or "").strip()
        )
        if not fields:
            continue
        stories.append(srs_docx.Story(seq_no=item.seq_no,
                                      title=_story_title(item), fields=fields))
    return stories


def _vetted_plan(db: Session, plan_id: str, user: User) -> BusinessPlan:
    plan = _accessible_plan(db, plan_id, user)
    if plan.status != "DONE":
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "Finish vetting this plan before building an SRS from it.",
        )
    return plan


async def _generate(db: Session, plan: BusinessPlan, srs: SrsDocument,
                    user_id: str) -> SrsDocument:
    """Match every vetted story to a section and write the filled-in .docx.

    A failed run is recorded on the row (status ERROR) before the error is
    raised, so the uploaded format is kept and the user can retry without
    uploading it again.
    """
    stories = _stories(plan)
    if not stories:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "None of this plan's businesses are approved yet, so there is "
            "nothing to write into an SRS. Vetting alone is not enough -- "
            "approve each one you want included first.",
        )

    try:
        # Which writer to use depends on what the upload turned out to be: a
        # blank story template gets its placeholder blocks filled in, and only
        # a finished specification needs the model to pick sections.
        shape = srs_docx.read_shape(srs.template_bytes)
        if shape.mode == srs_docx.TEMPLATE:
            output, placements, notice = srs_docx.fill_template(
                srs.template_bytes, stories,
            )
        else:
            assignments = await srs_agent.assign_sections(
                list(shape.sections), stories, user_id=user_id,
            )
            output, placements, notice = srs_docx.build_sections(
                srs.template_bytes, stories, assignments,
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - any failure is reported on the row
        srs.status = "ERROR"
        srs.error_message = str(exc)
        srs.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(srs)
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"The SRS could not be generated: {exc}",
        ) from exc

    srs.output_bytes = output
    srs.status = "READY"
    srs.notice = notice
    srs.error_message = ""
    srs.placements = json.dumps(
        [{"seq_no": story.seq_no, "section": placements.get(story.seq_no, "")}
         for story in stories]
    )
    srs.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(srs)
    return srs


@router.post("/{plan_id}/srs", response_model=SrsDocumentOut,
             status_code=status.HTTP_201_CREATED)
async def upload_srs_format(plan_id: str, file: UploadFile = File(...),
                            user: User = Depends(current_user),
                            db: Session = Depends(get_db)):
    """Take the user's SRS format and return it filled in.

    Uploading again replaces the plan's SRS rather than keeping both: there is
    one SRS per plan, and a second format means the first was the wrong one.
    """
    plan = _vetted_plan(db, plan_id, user)

    name = (file.filename or "").lower()
    if not name.endswith(".docx") or file.content_type not in (
        _DOCX_CONTENT_TYPE, "application/octet-stream", None, "",
    ):
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            "The SRS format must be a .docx file. A .doc or PDF cannot be "
            "written back into.",
        )
    data = await file.read()
    # Parses the file before storing it, so an unreadable upload fails here
    # with a clear message instead of at generation time.
    srs_docx.read_shape(data)

    srs = plan.srs
    if srs is None:
        srs = SrsDocument(plan_id=plan.id, user_id=user.id)
        db.add(srs)
    srs.user_id = user.id
    srs.template_filename = file.filename or "srs-format.docx"
    srs.template_bytes = data
    srs.output_bytes = None
    srs.status = "UPLOADED"
    srs.placements = "[]"
    srs.notice = ""
    srs.error_message = ""
    srs.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(srs)

    return SrsDocumentOut.model_validate(
        await _generate(db, plan, srs, user.id))


@router.post("/{plan_id}/srs/regenerate", response_model=SrsDocumentOut)
async def regenerate_srs(plan_id: str, user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    """Rebuild the SRS from the format already uploaded -- after a failed run,
    or after more of the plan was vetted."""
    plan = _vetted_plan(db, plan_id, user)
    if plan.srs is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            "No SRS format has been uploaded for this plan.")
    return SrsDocumentOut.model_validate(
        await _generate(db, plan, plan.srs, user.id))


@router.get("/{plan_id}/srs/download")
def download_srs(plan_id: str, user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    """The finished SRS as a .docx download."""
    plan = _accessible_plan(db, plan_id, user)
    srs = plan.srs
    if srs is None or srs.status != "READY" or not srs.output_bytes:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "This plan has no generated SRS to download yet.",
        )

    stem = (srs.template_filename or "srs.docx").rsplit(".", 1)[0]
    filename = f"{stem} - vetted.docx"
    ascii_name = _QUOTE_UNSAFE.sub("", _NON_ASCII.sub("_", filename))
    return Response(
        content=bytes(srs.output_bytes),
        media_type=_DOCX_CONTENT_TYPE,
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_name}"; '
                f"filename*=UTF-8''{quote(filename)}"
            ),
            # The browser reads the name off the header, which a cross-origin
            # fetch cannot see unless it is exposed.
            "Access-Control-Expose-Headers": "Content-Disposition",
        },
    )

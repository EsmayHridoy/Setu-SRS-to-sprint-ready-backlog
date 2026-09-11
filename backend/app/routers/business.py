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

from datetime import datetime

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, UploadFile, status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import business_agent, chat_memory, extraction, github_mcp
from ..db import SessionLocal, get_db
from ..models import BusinessItem, BusinessPlan, Message, User
from ..schemas import (
    BusinessItemOut, BusinessPlanDetail, BusinessPlanItemsIn, BusinessPlanOut,
)
from ..security import authorised_project, current_user, projects_for_user
from ..sse import SSE_HEADERS, sse_event

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

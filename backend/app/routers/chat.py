"""Conversations against a selected project.

Every endpoint resolves the project through `authorised_project`, so a user can
only ever read or write conversations for projects their roles grant. Ownership
is checked as well: conversations are private to the person who started them.
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import placeholder_ai
from ..config import get_settings
from ..db import get_db
from ..models import Citation, Conversation, Message, User
from ..schemas import (
    ConversationDetail, ConversationOut, MessageOut, NewConversation,
    NewMessage, SendMessageResult,
)
from ..security import authorised_project, current_user, projects_for_user

router = APIRouter(prefix="/api/conversations", tags=["chat"])


def _out(conversation: Conversation) -> ConversationOut:
    data = ConversationOut.model_validate(conversation)
    data.project_name = conversation.project.name if conversation.project else ""
    return data


def _owned(db: Session, conversation_id: str, user: User) -> Conversation:
    conversation = db.get(Conversation, conversation_id)
    if conversation is None or conversation.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    # The grant may have been withdrawn since the conversation was started.
    allowed = {p.id for p in projects_for_user(db, user)}
    if conversation.project_id not in allowed:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "You no longer have access to the project this conversation "
            "belongs to.",
        )
    return conversation


@router.get("", response_model=list[ConversationOut])
def list_conversations(project_id: str | None = None,
                       user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    """List the caller's own conversations, optionally filtered to one project,
    limited to projects their roles still grant."""
    allowed = {p.id for p in projects_for_user(db, user)}
    if not allowed:
        return []
    query = (db.query(Conversation)
             .filter(Conversation.user_id == user.id,
                     Conversation.project_id.in_(allowed)))
    if project_id:
        query = query.filter(Conversation.project_id == project_id)
    rows = query.order_by(Conversation.updated_at.desc()).all()
    return [_out(c) for c in rows]


@router.post("", response_model=ConversationOut,
             status_code=status.HTTP_201_CREATED)
def start_conversation(payload: NewConversation,
                       user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    """Start a new, empty conversation against a project the caller may access."""
    project = authorised_project(payload.project_id, db, user)
    conversation = Conversation(user_id=user.id, project_id=project.id)
    db.add(conversation)
    db.commit()
    return _out(conversation)


@router.get("/{conversation_id}", response_model=ConversationDetail)
def read_conversation(conversation_id: str,
                      user: User = Depends(current_user),
                      db: Session = Depends(get_db)):
    """Return one of the caller's own conversations with its full message history."""
    conversation = _owned(db, conversation_id, user)
    data = ConversationDetail.model_validate(conversation)
    data.project_name = conversation.project.name
    data.messages = [MessageOut.model_validate(m) for m in conversation.messages]
    return data


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_conversation(conversation_id: str,
                        user: User = Depends(current_user),
                        db: Session = Depends(get_db)):
    """Delete one of the caller's own conversations and its messages."""
    conversation = _owned(db, conversation_id, user)
    db.delete(conversation)
    db.commit()


def _persist_exchange(db: Session, conversation: Conversation,
                      question: str) -> tuple[Message, Message]:
    """Persist the user's question and the assistant's drafted reply (with
    citations), returning both messages. Shared by the plain and streaming
    endpoints so they cannot drift apart.

    Replace the marked block with the real pipeline: expand the query, run the
    hybrid search scoped to conversation.project_id, call the model, then
    discard any citation whose quoted text is not present in its artifact.
    """
    user_message = Message(conversation_id=conversation.id, role="USER",
                           content=question)
    db.add(user_message)

    if conversation.title == "New conversation":
        conversation.title = placeholder_ai.title_for(question)

    # --- the seam ------------------------------------------------------------
    drafted = placeholder_ai.answer(db, conversation.project, question)
    is_placeholder = get_settings().use_placeholder_ai
    # -------------------------------------------------------------------------

    reply = Message(conversation_id=conversation.id, role="ASSISTANT",
                    content=drafted.content, is_placeholder=is_placeholder)
    db.add(reply)
    db.flush()

    for draft in drafted.citations:
        db.add(Citation(message_id=reply.id, artifact_id=draft.artifact_id,
                        kind=draft.kind, source_ref=draft.source_ref,
                        quoted_span=draft.quoted_span,
                        # Real verification runs once retrieval is in place.
                        verified=False))

    conversation.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(reply)
    return user_message, reply


@router.post("/{conversation_id}/messages", response_model=SendMessageResult)
def send_message(conversation_id: str, payload: NewMessage,
                 user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    """Post a user question, generate an assistant reply with citations, and
    return both messages. Currently backed by the placeholder AI pipeline."""
    conversation = _owned(db, conversation_id, user)
    question = payload.content.strip()
    if not question:
        raise HTTPException(400, "Type a question first.")

    user_message, reply = _persist_exchange(db, conversation, question)

    return SendMessageResult(
        conversation=_out(conversation),
        user_message=MessageOut.model_validate(user_message),
        reply=MessageOut.model_validate(reply),
    )


def _sse(event: str, data: dict) -> str:
    """Format one Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


# Stream the reply in chunks that end on whitespace, so words are never split
# across events. When the real model is wired in, replace the pre-computed
# content below with the model's own token stream.
_CHUNK = re.compile(r"\S+\s*")


@router.post("/{conversation_id}/messages/stream")
def stream_message(conversation_id: str, payload: NewMessage,
                   user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    """Same as `send_message`, but the assistant reply is delivered over
    Server-Sent Events: a `start` frame with the persisted user message and an
    empty reply shell, a run of `delta` frames carrying the reply text, and a
    final `done` frame with the complete reply (ids, citations, placeholder
    flag). The whole exchange is persisted up front, so the stream only ever
    replays already-committed content and never touches the request's DB
    session from inside the generator.
    """
    conversation = _owned(db, conversation_id, user)
    question = payload.content.strip()
    if not question:
        raise HTTPException(400, "Type a question first.")

    user_message, reply = _persist_exchange(db, conversation, question)

    conversation_out = _out(conversation).model_dump(mode="json")
    user_message_out = MessageOut.model_validate(user_message).model_dump(mode="json")
    reply_out = MessageOut.model_validate(reply).model_dump(mode="json")
    content = reply_out["content"]

    async def event_stream():
        yield _sse("start", {
            "conversation": conversation_out,
            "user_message": user_message_out,
            "reply_id": reply_out["id"],
        })
        for chunk in _CHUNK.findall(content):
            yield _sse("delta", {"text": chunk})
            await asyncio.sleep(0.02)  # pacing; the real stream sets its own
        yield _sse("done", {
            "conversation": conversation_out,
            "reply": reply_out,
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # defeat proxy buffering (e.g. nginx)
        },
    )

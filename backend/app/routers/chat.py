"""Conversations against a selected project.

Every endpoint resolves the project through `authorised_project`, so a user can
only ever read or write conversations for projects their roles grant. Ownership
is checked as well: conversations are private to the person who started them.
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
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

    user_message = Message(conversation_id=conversation.id, role="USER",
                           content=question)
    db.add(user_message)

    if conversation.title == "New conversation":
        conversation.title = placeholder_ai.title_for(question)

    # --- the seam ------------------------------------------------------------
    # Replace this block with the real pipeline: expand the query, run the
    # hybrid search scoped to conversation.project_id, call the model, then
    # discard any citation whose quoted text is not present in its artifact.
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

    return SendMessageResult(
        conversation=_out(conversation),
        user_message=MessageOut.model_validate(user_message),
        reply=MessageOut.model_validate(reply),
    )

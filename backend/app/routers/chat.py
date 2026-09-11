"""Conversations against a selected project.

Every endpoint resolves the project through `authorised_project`, so a user can
only ever read or write conversations for projects their roles grant. Ownership
is checked as well: conversations are private to the person who started them.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, UploadFile, status,
)
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import chat_agent, extraction, github_mcp, placeholder_ai
from ..config import get_settings
from ..db import SessionLocal, get_db
from ..models import Citation, Conversation, Message, User
from ..schemas import (
    ConversationDetail, ConversationOut, MessageOut, NewConversation,
    NewMessage, SendMessageResult,
)
from ..security import authorised_project, current_user, projects_for_user
from ..sse import SSE_HEADERS, sse_event

log = logging.getLogger("setu")

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
async def delete_conversation(conversation_id: str,
                              user: User = Depends(current_user),
                              db: Session = Depends(get_db)):
    """Delete one of the caller's own conversations, its messages, and the
    agent-side history that goes with it.

    The conversation id is also the ADK session id (see chat_agent), so
    dropping the rows here without dropping that session would leave the
    transcript the model sees behind after the conversation is gone.
    """
    conversation = _owned(db, conversation_id, user)
    db.delete(conversation)
    db.commit()
    if not get_settings().use_placeholder_ai:
        await chat_agent.forget(conversation_id, user_id=user.id)


def _persist_user_message(db: Session, conversation: Conversation,
                          question: str) -> Message:
    """Persist the user's turn and title the conversation from it if it's
    still untitled. Shared by every path that starts an exchange, whether
    the reply comes from the placeholder or the real agent.
    """
    user_message = Message(conversation_id=conversation.id, role="USER",
                           content=question)
    db.add(user_message)

    if conversation.title == "New conversation":
        conversation.title = placeholder_ai.title_for(question)

    db.commit()
    db.refresh(user_message)
    return user_message


def _persist_reply(db: Session, conversation: Conversation, content: str, *,
                   is_placeholder: bool, citations=()) -> Message:
    """Persist the assistant's reply (with any citations) and bump the
    conversation's updated_at. Shared by the placeholder and real-agent paths.
    """
    reply = Message(conversation_id=conversation.id, role="ASSISTANT",
                    content=content, is_placeholder=is_placeholder)
    db.add(reply)
    db.flush()

    for draft in citations:
        db.add(Citation(message_id=reply.id, artifact_id=draft.artifact_id,
                        kind=draft.kind, source_ref=draft.source_ref,
                        quoted_span=draft.quoted_span,
                        # Real verification runs once retrieval is in place.
                        verified=False))

    conversation.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(reply)
    return reply


def _persist_exchange(db: Session, conversation: Conversation,
                      question: str) -> tuple[Message, Message]:
    """Placeholder path: answer from indexed project artifacts, with
    citations. Used whenever USE_PLACEHOLDER_AI is true; see chat_agent.py
    for the real, GitHub-MCP-backed path used when it's false.
    """
    user_message = _persist_user_message(db, conversation, question)
    drafted = placeholder_ai.answer(db, conversation.project, question)
    reply = _persist_reply(db, conversation, drafted.content,
                           is_placeholder=True, citations=drafted.citations)
    return user_message, reply


@router.post("/{conversation_id}/messages", response_model=SendMessageResult)
async def send_message(conversation_id: str, payload: NewMessage,
                       user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    """Post a user question, generate an assistant reply, and return both
    messages. Backed by the placeholder AI pipeline unless USE_PLACEHOLDER_AI
    is false, in which case the reply comes from chat_agent's GitHub-MCP
    agent instead (no citations in that case -- see chat_agent.py)."""
    conversation = _owned(db, conversation_id, user)
    question = payload.content.strip()
    if not question:
        raise HTTPException(400, "Type a question first.")

    if get_settings().use_placeholder_ai:
        user_message, reply = _persist_exchange(db, conversation, question)
    else:
        user_message = _persist_user_message(db, conversation, question)
        try:
            content = await chat_agent.answer(
                conversation.project.name, question, user_id=user.id,
                conversation_id=conversation.id,
            )
        except RuntimeError as exc:
            raise HTTPException(503, str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 - surface a clean error, not a raw 500
            log.warning("chat_agent.answer failed: %s", exc)
            raise HTTPException(
                503, "The agent could not answer right now. Please try again.",
            ) from exc
        reply = _persist_reply(db, conversation, content, is_placeholder=False)

    return SendMessageResult(
        conversation=_out(conversation),
        user_message=MessageOut.model_validate(user_message),
        reply=MessageOut.model_validate(reply),
    )


# Stream the reply in chunks that end on whitespace, so words are never split
# across events. When the real model is wired in, replace the pre-computed
# content below with the model's own token stream.
_CHUNK = re.compile(r"\S+\s*")


def _reply_stream(conversation: Conversation, user_message: Message,
                  reply: Message) -> StreamingResponse:
    """Deliver an already-persisted exchange over Server-Sent Events: a `start`
    frame with the user message and an empty reply shell, a run of `delta`
    frames carrying the reply text, then a `done` frame with the complete reply.

    Everything is serialized up front, so the async generator only replays
    in-memory strings and never touches the request's DB session — which the
    dependency will have closed by the time the body streams.
    """
    conversation_out = _out(conversation).model_dump(mode="json")
    user_message_out = MessageOut.model_validate(user_message).model_dump(mode="json")
    reply_out = MessageOut.model_validate(reply).model_dump(mode="json")
    content = reply_out["content"]

    async def event_stream():
        yield sse_event("start", {
            "conversation": conversation_out,
            "user_message": user_message_out,
            "reply_id": reply_out["id"],
        })
        for chunk in _CHUNK.findall(content):
            yield sse_event("delta", {"text": chunk})
            await asyncio.sleep(0.02)  # pacing; the real stream sets its own
        yield sse_event("done", {
            "conversation": conversation_out,
            "reply": reply_out,
        })

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers=SSE_HEADERS)


def _agent_reply_stream(conversation: Conversation, user_message: Message,
                        project_name: str, question: str,
                        user_id: str) -> StreamingResponse:
    """Same start/delta/done contract as _reply_stream, but the delta text is
    the real agent's own output as it's generated, and the reply's content
    isn't known until the stream finishes -- so, like
    app/routers/business.py's vet_stream, this opens its own SessionLocal()
    rather than the request-scoped `db` dependency, which FastAPI closes
    before a StreamingResponse body has actually sent anything.
    """
    conversation_out = _out(conversation).model_dump(mode="json")
    user_message_out = MessageOut.model_validate(user_message).model_dump(mode="json")
    conversation_id = conversation.id

    async def event_stream():
        session = SessionLocal()
        try:
            conv = session.get(Conversation, conversation_id)
            reply = Message(conversation_id=conv.id, role="ASSISTANT",
                            content="", is_placeholder=False)
            session.add(reply)
            session.commit()
            session.refresh(reply)

            yield sse_event("start", {
                "conversation": conversation_out,
                "user_message": user_message_out,
                "reply_id": reply.id,
            })

            final_text = ""
            try:
                async for is_final, text in chat_agent.answer_stream(
                    project_name, question, user_id=user_id,
                    conversation_id=conversation_id,
                ):
                    if is_final:
                        final_text = text
                    else:
                        yield sse_event("delta", {"text": text})
            except Exception as exc:  # noqa: BLE001 - surface, don't hang the stream
                final_text = f"Something went wrong answering this question: {exc}"

            reply.content = final_text or "The agent returned no response."
            conv.updated_at = datetime.utcnow()
            session.commit()
            session.refresh(reply)

            yield sse_event("done", {
                "conversation": _out(conv).model_dump(mode="json"),
                "reply": MessageOut.model_validate(reply).model_dump(mode="json"),
            })
        finally:
            session.close()

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers=SSE_HEADERS)


@router.post("/{conversation_id}/messages/stream")
def stream_message(conversation_id: str, payload: NewMessage,
                   user: User = Depends(current_user),
                   db: Session = Depends(get_db)):
    """Same as `send_message`, but the assistant reply is delivered over SSE.

    With USE_PLACEHOLDER_AI true, this is unchanged: compute the full
    placeholder answer, then fake-stream it word by word via _reply_stream --
    the same helper `.../messages/upload` uses, untouched by this branch.
    Otherwise, the reply streams for real from chat_agent as it's generated.
    """
    conversation = _owned(db, conversation_id, user)
    question = payload.content.strip()
    if not question:
        raise HTTPException(400, "Type a question first.")

    if get_settings().use_placeholder_ai:
        user_message, reply = _persist_exchange(db, conversation, question)
        return _reply_stream(conversation, user_message, reply)

    try:
        github_mcp.require_configured()
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc

    user_message = _persist_user_message(db, conversation, question)
    return _agent_reply_stream(conversation, user_message,
                               conversation.project.name, question, user.id)


@router.post("/{conversation_id}/messages/upload")
async def upload_message(conversation_id: str,
                         file: UploadFile = File(...),
                         prompt: str = Form(""),
                         user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    """Upload a PDF/DOCX and stream its extracted text back as the assistant
    reply. The user turn records that a document was uploaded (plus any typed
    prompt); the assistant turn is the extracted context itself, delivered over
    the same SSE frames as `stream_message`.
    """
    conversation = _owned(db, conversation_id, user)

    data = await file.read()
    kind, text, truncated = extraction.extract(file.filename, file.content_type, data)
    filename = file.filename or f"document.{kind}"

    note = f"[Uploaded document: {filename}]"
    if prompt.strip():
        note += f"\n\n{prompt.strip()}"
    user_message = Message(conversation_id=conversation.id, role="USER",
                           content=note)
    db.add(user_message)

    if conversation.title == "New conversation":
        conversation.title = placeholder_ai.title_for(prompt.strip() or filename)

    header = (f"Extracted {len(text):,} characters from {filename}"
              f"{' (truncated)' if truncated else ''}:")
    reply = Message(conversation_id=conversation.id, role="ASSISTANT",
                    content=f"{header}\n\n{text}", is_placeholder=False)
    db.add(reply)
    db.flush()

    conversation.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(reply)

    return _reply_stream(conversation, user_message, reply)

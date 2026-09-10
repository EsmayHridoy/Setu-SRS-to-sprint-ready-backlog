"""GitHub agent -- ask questions about the one repository GITHUB_PAT is
scoped to, answered by a Gemini ADK agent calling GitHub's hosted MCP server.

Kept separate from app/routers/chat.py: chat answers from indexed project
artifacts (or the placeholder standing in for that), this hits a live
repository through tools instead. Nothing here is project-scoped -- the PAT's
own fine-grained scope is what limits which repository can be reached.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from .. import github_agent
from ..models import User
from ..schemas import AgentPrompt, AgentReply
from ..security import current_user

router = APIRouter(prefix="/api/agent", tags=["agent"])


@router.post("/github", response_model=AgentReply)
async def ask_github(payload: AgentPrompt, user: User = Depends(current_user)):
    """Send one prompt to the GitHub agent and return its reply."""
    try:
        text = await github_agent.ask(payload.prompt, user_id=user.id)
    except RuntimeError as exc:
        raise HTTPException(503, str(exc)) from exc
    return AgentReply(response=text)

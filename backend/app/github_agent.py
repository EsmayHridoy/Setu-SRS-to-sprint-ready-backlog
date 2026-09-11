"""GitHub agent -- a Gemini-backed ADK agent wired to GitHub's hosted MCP server.

The agent reaches GitHub only through MCP tool calls, and the MCP server is
GitHub's own hosted endpoint (api.githubcopilot.com/mcp), authenticated with
GITHUB_PAT. Point that PAT at a fine-grained personal access token scoped to
exactly one repository and the agent can only ever act on that repository --
the token's scope is the boundary, not anything enforced in this file.

Kept separate from app/placeholder_ai.py and the /api/conversations pipeline
on purpose: that pipeline answers from indexed project artifacts, this one
answers by calling out to a live repository through tools.
"""
from __future__ import annotations

from google.adk.agents import LlmAgent

from sqlalchemy.orm import Session

from . import github_mcp
from .adk_runner import build_model, run_single_turn
from .db_settings import get_runtime_config

APP_NAME = "setu-github-agent"

_INSTRUCTION_TEMPLATE = (
    "You are Setu's GitHub assistant. {repo_hint} {tool_list_hint} You can "
    "read issues, pull requests, files and commits through the tools "
    "available to you. Never claim to have made a change unless a tool "
    "call actually reports success. Keep answers short and name the "
    "specific issue, PR or file you looked at."
)


async def ask(prompt: str, *, user_id: str, db: Session) -> str:
    """Run one turn against the GitHub agent and return its final text reply.

    A fresh agent, MCP connection and in-memory session are built per call.
    This endpoint is low volume, and it keeps a dropped MCP connection from
    being silently reused across unrelated requests.
    """
    cfg = get_runtime_config(db)
    github_mcp.require_configured(cfg)

    toolset = github_mcp.build_toolset(cfg)
    agent = LlmAgent(
        model=build_model(cfg),
        name="github_agent",
        instruction=_INSTRUCTION_TEMPLATE.format(
            repo_hint=github_mcp.repo_hint(),
            tool_list_hint=github_mcp.tool_list_hint(),
        ),
        tools=[toolset],
    )
    try:
        final_text = await run_single_turn(
            agent, prompt, app_name=APP_NAME, user_id=user_id,
        )
    finally:
        await toolset.close()

    return final_text or "The agent returned no response."

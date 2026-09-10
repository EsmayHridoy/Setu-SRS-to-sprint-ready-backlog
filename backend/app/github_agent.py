"""GitHub agent -- a Gemini ADK agent wired to GitHub's hosted MCP server.

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

import os

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools.mcp_tool.mcp_toolset import MCPToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)
from google.genai import types

from .config import get_settings

APP_NAME = "setu-github-agent"

_INSTRUCTION = (
    "You are Setu's GitHub assistant. You can read issues, pull requests, "
    "files and commits on the one repository the caller's token is scoped "
    "to, through the tools available to you. Never claim to have made a "
    "change unless a tool call actually reports success. Keep answers short "
    "and name the specific issue, PR or file you looked at."
)


def _require(value: str, env_var: str) -> str:
    if not value:
        raise RuntimeError(
            f"{env_var} is not set. Add it to backend/.env before using the "
            "GitHub agent -- see .env.example."
        )
    return value


def _build_agent() -> tuple[LlmAgent, MCPToolset]:
    settings = get_settings()
    token = _require(settings.github_pat, "GITHUB_PAT")
    api_key = _require(settings.gemini_api_key, "GOOGLE_API_KEY")

    # google-genai reads its key from this env var rather than taking one as
    # an argument; config.py's load_dotenv() has already set it if it came
    # from backend/.env, this just covers a key set only in Settings.
    os.environ.setdefault("GOOGLE_API_KEY", api_key)

    toolset = MCPToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=settings.github_mcp_url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-MCP-Toolsets": "all",
                "X-MCP-Readonly": "true" if settings.github_mcp_readonly else "false",
            },
        ),
    )
    agent = LlmAgent(
        model=settings.gemini_model,
        name="github_agent",
        instruction=_INSTRUCTION,
        tools=[toolset],
    )
    return agent, toolset


async def ask(prompt: str, *, user_id: str) -> str:
    """Run one turn against the GitHub agent and return its final text reply.

    A fresh agent, MCP connection and in-memory session are built per call.
    This endpoint is low volume, and it keeps a dropped MCP connection from
    being silently reused across unrelated requests.
    """
    agent, toolset = _build_agent()
    session_service = InMemorySessionService()
    session = await session_service.create_session(
        app_name=APP_NAME, user_id=user_id,
    )
    runner = Runner(app_name=APP_NAME, agent=agent, session_service=session_service)

    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    final_text = ""
    try:
        async for event in runner.run_async(
            user_id=user_id, session_id=session.id, new_message=content,
        ):
            if event.is_final_response() and event.content and event.content.parts:
                final_text = "".join(part.text or "" for part in event.content.parts)
    finally:
        await toolset.close()

    return final_text or "The agent returned no response."

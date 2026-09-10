"""GitHub agent, exposed for the ADK dev UI (`adk web`).

Same GitHub agent as app/github_agent.py, redefined here as a module-level
`root_agent` because that is the shape `adk web` looks for when it scans an
agents directory. Kept as a separate definition rather than imported from
app/github_agent.py: that module is driven by app/config.py, which requires a
reachable DATABASE_URL to construct Settings -- a requirement this standalone
dev-UI entry point has no reason to inherit. It loads backend/.env directly
instead, the same file the main app reads its GitHub/Gemini settings from.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_ENV_FILE)

_GITHUB_PAT = os.getenv("GITHUB_PAT", "")
if not _GITHUB_PAT:
    raise RuntimeError(f"GITHUB_PAT is not set in {_ENV_FILE}.")
if not os.getenv("GOOGLE_API_KEY"):
    raise RuntimeError(f"GOOGLE_API_KEY is not set in {_ENV_FILE}.")
_MCP_URL = os.getenv("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/")
_READONLY = os.getenv("GITHUB_MCP_READONLY", "true").lower() == "true"
_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash")

_INSTRUCTION = (
    "You are Setu's GitHub assistant. You can read issues, pull requests, "
    "files and commits on the one repository the caller's token is scoped "
    "to, through the tools available to you. Never claim to have made a "
    "change unless a tool call actually reports success. Keep answers short "
    "and name the specific issue, PR or file you looked at."
)

root_agent = LlmAgent(
    model=_MODEL,
    name="github_agent",
    instruction=_INSTRUCTION,
    tools=[
        McpToolset(
            connection_params=StreamableHTTPConnectionParams(
                url=_MCP_URL,
                headers={
                    "Authorization": f"Bearer {_GITHUB_PAT}",
                    "X-MCP-Toolsets": "all",
                    "X-MCP-Readonly": "true" if _READONLY else "false",
                },
            ),
        )
    ],
)

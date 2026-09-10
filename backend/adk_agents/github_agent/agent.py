from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_ENV_FILE)

_GITHUB_PAT = os.getenv("GITHUB_PAT", "")
if not _GITHUB_PAT:
    raise RuntimeError(f"GITHUB_PAT is not set in {_ENV_FILE}.")
_GITHUB_REPO = os.getenv("GITHUB_REPO", "chatbot-api")
_MCP_URL = os.getenv("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/")
_BRANCH = os.getenv("GITHUB_BRANCH", "claude/context-file-gitlab-mcp-257fb0")
_READONLY = os.getenv("GITHUB_MCP_READONLY", "true").lower() == "true"
_OWNER = os.getenv("GITHUB_OWNER", "Mohiuddin2000")
_MODEL = LiteLlm(
    model=f"ollama_chat/{os.getenv('OLLAMA_MODEL', '')}",
    api_base=os.getenv("OLLAMA_API_BASE", "http://localhost:11434"),
    reasoning_effort="high",
)

_DEFAULT_TOOLSET_TOOLS = [
    "get_file_contents", "get_label", "get_me", "list_branches", "search_code",
    "search_repositories", "search_users"
]

_INSTRUCTION = (
    f"You are Setu's GitHub assistant. Github owner : {_OWNER}. You can read contents, codes, branches, repositories, users from the {_GITHUB_REPO} repository of branch : {_BRANCH} the caller's token is scoped "
    "to, through the tools available to you. For file location and context of the whole repository, you can read .agent/context.yaml file. You shouldn't provide any answer from other source or by yourself. Please provide answer from tools. Never claim to have made a "
    "change unless a tool call actually reports success. Keep answers short "
    "and name the specific code, content file you looked at."
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
                    "X-MCP-Toolsets": f"{_DEFAULT_TOOLSET_TOOLS}",
                    "X-MCP-Readonly": "true" if _READONLY else "false",
                },
                # ADK's default (5s) is too short for a remote MCP handshake.
                timeout=30.0,
            ),
        )
    ],
)

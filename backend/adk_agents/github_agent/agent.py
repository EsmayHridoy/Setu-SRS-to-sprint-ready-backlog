"""GitHub agent, exposed for the ADK dev UI (`adk web`).

Same GitHub agent as app/github_agent.py, redefined here as a module-level
`root_agent` because that is the shape `adk web` looks for when it scans an
agents directory. Kept as a separate definition rather than imported from
app/github_agent.py: that module is driven by app/config.py, which requires a
reachable DATABASE_URL to construct Settings -- a requirement this standalone
dev-UI entry point has no reason to inherit. It loads backend/.env directly
instead, the same file the main app reads its GitHub/Ollama settings from.
"""
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
_READONLY = os.getenv("GITHUB_MCP_READONLY", "true").lower() == "true"
_TOOLSETS = os.getenv("GITHUB_MCP_TOOLSETS", "default")
_MODEL = LiteLlm(
    model=f"ollama_chat/{os.getenv('OLLAMA_MODEL', 'qwen3.6:27b')}",
    api_base=os.getenv("OLLAMA_API_BASE", "http://localhost:11434"),
    # Maps to Ollama's own `think` param -- without it qwen writes its
    # reasoning inline as ordinary text with no way to separate it back out.
    reasoning_effort="high",
)

if _GITHUB_REPO:
    _REPO_HINT = (
        f"You have access to exactly one repository: `{_GITHUB_REPO}`. Use "
        "that \"owner/repo\" directly in every tool call -- never search "
        "GitHub by name to find it. This repository may be private -- that "
        "does not mean you lack access: your token has been explicitly "
        "granted read access to it specifically, so read its actual files "
        "and code with your tools rather than refusing or assuming you "
        "can't see its contents."
    )
else:
    _REPO_HINT = (
        "You have access to exactly one repository, whichever your access "
        "token is scoped to. If a tool needs an explicit owner/repo and you "
        "don't already know it, use a tool that reveals your own access "
        "context first rather than guessing or searching by name."
    )

# The exact tool names GitHub's hosted MCP server registers for
# X-MCP-Toolsets=default (context, repos, issues, pull_requests, users),
# captured directly from a live ListTools response. Stops the model from
# inventing a plausible-sounding but wrong answer when asked what it can do
# (observed: it claimed 4 search-only tools when it actually had these 28,
# including list_branches and get_file_contents -- exactly what it claimed
# not to have). Only accurate while GITHUB_MCP_TOOLSETS is "default".
_DEFAULT_TOOLSET_TOOLS = [
    "get_commit", "get_copilot_job_status", "get_file_contents", "get_label",
    "get_latest_release", "get_me", "get_release_by_tag", "get_tag",
    "get_team_members", "get_teams", "issue_read", "list_branches",
    "list_commits", "list_issue_fields", "list_issue_types", "list_issues",
    "list_pull_requests", "list_releases", "list_repository_collaborators",
    "list_tags", "pull_request_read", "run_secret_scanning", "search_code",
    "search_commits", "search_issues", "search_pull_requests",
    "search_repositories", "search_users",
]
if _TOOLSETS == "default":
    _tool_names = ", ".join(f"`{name}`" for name in _DEFAULT_TOOLSET_TOOLS)
    _TOOL_LIST_HINT = (
        f"Your exact available tools are: {_tool_names}. If asked what you "
        "can do, answer from this list -- never guess or describe a "
        "plausible-sounding but different set of tools."
    )
else:
    _TOOL_LIST_HINT = ""

_INSTRUCTION = (
    f"You are Setu's GitHub assistant. {_REPO_HINT} {_TOOL_LIST_HINT} You "
    "can read issues, pull requests, files and commits through the tools "
    "available to you. Never claim to have made a change unless a tool "
    "call actually reports success. Keep answers short and name the "
    "specific issue, PR or file you looked at."
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
                # ADK's default (5s) is too short for a remote MCP handshake.
                timeout=30.0,
            ),
        )
    ],
)

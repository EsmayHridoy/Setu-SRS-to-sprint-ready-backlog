from __future__ import annotations

import os
from pathlib import Path

import httpx
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
# GITHUB_REPO is "owner/repo" combined (same var app/github_mcp.py reads) --
# split once here instead of also requiring a separate GITHUB_OWNER var that
# could drift out of sync with it.
_OWNER, _, _REPO_NAME = os.getenv("GITHUB_REPO", "Mohiuddin2000/chatbot-api").partition("/")
_MCP_URL = os.getenv("GITHUB_MCP_URL", "https://api.githubcopilot.com/mcp/")
_BRANCH = os.getenv("GITHUB_BRANCH", "claude/context-file-gitlab-mcp-257fb0")
_READONLY = os.getenv("GITHUB_MCP_READONLY", "true").lower() == "true"
_OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "devstral:24b")
_OLLAMA_API_BASE = os.getenv("OLLAMA_API_BASE", "http://localhost:11434")


def _model_supports_thinking(model: str, api_base: str) -> bool:
    """Ollama rejects the whole request if `think` is passed to a model
    that doesn't support it (e.g. devstral) -- check first rather than
    assume, since models here vary and get swapped often. Same check as
    app/adk_runner.py, duplicated since this file deliberately doesn't
    import from the `app` package (see module docstring above).
    """
    try:
        resp = httpx.post(f"{api_base}/api/show", json={"model": model}, timeout=5.0)
        resp.raise_for_status()
        return "thinking" in resp.json().get("capabilities", [])
    except Exception:  # noqa: BLE001
        return False


# _MODEL was missing from this file (NameError) -- restored here, same
# capability-checked construction as app/adk_runner.py.
_model_kwargs = {"model": f"ollama_chat/{_OLLAMA_MODEL}", "api_base": _OLLAMA_API_BASE}
if _model_supports_thinking(_OLLAMA_MODEL, _OLLAMA_API_BASE):
    _model_kwargs["reasoning_effort"] = "high"
_MODEL = LiteLlm(**_model_kwargs)

_DEFAULT_TOOLSET_TOOLS = [
    "get_file_contents", "get_label", "get_me", "list_branches", "search_code",
    "search_repositories", "search_users"
]
_INSTRUCTION = (
    f"You are Setu's GitHub assistant. Github owner: {_OWNER}. You can read "
    f"contents, code, branches, repositories and users from the "
    f"{_OWNER}/{_REPO_NAME} repository, branch: {_BRANCH}, through the "
    "tools available to you. Whenever a tool needs `owner` and `repo` "
    f"arguments, always pass owner=\"{_OWNER}\" and repo=\"{_REPO_NAME}\" "
    "exactly (as two separate values, never the combined \"owner/repo\" "
    "form) -- never omit them or guess different values.\n\n"
    "You MUST call at least one tool before writing your answer, for every "
    "question, with no exceptions -- even if you think you already know "
    "the answer from general knowledge. Never answer from memory or common "
    "patterns in other codebases; this repository's actual behavior may "
    "differ, and only your tools can confirm it. Your first tool call, "
    "every time, before anything else, is always to read "
    "`.agent/context.yaml` at the root of the repository -- it maps "
    "feature names to the file locations that implement them. Use that "
    "mapping to go straight to the relevant file(s) with get_file_contents. "
    "Only fall back to a broader code search if the file you need isn't "
    "covered by that map.\n\n"
    "You shouldn't provide any answer from another source or by yourself; "
    "answer only from what your tools actually return. Never claim to have "
    "made a change unless a tool call actually reports success. Keep "
    "answers short and name the specific code or content file you looked at."
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
                    "X-MCP-Toolsets": "default",
                    "X-MCP-Readonly": "true" if _READONLY else "false",
                },
                # ADK's default (5s) is too short for a remote MCP handshake.
                timeout=30.0,
            ),
            # Restricts the "default" category's ~28 tools down to the
            # curated set above -- smaller tool-schema payload per request.
            tool_filter=_DEFAULT_TOOLSET_TOOLS,
        )
    ],
)

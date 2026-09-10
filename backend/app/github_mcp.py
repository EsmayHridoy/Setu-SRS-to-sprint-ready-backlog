"""The one GitHub MCP connection every agent in this app shares.

GITHUB_PAT is a single fine-grained personal access token scoped to exactly
one repository -- that token's own scope is what limits what any agent built
with this toolset can reach, not anything enforced here.
"""
from __future__ import annotations

from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)

from .config import get_settings


def require_configured() -> None:
    """Raise a clear error if GITHUB_PAT isn't set. No Ollama key check here
    -- it's local and unauthenticated; if it's not running, the completion
    call itself fails with a clear connection error.
    """
    if not get_settings().github_pat:
        raise RuntimeError(
            "GITHUB_PAT is not set. Add it to backend/.env -- see .env.example."
        )


def repo_hint() -> str:
    """A sentence telling the agent exactly which repo it can reach.

    Without this, an agent has no way to know the actual "owner/repo" slug
    and wastes tool calls guess-searching GitHub by whatever human-readable
    name it was given (a project's display name, say) -- which may not
    match the repo at all. Falls back to a vaguer hint if GITHUB_REPO isn't
    set, since the PAT's scope still limits the agent even without it named.
    """
    repo = get_settings().github_repo
    if repo:
        return (
            f"You have access to exactly one repository: `{repo}`. Use that "
            "\"owner/repo\" directly in every tool call -- never search "
            "GitHub by name to find it. This repository may be private -- "
            "that does not mean you lack access: your token has been "
            "explicitly granted read access to it specifically, so read its "
            "actual files and code with your tools rather than refusing or "
            "assuming you can't see its contents."
        )
    return (
        "You have access to exactly one repository, whichever your access "
        "token is scoped to. If a tool needs an explicit owner/repo and you "
        "don't already know it, use a tool that reveals your own access "
        "context first rather than guessing or searching by name."
    )


# The exact tool names GitHub's hosted MCP server registers for
# X-MCP-Toolsets=default (context, repos, issues, pull_requests, users),
# captured directly from a live ListTools response. If GITHUB_MCP_TOOLSETS
# is ever changed away from "default", this list stops matching reality --
# tool_list_hint() below deliberately says nothing rather than assert a
# wrong list in that case.
_DEFAULT_TOOLSET_TOOLS = [
    "get_file_contents", "get_label", "get_me", "list_branches", "search_code",
    "search_repositories", "search_users"
]


def tool_list_hint() -> str:
    """A sentence naming the agent's exact available tools, so it answers
    questions about its own capabilities from this instead of guessing.

    Observed directly in this app: a model asked "what tools do you have"
    invented a plausible-sounding but wrong answer -- 4 search-only tools --
    when it actually had 28, including list_branches and get_file_contents:
    exactly the ones it claimed not to have. The real tool schema is already
    sent with every request for actual tool *calls*; this reinforces the
    same facts in the system instruction for when the agent is just asked
    to *describe* what it can do, in plain conversation.
    """
    if get_settings().github_mcp_toolsets != "default":
        return ""
    names = ", ".join(f"`{name}`" for name in _DEFAULT_TOOLSET_TOOLS)
    return (
        f"Your exact available tools are: {names}. If asked what you can "
        "do, answer from this list -- never guess or describe a "
        "plausible-sounding but different set of tools."
    )


def build_toolset() -> McpToolset:
    settings = get_settings()
    return McpToolset(
        connection_params=StreamableHTTPConnectionParams(
            url=settings.github_mcp_url,
            headers={
                "Authorization": f"Bearer {settings.github_pat}",
                "X-MCP-Toolsets": settings.github_mcp_toolsets,
                "X-MCP-Readonly": "true" if settings.github_mcp_readonly else "false",
            },
            # ADK's default (5s) is too short for a remote MCP handshake and
            # was observed timing out in practice, silently leaving the agent
            # with no tools at all instead of failing loudly.
            timeout=30.0,
        ),
    )

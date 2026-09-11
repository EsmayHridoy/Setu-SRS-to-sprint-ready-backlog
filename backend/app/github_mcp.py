"""The one GitHub MCP connection every agent in this app shares.

GITHUB_PAT is a single fine-grained personal access token scoped to exactly
one repository -- that token's own scope is what limits what any agent built
with this toolset can reach, not anything enforced here.
"""
from __future__ import annotations

import anyio
from google.adk.tools.mcp_tool import McpToolset
from google.adk.tools.mcp_tool.mcp_session_manager import (
    StreamableHTTPConnectionParams,
)

from .config import get_settings


def require_configured() -> None:
    """Raise a clear error if GITHUB_PAT isn't set. No Gemini key check here
    -- if GEMINI_API_KEY is missing the model call itself fails with a clear
    auth error.
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
    settings = get_settings()
    repo = settings.github_repo
    branch = settings.github_branch
    branch_clause = (
        f" Read everything from the `{branch}` branch: pass it as the `ref` "
        "(branch) argument on every get_file_contents, search and list call, "
        "not the repository's default branch."
        if branch else ""
    )
    if repo:
        return (
            f"You have access to exactly one repository: `{repo}`. Use that "
            "\"owner/repo\" directly in every tool call -- never search "
            f"GitHub by name to find it.{branch_clause} This repository may be "
            "private -- that does not mean you lack access: your token has "
            "been explicitly granted read access to it specifically, so read "
            "its actual files and code with your tools rather than refusing or "
            "assuming you can't see its contents."
        )
    return (
        "You have access to exactly one repository, whichever your access "
        "token is scoped to. If a tool needs an explicit owner/repo and you "
        "don't already know it, use a tool that reveals your own access "
        f"context first rather than guessing or searching by name.{branch_clause}"
    )


# The exact tool names GitHub's hosted MCP server registers for
# X-MCP-Toolsets=default (context, repos, issues, pull_requests, users),
# captured directly from a live ListTools response. If GITHUB_MCP_TOOLSETS
# is ever changed away from "default", this list stops matching reality --
# tool_list_hint() below deliberately says nothing rather than assert a
# wrong list in that case.
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


def investigation_procedure() -> str:
    """The repo-grounding discipline the repo-reading agents share.

    Built around `.agent/context.yaml` as the single source of truth: every
    request reads that map first and follows its `paths` straight to the
    handful of files that implement the feature in question -- no blind
    searching, no directory crawling. That is both the efficiency lever (a
    couple of targeted reads instead of dozens of searches) and the
    truthfulness lever (every claim traceable to a file the map pointed to and
    the agent actually opened). Searching is a fallback used only when the map
    is missing or doesn't cover the area.
    """
    return (
        "STEP 1 - ALWAYS, on every single request, before anything else: read "
        "`.agent/context.yaml` with get_file_contents. This file is the "
        "authoritative map of the repository and it is how you decide what to "
        "read. You do NOT search the codebase to find things -- you look them "
        "up in this map. Read it first, every time, no exceptions.\n\n"
        "Its shape: a `project` summary; `conventions` (datasources, config "
        "files, shared constants); and a `features` list where each feature "
        "has `name`, `description`, `paths` (the exact files that implement "
        "it, most-relevant first), `key_symbols` (classes/methods to read), "
        "`related_features` (what a change ripples into) and `http` (its REST "
        "surface).\n\n"
        "STEP 2 - locate the feature: find the feature(s) in `features` whose "
        "name/description match the request. The map now tells you exactly "
        "which files matter -- you do not need to guess or search.\n\n"
        "STEP 3 - read only the mapped files: fetch ONLY the files listed "
        "under those features' `paths` with get_file_contents (for an impact "
        "question, also read the `paths` of their `related_features`). Do not "
        "search_code and do not crawl directories to rediscover files the map "
        "already names.\n\n"
        "STEP 4 - answer, grounded: base every statement on the file contents "
        "you actually read, never on a guess (how you present that -- business "
        "language or technical detail -- is set by your own instructions). The "
        "map plus the handful of files it names is enough, so as soon as you "
        "can answer, STOP calling tools and write the answer. Never read the "
        "same file twice.\n\n"
        "FALLBACK - only if `.agent/context.yaml` genuinely does not exist, or "
        "has no feature covering the request: then, and ONLY then, read the "
        "README and use search_code to locate the relevant files. Never fall "
        "back to searching while the map still covers the area. If even the "
        "fallback shows nothing, say so plainly rather than guessing."
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


async def close_toolset(toolset: McpToolset) -> None:
    """Close the MCP connection even when the turn was cancelled.

    Pressing Stop drops the SSE connection, and Starlette cancels the stream
    mid-turn. Without the shield, the cancellation interrupts this close too
    (observed: it never finishes), leaking the connection on every stop.
    """
    with anyio.CancelScope(shield=True):
        await toolset.close()

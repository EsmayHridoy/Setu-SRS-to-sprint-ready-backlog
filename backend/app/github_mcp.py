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

    Deliberately terse: this is re-sent on every model call in every tool
    round, so each clause has to earn its tokens. Two facts survive -- the
    slug to use, and that "private" does not mean "denied" (a model that
    concludes it lacks access stops investigating and starts guessing).
    """
    repo = get_settings().github_repo
    if repo:
        return (
            f"Your tools reach exactly one repository: `{repo}`. Pass that "
            "owner/repo to every tool call; never search GitHub to find it. "
            "If it appears private you still have read access -- read its "
            "files rather than assuming you cannot."
        )
    return (
        "Your tools reach exactly one repository, whichever your token is "
        "scoped to. If a tool needs an explicit owner/repo and you don't "
        "know it, call a tool that reveals your own access context rather "
        "than guessing."
    )


def context_map_hint() -> str:
    """A directive to check the repo's own feature->file map before
    searching, when GITHUB_REPO is set (the map is repo-specific, so this
    says nothing when no particular repo is configured).

    Deliberately emphatic ("every time, before anything else, is always"),
    and deliberately naming get_file_contents rather than saying "your
    file-reading tool": a model that treats reading the map as optional
    falls back to search_code, which costs far more tokens and often misses
    the file the map would have handed it directly. The emphasis is the
    cheapest part of this hint and the part that makes the rest pay off.

    What this no longer carries is the "always call a tool, never answer
    from memory" rule -- that is a grounding rule, it applies to every
    answer rather than to where you look first, and each agent's own
    instruction already states it. Saying it here too meant sending the
    same demand twice on every model call.

    `.agent/context.yaml` is a convention this repo maintains itself,
    mapping feature names to the files that implement them. Reading that one
    small file first lets the agent jump straight to get_file_contents on
    the right path instead of a broader, more token-expensive search_code
    call -- the whole point of maintaining the map in the first place.
    """
    if not get_settings().github_repo:
        return ""
    return (
        "Your first tool call, every time, before anything else, is always "
        "to read `.agent/context.yaml` at the root of the repository -- it "
        "maps feature names to the file locations that implement them. Use "
        "that mapping to go straight to the relevant file(s) with "
        "get_file_contents. Only fall back to a broader code search if the "
        "file you need isn't covered by that map."
    )


# A curated subset of the tools GitHub's hosted MCP server registers under
# X-MCP-Toolsets=default (context, repos, issues, pull_requests, users --
# ~28 tools total). X-MCP-Toolsets only filters by *category*, not by
# individual tool, so trimming to exactly these names is done client-side
# via McpToolset(tool_filter=...) in build_toolset() below, not the header.
# Keep this list and that tool_filter argument in sync -- they're meant to
# describe the exact same restriction.
# _DEFAULT_TOOLSET_TOOLS = [
#     "get_file_contents", "get_label", "get_me", "list_branches", "search_code",
#     "search_repositories", "search_users",
# ]
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
    when it actually had a different real set. The real tool schema is
    already sent with every request for actual tool *calls*; this reinforces
    the same facts in the system instruction for when the agent is just
    asked to *describe* what it can do, in plain conversation.
    """
    # Bare names, not `backticked` ones: the backticks alone tokenise to 29
    # tokens across these 28 names, on every model call, and buy nothing --
    # snake_case identifiers in a comma-separated list are already
    # unambiguous. Comma-separated rather than space-separated (which would
    # save 27 more) because this is the one hint whose entire job is being
    # read exactly right.
    names = ", ".join(_DEFAULT_TOOLSET_TOOLS)
    return (
        f"Your only tools are: {names}. Never call or name a tool outside "
        "this list; if asked what you can do, answer from it."
    )


def readonly_hint() -> str:
    """One sentence forbidding the agent from claiming it changed anything.

    Worth its tokens because the failure it prevents is the expensive kind:
    an agent that says it opened a PR or edited a file has produced a
    confident, checkable falsehood, and a reader who believes it acts on
    something that never happened. When GITHUB_MCP_READONLY is on, the
    write tools genuinely aren't there, so this states a fact rather than
    imposing a rule; when it's off, the weaker form from github_agent
    applies -- a change may be claimed only once a tool has confirmed it.
    """
    if get_settings().github_mcp_readonly:
        return (
            "Your access is strictly read-only. Never say or imply that you "
            "created, changed, merged or opened anything."
        )
    return (
        "Never claim to have made a change unless a tool call actually "
        "reported success."
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
        # Restricts the ~28 tools the "default" category returns down to the
        # curated set above -- smaller tool-schema payload per request, which
        # matters a lot for a local model's limited context/token budget.
        # tool_filter=_DEFAULT_TOOLSET_TOOLS,
    )

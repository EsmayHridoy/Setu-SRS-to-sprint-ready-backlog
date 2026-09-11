"""The chat agent's long-lived pieces, built once per process.

app/chat_agent.py used to build a fresh LlmAgent, a fresh GitHub MCP
connection and a throwaway InMemorySessionService on *every* message. That
cost a full MCP handshake plus a ~28-tool `tools/list` before the first
token of every reply, and -- because the session went out of scope with the
call -- gave the model no memory of the conversation it was in.

This module owns the four things that should outlive a request:

  * the LiteLlm model
  * the GitHub McpToolset (one connection, reused)
  * the LlmAgent (stateless config; the project it is answering about now
    arrives per-turn through session state, see chat_agent.instruction)
  * a DatabaseSessionService on the application's own Postgres, so a
    conversation's history survives both the request and a restart

Built lazily on first use rather than unconditionally at import: with
USE_PLACEHOLDER_AI=true or no GITHUB_PAT there is nothing to connect to, and
that must stay a clear error at call time (as github_mcp.require_configured
has always given) rather than a failure to boot. app/main.py's lifespan
calls startup() to pay the cost up front when it *is* configured.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from google.adk.agents import LlmAgent
from google.adk.runners import Runner
from google.adk.sessions import DatabaseSessionService
from google.adk.tools.mcp_tool import McpToolset

from .config import get_settings

log = logging.getLogger("setu")

APP_NAME = "setu-chat-agent"
# The single agent every conversation runs on. Shared with
# chat_agent, which authors session-state events as this agent --
# ADK warns about events from an agent it doesn't know.
AGENT_NAME = "chat_agent"


@dataclass
class ChatRuntime:
    agent: LlmAgent
    toolset: McpToolset
    session_service: DatabaseSessionService
    runner: Runner


_runtime: ChatRuntime | None = None
_build_lock = asyncio.Lock()

# One lock per conversation, so two turns in the same conversation can never
# append events to the same session concurrently. This did not matter while
# every turn had its own throwaway session; now that a session is durable and
# shared across requests, interleaved appends would leave a permanently
# corrupt history (two user turns in a row, or a function_call whose
# function_response landed in the middle of another turn). Turns in
# *different* conversations still run in parallel.
_turn_locks: dict[str, asyncio.Lock] = {}
_turn_locks_guard = asyncio.Lock()


async def _build() -> ChatRuntime:
    # Imported here, not at module scope: chat_agent imports this module for
    # the runtime, and this needs chat_agent's instruction.
    from . import chat_agent, github_mcp
    from .adk_runner import build_model

    github_mcp.require_configured()
    settings = get_settings()

    toolset = github_mcp.build_toolset()
    agent = LlmAgent(
        model=build_model(),
        name=AGENT_NAME,
        instruction=chat_agent.instruction(),
        tools=[toolset],
    )

    # ADK keeps its sessions in its own tables in the same database the rest
    # of the app uses. DATABASE_URL is already "postgresql+psycopg://...",
    # and psycopg 3 is an async driver, so it works as-is with the
    # create_async_engine DatabaseSessionService builds internally.
    session_service = DatabaseSessionService(db_url=settings.database_url)
    # Otherwise ADK creates its tables lazily inside the first turn, which
    # would surface a schema error mid-stream instead of at startup. This is
    # the one place the app creates tables (see main.py's note on why the
    # application's own schema is left to db/POSTGRES.md) -- these are ADK's
    # own tables, and it would create them on its own regardless.
    await session_service.prepare_tables()

    runner = Runner(app_name=APP_NAME, agent=agent,
                    session_service=session_service)

    # McpToolset connects lazily -- building it opens nothing. Listing the
    # tools now is what actually performs the handshake, so the cost lands
    # here (once, at startup) instead of inside the first user's first
    # message, and an unreachable server or a dead PAT shows up as a startup
    # warning rather than a stalled reply.
    tools = await toolset.get_tools()

    log.info("Chat runtime ready: model=%s, %d MCP tools, sessions=ADK/Postgres",
             settings.ollama_model, len(tools))
    return ChatRuntime(agent=agent, toolset=toolset,
                       session_service=session_service, runner=runner)


async def get_runtime() -> ChatRuntime:
    """The process-wide chat runtime, building it on first use.

    Raises RuntimeError (from github_mcp.require_configured) if GITHUB_PAT
    isn't set -- callers already turn that into a 503.
    """
    global _runtime
    if _runtime is not None:
        return _runtime
    async with _build_lock:
        if _runtime is None:  # another request may have built it while we waited
            _runtime = await _build()
    return _runtime


async def turn_lock(conversation_id: str) -> asyncio.Lock:
    async with _turn_locks_guard:
        lock = _turn_locks.get(conversation_id)
        if lock is None:
            lock = _turn_locks[conversation_id] = asyncio.Lock()
        return lock


async def release_turn_lock(conversation_id: str) -> None:
    """Drop a conversation's lock once nothing is waiting on it, so the dict
    doesn't grow for the life of the process."""
    async with _turn_locks_guard:
        lock = _turn_locks.get(conversation_id)
        if lock is not None and not lock.locked():
            del _turn_locks[conversation_id]


async def startup() -> None:
    """Build the runtime during application startup when it's configured to
    run at all, so the first user message doesn't pay for the MCP handshake.

    Best-effort on purpose: an unreachable MCP server or a missing token must
    not stop the app from serving everything that doesn't need an agent.
    """
    settings = get_settings()
    if settings.use_placeholder_ai or not settings.github_pat:
        return
    try:
        await get_runtime()
    except Exception as exc:  # noqa: BLE001 - degraded, not fatal; retried per-request
        log.warning("Chat runtime not ready at startup (%s). It will be "
                    "rebuilt on the first request that needs it.", exc)


async def shutdown() -> None:
    global _runtime
    if _runtime is None:
        return
    runtime, _runtime = _runtime, None
    try:
        await runtime.toolset.close()
    except Exception as exc:  # noqa: BLE001 - shutting down anyway
        log.warning("Chat runtime: MCP toolset did not close cleanly: %s", exc)
    try:
        await runtime.session_service.close()
    except Exception as exc:  # noqa: BLE001 - shutting down anyway
        log.warning("Chat runtime: session service did not close cleanly: %s", exc)

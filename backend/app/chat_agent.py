"""Chat's real answer engine -- replaces app/placeholder_ai.py when
USE_PLACEHOLDER_AI=false.

A local-Ollama-backed ADK agent answers questions about a project by investigating
the live repository through the same global-PAT GitHub MCP connection
app/github_agent.py and app/business_agent.py use (see app/github_mcp.py).
Kept as its own module, distinct from those two, because its persona is
different: it answers project questions the way placeholder_ai's canned
answers do, not "help with GitHub" or "vet this business requirement."

Unlike those two, this agent is *conversational*: one durable ADK session per
Setu conversation, keyed by the conversation's own id, so each turn is
answered with the previous turns in view. The agent itself, its MCP
connection and the session store are process-wide and live in
app/chat_runtime.py; this module only maps a Setu conversation onto an ADK
session and runs the turn.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from google.adk.events import Event, EventActions
from google.adk.sessions import BaseSessionService, Session

from . import github_mcp
from .adk_runner import run_turn, stream_turn
from .chat_runtime import (
    AGENT_NAME, APP_NAME, get_runtime, release_turn_lock, turn_lock,
)
from .config import get_settings

__all__ = ["AGENT_NAME", "APP_NAME", "answer", "answer_stream", "forget", "instruction"]

# One template, formatted once at startup -- the same shape github_agent.py
# and business_agent.py use, so all three agents read the same way.
#
# {{project_name}} is doubled on purpose. str.format() collapses `{{` to a
# single `{`, so what comes out of instruction() still contains the literal
# text {project_name}, which ADK then substitutes from the session's state on
# every turn (google.adk.utils.instructions_utils). That is what lets one
# shared agent serve every project, and therefore what lets the agent and its
# MCP connection be built once per process instead of once per message.
#
# The single-braced names below are ordinary format placeholders, filled in
# by instruction(). Nothing else here may wrap a valid Python identifier in
# braces, or ADK would try to resolve it as session state.
#
# Trimmed to the least this agent's local model needs to behave. This
# instruction goes out on every model call in every tool round, and a small
# local model degrades as the prompt grows -- it starts skimming the rules
# instead of following them -- so length here costs accuracy, not just
# tokens. What survives is only what a dropped clause was observed to break:
#
#   * the repo slug, without which every tool call has to guess its target;
#   * context_map_hint verbatim, the one clause that decides where the agent
#     looks first and so what the whole turn costs. Its last sentence, the
#     fallback to a broader search, is load-bearing whenever the repo has no
#     .agent/context.yaml -- which is the case for the repo this is pointed
#     at today, so it stays;
#   * one grounding sentence and one read-only sentence.
#
# Deliberately gone: github_mcp.tool_list_hint(), whose 28 tool names were
# by far the largest block here and are already sent with every request as
# the real tool schema; and the longer grounding paragraph, whose several
# ways of saying "use your tools" the sentence below says once. The full
# repo_hint is shortened to the slug alone for the same reason.
_INSTRUCTION_TEMPLATE = (
    "You are Setu's assistant for the project \"{{project_name}}\". "
    "{repo_hint} {context_map_hint} {readonly_hint} Branch: claude/context-file-gitlab-mcp-257fb0"
    "Ground every claim in output your tools actually returned and name the "
    "file path it came from; if they don't show enough, say so rather than "
    "guessing. Answer briefly: the conclusion first, then the evidence."
)


def _repo_hint() -> str:
    """The repo slug alone -- chat_agent's cut-down github_mcp.repo_hint().

    The shared hint spends most of its words on failure modes the other two
    agents hit (searching GitHub for the slug, reading "private" as
    "denied"). This agent always runs against the configured repo, so only
    the slug has to survive. Falls back to the shared wording when
    GITHUB_REPO isn't set, since then there is no slug to state.
    """
    repo = get_settings().github_repo
    if not repo:
        return github_mcp.repo_hint()
    return f"Your tools reach exactly one repository, `{repo}`. Pass that " \
           "owner/repo to every tool call."


def instruction() -> str:
    """The agent's full system instruction, assembled once at startup.

    Everything except {project_name} is fixed for the life of the process --
    the hints all derive from settings -- so only the project varies, and it
    varies per session rather than per agent.

    readonly_hint comes from github_agent, which was the one place in the app
    that ever forbade claiming a change it hadn't made. chat_agent is the
    agent an end user actually talks to, so it needed that rule most and had
    it least.
    """
    text = _INSTRUCTION_TEMPLATE.format(
        repo_hint=_repo_hint(),
        context_map_hint=github_mcp.context_map_hint(),
        readonly_hint=github_mcp.readonly_hint(),
    )
    # context_map_hint() is empty when GITHUB_REPO isn't set, which would
    # otherwise leave a double space in the middle of the prompt.
    return " ".join(text.split())


async def _session(service: BaseSessionService, *, conversation_id: str,
                   user_id: str, project_name: str) -> Session:
    """The ADK session for a Setu conversation, created on first use.

    The conversation's own id *is* the session id: a Setu conversation and an
    ADK session are the same thing viewed from two sides, so there is nothing
    to map and nothing to store. Fetch first, create only on a miss -- a hit
    is what carries the history forward.
    """
    session = await service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=conversation_id,
    )
    if session is None:
        return await service.create_session(
            app_name=APP_NAME, user_id=user_id, session_id=conversation_id,
            state={"project_name": project_name},
        )

    # A project can be renamed after a conversation has started, which would
    # otherwise leave this session addressing the agent by a name the project
    # no longer has. State only changes through events, so record the rename
    # as one.
    if session.state.get("project_name") != project_name:
        await service.append_event(
            session,
            Event(author=AGENT_NAME,
                  actions=EventActions(
                      state_delta={"project_name": project_name})),
        )
    return session


async def answer(project_name: str, question: str, *, user_id: str,
                 conversation_id: str) -> str:
    runtime = await get_runtime()
    lock = await turn_lock(conversation_id)
    try:
        async with lock:
            session = await _session(
                runtime.session_service, conversation_id=conversation_id,
                user_id=user_id, project_name=project_name,
            )
            text = await run_turn(runtime.runner, question, user_id=user_id,
                                  session_id=session.id)
    finally:
        # Outside the `async with`: the lock is only discardable once it has
        # actually been released.
        await release_turn_lock(conversation_id)
    return text or "The agent returned no response."


async def answer_stream(project_name: str, question: str, *, user_id: str,
                        conversation_id: str,
                        ) -> AsyncIterator[tuple[bool, str]]:
    runtime = await get_runtime()
    lock = await turn_lock(conversation_id)
    try:
        async with lock:
            session = await _session(
                runtime.session_service, conversation_id=conversation_id,
                user_id=user_id, project_name=project_name,
            )
            async for is_final, text in stream_turn(
                runtime.runner, question, user_id=user_id,
                session_id=session.id,
            ):
                yield is_final, text
    finally:
        # Outside the `async with`, as in answer(). Reached on an abandoned
        # stream too -- the caller closing the generator throws GeneratorExit
        # in here, which releases the lock rather than stranding it.
        await release_turn_lock(conversation_id)


async def forget(conversation_id: str, *, user_id: str) -> None:
    """Drop the ADK session behind a deleted conversation.

    Without this the history outlives the conversation it belongs to: rows
    nothing can reach, and -- since the session id is the conversation id --
    a transcript that a future conversation reusing that id would inherit.
    Never raises: the Setu-side delete has already been committed by the time
    this is called, and failing to tidy up must not turn that into an error.
    """
    try:
        runtime = await get_runtime()
        await runtime.session_service.delete_session(
            app_name=APP_NAME, user_id=user_id, session_id=conversation_id,
        )
    except Exception:  # noqa: BLE001 - best effort; nothing left to tell the user
        pass

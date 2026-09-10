"""Chat's real answer engine -- replaces app/placeholder_ai.py when
USE_PLACEHOLDER_AI=false.

A local-Ollama-backed ADK agent answers questions about a project by investigating
the live repository through the same global-PAT GitHub MCP connection
app/github_agent.py and app/business_agent.py use (see app/github_mcp.py).
Kept as its own module, distinct from those two, because its persona is
different: it answers project questions the way placeholder_ai's canned
answers do, not "help with GitHub" or "vet this business requirement."
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from google.adk.agents import LlmAgent
from google.adk.tools.mcp_tool import McpToolset

from . import github_mcp
from .adk_runner import build_model, run_single_turn, stream_single_turn

APP_NAME = "setu-chat-agent"

_INSTRUCTION_TEMPLATE = (
    "You are Setu's assistant for the project \"{project}\". {repo_hint} "
    "{tool_list_hint} "
    "Answer the user's question about how this system behaves by "
    "investigating the actual repository through the tools available to "
    "you -- do not guess or answer from general knowledge alone. If the "
    "repository doesn't show enough to answer confidently, say so plainly "
    "rather than speculating. Keep answers focused, and name the specific "
    "file, function or code path you looked at."
)


def _build_agent(project_name: str) -> tuple[LlmAgent, McpToolset]:
    github_mcp.require_configured()
    toolset = github_mcp.build_toolset()
    agent = LlmAgent(
        model=build_model(),
        name="chat_agent",
        instruction=_INSTRUCTION_TEMPLATE.format(
            project=project_name,
            repo_hint=github_mcp.repo_hint(),
            tool_list_hint=github_mcp.tool_list_hint(),
        ),
        tools=[toolset],
    )
    return agent, toolset


async def answer(project_name: str, question: str, *, user_id: str) -> str:
    agent, toolset = _build_agent(project_name)
    try:
        text = await run_single_turn(
            agent, question, app_name=APP_NAME, user_id=user_id,
        )
    finally:
        await toolset.close()
    return text or "The agent returned no response."


async def answer_stream(project_name: str, question: str, *,
                        user_id: str) -> AsyncIterator[tuple[bool, str]]:
    agent, toolset = _build_agent(project_name)
    try:
        async for is_final, text in stream_single_turn(
            agent, question, app_name=APP_NAME, user_id=user_id,
        ):
            yield is_final, text
    finally:
        await toolset.close()

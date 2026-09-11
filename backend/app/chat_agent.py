"""Chat's real answer engine -- replaces app/placeholder_ai.py when
USE_PLACEHOLDER_AI=false.

A Gemini-backed ADK agent answers questions about a project by investigating
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
from .adk_runner import (
    build_generate_config, build_model, run_single_turn, stream_single_turn,
)
from .chat_memory import with_history

APP_NAME = "setu-chat-agent"

_INSTRUCTION_TEMPLATE = (
    "You are Setu's assistant, helping a Business Analyst on the project "
    "\"{project}\". The person reading you is NOT a developer -- they think "
    "in features, business rules and impact, not in code. {repo_hint}\n\n"
    "Investigate the repository to ground yourself, but do it SILENTLY. Never "
    "reveal the mechanics: do not mention or list the tools you use, do not "
    "name the repository, and do not put file paths, class or function names, "
    "or database table/column names into your answer. The BA does not want to "
    "read code -- they want to understand how the business behaves.\n\n"
    "{procedure}\n\n"
    "Then answer ENTIRELY in plain business language:\n"
    "- Describe what the system actually does today in the area asked about -- "
    "the real features and rules you found, stated the way a BA or client "
    "would say them, not in technical terms.\n"
    "- When the user proposes a requirement, tell them whether it is already "
    "supported today, whether it is feasible given how the system currently "
    "works, and which other business areas or features it would affect.\n"
    "- Base everything on what the repository really shows. If it does not "
    "show enough, say so plainly -- never invent a feature that isn't there.\n"
    "- If asked what you can do, answer in business terms (you explain what "
    "this system does today, whether a new requirement fits and is feasible, "
    "and what it impacts) -- never by listing tools or technical abilities.\n\n"
    "OUTPUT FORMAT -- the chat shows your reply as PLAIN TEXT with line breaks "
    "kept, but it does NOT render Markdown. Structure it with headings and "
    "points but WITHOUT Markdown symbols:\n"
    "- Put each section heading on its own line in Title Case ending with a "
    "colon (e.g. 'Currently Supported:'), with a blank line before it. No # "
    "or ## and no ** ** -- they show up as literal broken characters.\n"
    "- Under a heading use points starting with '- ' (dash + space), or "
    "numbered 1., 2. when order matters; indent sub-points two spaces.\n"
    "- Never use backticks or ``` code fences.\n"
    "- Open with a one-line summary, then the sections; keep it tight.\n\n"
    "When you assess whether a requirement is possible, use exactly these "
    "headings in order: 'Summary:', 'Currently Supported:', 'Feasibility:', "
    "'Business Impact:'. Under each, a few short points, all in business "
    "language.\n\n"
    "The message may open with the conversation so far. Use it to resolve "
    "what the user is referring to (\"that rule\", \"the second requirement\", "
    "an earlier answer) and to build on earlier turns -- including business "
    "requirements extracted from documents they uploaded and the vetting "
    "results for them -- instead of starting from scratch. Answer only the "
    "current message; do not repeat or re-answer earlier ones."
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
            procedure=github_mcp.investigation_procedure(),
        ),
        tools=[toolset],
        generate_content_config=build_generate_config(),
    )
    return agent, toolset


async def answer(project_name: str, question: str, *, user_id: str,
                 history: str = "") -> str:
    """`history` is the conversation so far from chat_memory.build_history()."""
    agent, toolset = _build_agent(project_name)
    try:
        text = await run_single_turn(
            agent, with_history(history, question),
            app_name=APP_NAME, user_id=user_id,
        )
    finally:
        await toolset.close()
    return text or "The agent returned no response."


async def answer_stream(project_name: str, question: str, *, user_id: str,
                        history: str = "") -> AsyncIterator[tuple[bool, str]]:
    agent, toolset = _build_agent(project_name)
    try:
        async for is_final, text in stream_single_turn(
            agent, with_history(history, question),
            app_name=APP_NAME, user_id=user_id,
        ):
            yield is_final, text
    finally:
        await toolset.close()

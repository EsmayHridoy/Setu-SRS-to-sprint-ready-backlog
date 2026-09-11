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
    "- Base everything on what the repository really shows. If it does not "
    "show enough, say so plainly -- never invent a feature that isn't there.\n"
    "- If asked what you can do, answer in business terms (you explain what "
    "this system does today, whether a new requirement fits and is feasible, "
    "and what it impacts) -- never by listing tools or technical abilities.\n\n"
    "When the user proposes or asks you to verify a business requirement, "
    "walk through exactly these three steps, same as the formal vetting "
    "process does, and cover all of them:\n"
    "1. Current business -- describe fully how the specific area this "
    "requirement touches works today: its features, rules and flow as they "
    "exist now.\n"
    "2. Feasibility -- say whether the requirement itself is clear enough to "
    "act on, or vague/ambiguous enough that the client should be asked to "
    "clarify it first; then, given how the system works today, say whether "
    "it is feasible to build.\n"
    "3. Integration and impact -- decide whether this is already supported "
    "today, should be added onto an existing feature (name that feature), or "
    "needs a new feature/endpoint of its own; then say plainly whether other "
    "features are affected and which ones.\n\n"
    "OUTPUT FORMAT -- the chat shows your reply as PLAIN TEXT with line breaks "
    "kept, but it does NOT render Markdown. Structure it with headings and "
    "points but WITHOUT Markdown symbols:\n"
    "- Put each section heading on its own line in Title Case ending with a "
    "colon (e.g. 'Current Business:'), with a blank line before it. No # "
    "or ## and no ** ** -- they show up as literal broken characters.\n"
    "- Under a heading use points starting with '- ' (dash + space), or "
    "numbered 1., 2. when order matters; indent sub-points two spaces.\n"
    "- Never use backticks or ``` code fences.\n"
    "- Open with a one-line summary, then the sections; keep it tight.\n\n"
    "When you assess whether a requirement is possible, use exactly these "
    "headings in order: 'Summary:', 'Current Business:', 'Feasibility:', "
    "'Integration & Impact:'. Under each, a few short points, all in business "
    "language -- 'Current Business:' is step 1, 'Feasibility:' is step 2, "
    "'Integration & Impact:' is step 3.\n\n"
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
        generate_content_config=build_generate_config(show_thinking=True),
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
        await github_mcp.close_toolset(toolset)
    return text or "The agent returned no response."


async def answer_stream(project_name: str, question: str, *, user_id: str,
                        history: str = "") -> AsyncIterator[tuple[str, str]]:
    """(kind, text) pairs from adk_runner.stream_single_turn: STATUS progress
    lines, DELTA pieces of the answer, then the FINAL answer."""
    agent, toolset = _build_agent(project_name)
    try:
        async for kind, text in stream_single_turn(
            agent, with_history(history, question),
            app_name=APP_NAME, user_id=user_id,
        ):
            yield kind, text
    finally:
        await github_mcp.close_toolset(toolset)

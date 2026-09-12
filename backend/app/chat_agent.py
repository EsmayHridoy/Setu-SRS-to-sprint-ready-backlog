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

import asyncio
from collections.abc import AsyncIterator

from google.adk.agents import LlmAgent
from google.adk.tools import FunctionTool
from google.adk.tools.mcp_tool import McpToolset

from . import github_mcp
from .adk_runner import (
    build_generate_config, build_model, run_single_turn, stream_single_turn,
)
from .chat_memory import with_history
from .db import SessionLocal
from .models import BusinessItem, BusinessPlan

APP_NAME = "setu-chat-agent"


def _build_business_items_tools(project_id: str, user_id: str,
                                conversation_id: str,
                                created_plan_ids: list[str],
                                ) -> list[FunctionTool]:
    """The two local (non-MCP) tools the chat agent uses against this app's
    own database, both scoped to `conversation_id` by closure -- never a
    model-supplied argument, so the agent cannot widen its own reach to
    another chat, even within the same project. A vetting request in one
    chat must never read as though it "already knows" about an item that
    was only ever discussed in a different chat -- each conversation's view
    of the project's business items is independent (see V9 migration).

    `created_plan_ids` is an output parameter: `create_business_item`
    appends to it as a side effect, since a FunctionTool's return value
    only reaches the model, not the caller of answer()/answer_stream(). The
    router reads it back afterwards to link the chat reply to the new plan
    the same way an uploaded document's reply is (see
    app/routers/chat.py's _plan_reply_stream), so it renders as the same
    editable draft-plan card in the thread.
    """
    def _query(fn):
        """Run a synchronous SQLAlchemy closure in a worker thread -- these
        tools run inside the same event loop the rest of the turn awaits on,
        and SessionLocal is a plain, non-async session."""
        return asyncio.to_thread(fn)

    async def list_known_business_requirements() -> list[dict]:
        """List every business requirement already tracked in THIS chat
        conversation -- each with its current status and, if vetted, the
        verdict -- so a question or a vetting request can be answered from
        the existing record instead of redoing work already done.

        Never includes items from a different chat, or from a document
        uploaded directly on the Business tab -- only what this
        conversation itself extracted or logged. Excludes items whose plan
        was discarded (the BA chose not to pursue that document, so those
        items should be treated as if they never existed). Capped and
        newest-first so the list stays small.
        """
        def _run() -> list[dict]:
            db = SessionLocal()
            try:
                rows = (
                    db.query(BusinessItem)
                    .join(BusinessPlan, BusinessItem.plan_id == BusinessPlan.id)
                    .filter(BusinessPlan.conversation_id == conversation_id)
                    .filter(BusinessPlan.status != "DISCARDED")
                    .order_by(BusinessPlan.created_at.desc())
                    .limit(200)
                    .all()
                )
                return [
                    {
                        "description": row.description,
                        "vetting_status": row.vetting_status,
                        "is_feasible": row.is_feasible,
                        "verdict": row.verdict,
                    }
                    for row in rows
                ]
            finally:
                db.close()
        return await _query(_run)

    async def create_business_item(description: str) -> dict:
        """Log a new business requirement for this project as a draft,
        PENDING item, ready for the Business Analyst to review and vet.

        Call this ONLY when the user has explicitly asked you to vet
        something (not just asked a feasibility question) AND your
        business-requirements lookup found no existing match for it.
        Never call this for a plain question, and never call it twice for
        the same requirement in one message. `description` must be a
        clear, well-formed statement of the requirement in your own
        words -- not the user's raw, casual phrasing.
        """
        def _run() -> str:
            db = SessionLocal()
            try:
                plan = BusinessPlan(project_id=project_id, user_id=user_id,
                                    conversation_id=conversation_id,
                                    source_filename="(from chat)")
                db.add(plan)
                db.flush()
                db.add(BusinessItem(plan_id=plan.id, seq_no=1,
                                    description=description.strip()))
                db.commit()
                return plan.id
            finally:
                db.close()
        plan_id = await _query(_run)
        created_plan_ids.append(plan_id)
        return {"logged": True}

    return [
        FunctionTool(list_known_business_requirements),
        FunctionTool(create_business_item),
    ]


_INSTRUCTION_TEMPLATE = (
    "You are Setu's assistant for the project \"{project}\". {repo_hint}\n\n"
    "FIRST, work out what kind of message this is and answer accordingly -- "
    "size up the message the way any competent assistant (Claude, ChatGPT) "
    "would, rather than forcing every message into one fixed shape:\n\n"
    "CASE 1a -- GREETING / SMALL TALK: hi, hello, kemon acho, thanks, bye "
    "and the like, with no actual question in them. Reply with a normal, "
    "warm greeting back, then invite them to ask about this project -- e.g. "
    "\"How can I help you with this project?\" (in their own language). Do "
    "NOT investigate the repository, do NOT use headings.\n\n"
    "CASE 1b -- OFF-TOPIC: an actual question or request that has nothing "
    "to do with this project (general knowledge, writing help, a coding "
    "question unrelated to this project, another product, etc). Do NOT "
    "answer it. Politely say you are this project's assistant and can only "
    "help with questions about this project, in one short sentence, in the "
    "user's own language. Do NOT investigate the repository, do NOT use "
    "headings, do NOT attempt the actual question even partially.\n\n"
    "CASE 2 -- ABOUT THIS PROJECT: anything grounded in this project -- what "
    "the system does today, how a feature or rule currently works, which "
    "project(s) you can see, AND also a new idea or a specific requirement "
    "the user asks you to think through, including whether it looks "
    "feasible given how things work today. This is a normal, informative "
    "conversation about the business and the repository behind it -- give "
    "your own grounded take, do not refuse to engage with a new idea just "
    "because it is new.\n\n"
    "CASE 3 -- AN EXPLICIT VETTING REQUEST: the user is not just asking "
    "whether something is feasible -- they are asking you to actually "
    "PERFORM vetting on it (\"vet this\", \"please assess this formally\", "
    "\"process this as a requirement\", \"add this to the backlog\" and the "
    "like). Never confuse this with a plain feasibility question (that is "
    "still case 2) -- case 3 is specifically about the ACT of vetting.\n\n"
    "For BOTH case 2 and case 3, before doing anything else, check the "
    "existing record: call your business-requirements lookup tool once and "
    "look for an item whose description clearly matches what the user is "
    "talking about (same underlying idea, even if worded differently -- not "
    "just a keyword overlap). Do not call it more than once per message.\n"
    "- MATCH, already vetted (vetting_status DONE): do not re-analyze or "
    "re-investigate. For case 2, just relay that item's existing verdict "
    "and reasoning in plain conversational language. For case 3, tell the "
    "user it has already been vetted, briefly state the verdict, and ask "
    "whether they want it re-vetted -- if so, tell them to open that item "
    "on the Business tab and add a comment there explaining what should "
    "change; that re-vets it in place, no re-upload needed. You still "
    "cannot revise the verdict yourself, in chat, under any circumstance.\n"
    "- MATCH, still pending (vetting_status PENDING): for case 2, say it "
    "has already been submitted and is awaiting vetting, and share the "
    "requirement text so they know what's already logged. For case 3, tell "
    "them it is already submitted and waiting to be vetted -- nothing more "
    "to do.\n"
    "- NO MATCH: for case 2, investigate the repository yourself (see "
    "procedure below) and give your own grounded feasibility/impact take, "
    "as normal -- informal analysis, not a formal record. If it's clear the "
    "user wants an official record for the backlog, add one short line "
    "pointing them to the Business tab -- a footnote, never a reason to "
    "withhold your own analysis. For case 3, you cannot vet it yourself, "
    "but you CAN log it: call create_business_item with a clean, "
    "well-written statement of the requirement, then tell the user you've "
    "added it to the Business tab for review -- it will go through the "
    "same review and vetting as anything uploaded there. Never call "
    "create_business_item for a case 2 message, only case 3.\n\n"
    "Never apply case 2/3 handling to a case 1a/1b message -- an off-topic "
    "question does not get investigated or answered just because it sounds "
    "detailed or technical.\n\n"
    "For CASE 2 and 3, remember who is reading you: a Business Analyst, not "
    "a developer -- they think in features, business rules and impact, not "
    "in code. Investigate the repository to ground yourself, but do it "
    "SILENTLY: never reveal the mechanics, never mention or list the tools "
    "you use, never name the repository, and never put file paths, class or "
    "function names, or database table/column names into your answer. "
    "Follow this procedure before investigating the repository (skip "
    "entirely for case 1a, 1b, and any case 2/3 turn resolved from the "
    "existing record without needing the repository):\n\n"
    "{procedure}\n\n"
    "For CASE 2 and 3 answers:\n"
    "- Describe what the system actually does today in the area asked about "
    "-- the real features and rules you found, stated the way a BA or "
    "client would say them, not in technical terms.\n"
    "- Base everything on what the repository or the existing record really "
    "shows. If it does not show enough, say so plainly -- never invent a "
    "feature or a verdict that isn't there.\n"
    "- If asked what you can do, answer in business terms (you explain what "
    "this system does today, that you can think through a new idea's fit "
    "and feasibility, and that formal vetting itself happens on the "
    "Business tab) -- never by listing tools or technical abilities.\n\n"
    "OUTPUT FORMAT -- the chat shows your reply as PLAIN TEXT with line "
    "breaks kept, but it does NOT render Markdown. There is no fixed shape "
    "or heading structure for any case in chat -- reply however reads best "
    "for a normal conversation: prose, or a short dash-point list if that's "
    "genuinely clearer. No # or ## and no ** ** -- they show up as literal "
    "broken characters. Never use backticks or ``` code fences. Keep every "
    "answer tight -- case 1a and 1b are one or two sentences, no more.\n\n"
    "The message may open with the conversation so far. Use it to resolve "
    "what the user is referring to (\"that rule\", \"the second requirement\", "
    "an earlier answer) and to build on earlier turns -- including business "
    "requirements extracted from documents they uploaded and the vetting "
    "results for them -- instead of starting from scratch. Answer only the "
    "current message; do not repeat or re-answer earlier ones."
)


def _build_agent(project_name: str, project_id: str, user_id: str,
                 conversation_id: str, created_plan_ids: list[str],
                 ) -> tuple[LlmAgent, McpToolset]:
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
        tools=[toolset, *_build_business_items_tools(
            project_id, user_id, conversation_id, created_plan_ids)],
        generate_content_config=build_generate_config(show_thinking=True),
    )
    return agent, toolset


async def answer(project_name: str, question: str, *, project_id: str,
                 user_id: str, conversation_id: str, history: str = "",
                 created_plan_ids: list[str] | None = None) -> str:
    """`history` is the conversation so far from chat_memory.build_history().

    `created_plan_ids` is an optional output list: if the agent logs a new
    business item this turn (see _build_business_items_tools), its plan id
    is appended to it, so the caller can link the reply to that plan the
    same way an uploaded document's reply is linked.
    """
    agent, toolset = _build_agent(
        project_name, project_id, user_id, conversation_id,
        created_plan_ids if created_plan_ids is not None else [],
    )
    try:
        text = await run_single_turn(
            agent, with_history(history, question),
            app_name=APP_NAME, user_id=user_id,
        )
    finally:
        await github_mcp.close_toolset(toolset)
    return text or "The agent returned no response."


async def answer_stream(project_name: str, question: str, *, project_id: str,
                        user_id: str, conversation_id: str, history: str = "",
                        created_plan_ids: list[str] | None = None,
                        ) -> AsyncIterator[tuple[str, str]]:
    """(kind, text) pairs from adk_runner.stream_single_turn: STATUS progress
    lines, DELTA pieces of the answer, then the FINAL answer. See answer()
    for `created_plan_ids`."""
    agent, toolset = _build_agent(
        project_name, project_id, user_id, conversation_id,
        created_plan_ids if created_plan_ids is not None else [],
    )
    try:
        async for kind, text in stream_single_turn(
            agent, with_history(history, question),
            app_name=APP_NAME, user_id=user_id,
        ):
            yield kind, text
    finally:
        await github_mcp.close_toolset(toolset)

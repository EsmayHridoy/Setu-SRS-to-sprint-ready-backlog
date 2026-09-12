"""Run one turn against an ADK agent, either all at once or streamed.

Shared by every agent call in this app (app/github_agent.py, app/business_agent.py,
app/chat_agent.py): build a throwaway session, run the agent once, pull text out
of the event stream. Conversation history is folded into the prompt by the
caller (app/chat_memory.py) rather than kept in an ADK session, so a fresh
InMemorySessionService per call is enough.

Besides the answer, a turn reports what the agent is doing while it works --
"Reading the project's feature map", "Searching the code for 'approver'", or a
heading from the model's own thought summary. These are progress lines for
the UI only: callers show them and drop them, and they never reach the
stored reply, so they never reach the conversation history either. They are
worded for the Business Analyst reading the chat -- no tool names, no file
paths -- the same rule the agents' own answers follow.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator

from google.adk.agents import LlmAgent, RunConfig
from google.adk.agents.run_config import StreamingMode
from google.adk.events import Event
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .config import get_settings

log = logging.getLogger("setu")

# What iter_turn yields, as (kind, text) pairs.
STATUS = "status"  # a progress line for the UI, never stored
DELTA = "delta"    # a piece of the answer as it's generated (streaming only)
FINAL = "final"    # the complete answer, exactly once, last


def _unescape_literal_newlines(value):
    """Gemini's JSON output sometimes double-escapes the backslash in a
    multi-line field, e.g. sends the four characters `\\\\n` where valid JSON
    needs `\\n` to decode to one real newline. json.loads then hands back a
    string containing the literal two characters backslash-n instead of an
    actual newline byte, which renders as a visible "\\n" in the UI. Fix it
    up after parsing rather than trying to prompt the model out of it.
    """
    if isinstance(value, str):
        return value.replace("\\n", "\n").replace("\\t", "\t")
    if isinstance(value, list):
        return [_unescape_literal_newlines(v) for v in value]
    if isinstance(value, dict):
        return {k: _unescape_literal_newlines(v) for k, v in value.items()}
    return value


def parse_json(final_text: str, label: str):
    """An agent's `output_schema` answer as Python data, or None if the model
    did not produce parseable JSON. `label` names the call in the log.

    Shared by every agent that asks for structured output, so they all treat
    an unparseable answer the same way: return None and let the caller decide
    whether that is recoverable.
    """
    try:
        parsed = json.loads(final_text)
    except (json.JSONDecodeError, TypeError):
        log.warning("adk_runner: could not parse %s output as JSON: %r",
                    label, final_text[:500])
        return None
    return _unescape_literal_newlines(parsed)


def build_model() -> Gemini:
    """The model every agent in this app runs on: a Google Gemini model
    through ADK's native Gemini integration (Google AI Studio's Generative
    Language API).

    Native rather than LiteLlm(model="gemini/...") on ADK's own recommendation
    -- it warns that Gemini via LiteLLM gives worse performance and reliability
    and lags on features. The structured-output + MCP-tools combination
    app/business_agent.py and app/chat_agent.py rely on (output_schema kept
    alongside tools) is supported here just as it was on the LiteLlm path.

    The key is passed explicitly via client_kwargs when set; left blank, the
    underlying google-genai client reads GEMINI_API_KEY from the environment
    instead, and if there is none the call fails at request time with a clear
    auth error -- this feature is opt-in, so the app still starts without it.
    """
    settings = get_settings()
    client_kwargs = (
        {"api_key": settings.gemini_api_key} if settings.gemini_api_key else None
    )
    # Ride through the transient upstream blips a long tool loop is exposed to:
    # a single 503 ("model experiencing high demand") or 429 on call number 15
    # would otherwise throw away the whole multi-minute investigation. Retry
    # those with exponential backoff instead of failing the turn.
    retry_options = types.HttpRetryOptions(
        attempts=5, http_status_codes=[429, 500, 502, 503, 504],
    )
    return Gemini(model=settings.gemini_model, client_kwargs=client_kwargs,
                  retry_options=retry_options)


def build_generate_config(*, show_thinking: bool = False,
                          ) -> types.GenerateContentConfig:
    """Generation settings shared by the repo-reading agents.

    temperature=0 makes tool use and grounding deterministic: the model picks
    the obvious next file to read instead of sampling a creative detour, and
    it is far less likely to invent behaviour the code doesn't show. For an
    agent whose whole job is "answer only from what the repository actually
    says," low-temperature is the correct default, not a stylistic one.

    `show_thinking` asks the model for thought summaries at
    GEMINI_THINKING_LEVEL, for agents whose progress is shown to the user.
    Thought parts are kept out of every answer this module returns.
    """
    config = types.GenerateContentConfig(temperature=0.0)
    if show_thinking:
        level = get_settings().gemini_thinking_level
        config.thinking_config = types.ThinkingConfig(
            include_thoughts=True, thinking_level=level or None,
        )
    return config


# --- progress lines ----------------------------------------------------------

# A thought summary opens each idea with a bold heading, e.g.
# "**Analyzing Refund Policies**\n\nI've been looking at..." -- the heading
# alone is the progress line.
_THOUGHT_HEADING = re.compile(r"\*\*(.+?)\*\*")
# Without a heading, the thought's first sentence stands in for one.
_FIRST_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n")
# Anything path- or file-shaped: src/app/Foo.java, context.yaml, owner/repo --
# or a code identifier: RequisitionProjection, findByPin, approver_pin.
_PATHISH = re.compile(
    r"[\w.-]*/[\w./-]+|\b[\w-]+\.[A-Za-z]{1,5}\b"
    r"|\b[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]+)+\b"
    r"|\b[a-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+\b"
    r"|\b[a-z][a-z0-9]*(?:_[a-z0-9]+)+\b"
)
# GitHub search qualifiers (repo:acme/api, path:src, language:java).
_SEARCH_QUALIFIER = re.compile(r"\b\w+:\S+")
_MAX_STEP_CHARS = 90


def _words(name: str) -> str:
    """RequisitionService.java -> "requisition service"."""
    stem = name.rsplit("/", 1)[-1].split(".", 1)[0]
    spaced = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", stem)
    return " ".join(re.split(r"[\s_-]+", spaced)).strip().lower()


def _clip(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _MAX_STEP_CHARS:
        return text
    return text[:_MAX_STEP_CHARS].rsplit(" ", 1)[0] + "…"


def _plain(text: str) -> str:
    """Strip code formatting and turn file paths into plain words."""
    text = text.replace("`", "")
    return _clip(_PATHISH.sub(lambda m: _words(m.group(0)), text))


def _sentence_case(heading: str) -> str:
    """"Examining Requisition Features" -> "Examining requisition features",
    to read like the tool steps around it. Acronyms (BRAC, API) are kept."""
    first, *rest = heading.split(" ")
    return " ".join([first] + [
        w.lower() if w[:1].isupper() and w[1:].islower() else w for w in rest
    ])


def _step_for_call(call: types.FunctionCall) -> str:
    """A tool call as the BA should read it: what's being looked at, never
    which tool, and never a path."""
    name = call.name or ""
    args = call.args or {}
    if name == "get_file_contents":
        path = str(args.get("path") or "").strip("/")
        filename = path.rsplit("/", 1)[-1]
        if path.endswith(".agent/context.yaml"):
            return "Reading the project's feature map"
        if filename.lower().startswith("readme"):
            return "Reading the project overview"
        if "." not in filename:
            return "Browsing the project files"
        return _clip(f"Reading {_words(filename)}")
    if name == "search_code":
        terms = _SEARCH_QUALIFIER.sub("", str(args.get("query") or ""))
        terms = " ".join(terms.replace('"', "").split())
        return _clip(f"Searching the code for “{terms}”") if terms \
            else "Searching the code"
    if name == "set_model_response":  # ADK's tool for an output_schema answer
        return "Writing up the findings"
    if "commit" in name:
        return "Checking recent changes"
    if "pull_request" in name:
        return "Looking through past change requests"
    if "issue" in name:
        return "Looking through reported issues"
    if any(word in name for word in ("branch", "tag", "release")):
        return "Checking the project's versions"
    return "Looking into the project"


class _Progress:
    """Turns a run's events into progress lines, each reported once.

    Streamed thought text arrives in pieces (partial events) and is then
    repeated whole in one aggregated event, so headings are matched against
    the text so far and de-duplicated. Tool calls are only read from complete
    events, once their arguments are final.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._last = ""
        self._thought = ""

    def read(self, event: Event) -> list[str]:
        parts = event.content.parts
        found: list[str] = []

        thought = "".join(p.text or "" for p in parts if p.thought)
        if thought:
            if event.partial:
                self._thought += thought
                thought = self._thought
            else:
                self._thought = ""
            headings = _THOUGHT_HEADING.findall(thought)
            if not headings and not event.partial:
                headings = [_FIRST_SENTENCE.split(thought.strip(), 1)[0]]
            found += [_sentence_case(_plain(h)) for h in headings]

        if not event.partial:
            found += [_step_for_call(p.function_call)
                      for p in parts if p.function_call]

        fresh = []
        for step in found:
            if step and step not in self._seen and step != self._last:
                self._seen.add(step)
                self._last = step
                fresh.append(step)
        return fresh


# --- running a turn ----------------------------------------------------------

def _build_runner(agent: LlmAgent, *, app_name: str) -> Runner:
    return Runner(app_name=app_name, agent=agent,
                  session_service=InMemorySessionService())


def _answer_text(parts: list[types.Part]) -> str:
    """The answer's own text, without the model's thought summaries."""
    return "".join(p.text or "" for p in parts if not p.thought)


async def iter_turn(agent: LlmAgent, prompt: str, *, app_name: str,
                    user_id: str, streaming: bool = False,
                    ) -> AsyncIterator[tuple[str, str]]:
    """Yield (STATUS, line) progress lines while the agent works, then exactly
    one (FINAL, answer) once the turn completes.

    With `streaming`, also yield (DELTA, text) pieces of the answer as they
    are generated, via RunConfig(streaming_mode=StreamingMode.SSE): the runner
    yields event.partial=True chunks for the typewriter effect plus one final
    event.partial=False event with the complete text -- both documented
    directly on google.adk.agents.run_config.StreamingMode.SSE. Text in an
    event that also calls a tool is the model thinking aloud before the call,
    not part of the answer, and is skipped.
    """
    runner = _build_runner(agent, app_name=app_name)
    session = await runner.session_service.create_session(
        app_name=app_name, user_id=user_id)

    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    run_config = RunConfig(
        streaming_mode=StreamingMode.SSE if streaming else StreamingMode.NONE)
    progress = _Progress()
    final_text = ""
    async for event in runner.run_async(
        user_id=user_id, session_id=session.id, new_message=content,
        run_config=run_config,
    ):
        if not event.content or not event.content.parts:
            continue
        for step in progress.read(event):
            yield STATUS, step

        parts = event.content.parts
        if any(p.function_call for p in parts):
            continue
        text = _answer_text(parts)
        if not text:
            continue
        if event.partial:
            yield DELTA, text
        elif event.is_final_response():
            final_text = text

    yield FINAL, final_text


async def run_single_turn(agent: LlmAgent, prompt: str, *,
                          app_name: str, user_id: str) -> str:
    """The answer alone, for callers that don't show progress."""
    final_text = ""
    async for kind, text in iter_turn(agent, prompt, app_name=app_name,
                                      user_id=user_id):
        if kind == FINAL:
            final_text = text
    return final_text


async def stream_single_turn(agent: LlmAgent, prompt: str, *,
                             app_name: str, user_id: str,
                             ) -> AsyncIterator[tuple[str, str]]:
    """iter_turn with the answer streamed: STATUS, DELTA and FINAL pairs."""
    async for kind, text in iter_turn(agent, prompt, app_name=app_name,
                                      user_id=user_id, streaming=True):
        yield kind, text

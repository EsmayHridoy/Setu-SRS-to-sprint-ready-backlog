"""Run one turn against an ADK agent, either all at once or streamed.

Shared by every agent call in this app (app/github_agent.py, app/business_agent.py,
app/chat_agent.py): build a throwaway session, run the agent once, pull text out
of the event stream. None of them need conversation history to persist across
calls, so a fresh InMemorySessionService per call is enough.
"""
from __future__ import annotations

from collections.abc import AsyncIterator

from google.adk.agents import LlmAgent, RunConfig
from google.adk.agents.run_config import StreamingMode
from google.adk.models import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .config import get_settings


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


def build_generate_config() -> types.GenerateContentConfig:
    """Generation settings shared by the repo-reading agents.

    temperature=0 makes tool use and grounding deterministic: the model picks
    the obvious next file to read instead of sampling a creative detour, and
    it is far less likely to invent behaviour the code doesn't show. For an
    agent whose whole job is "answer only from what the repository actually
    says," low-temperature is the correct default, not a stylistic one.
    """
    return types.GenerateContentConfig(temperature=0.0)


def _build_runner(agent: LlmAgent, *, app_name: str) -> Runner:
    return Runner(app_name=app_name, agent=agent,
                  session_service=InMemorySessionService())


async def run_single_turn(agent: LlmAgent, prompt: str, *,
                          app_name: str, user_id: str) -> str:
    runner = _build_runner(agent, app_name=app_name)
    session = await runner.session_service.create_session(
        app_name=app_name, user_id=user_id)

    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    final_text = ""
    async for event in runner.run_async(
        user_id=user_id, session_id=session.id, new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(part.text or "" for part in event.content.parts)

    return final_text


async def stream_single_turn(agent: LlmAgent, prompt: str, *,
                             app_name: str, user_id: str,
                             ) -> AsyncIterator[tuple[bool, str]]:
    """Yield (is_final, text) pairs: partial text deltas as they're generated
    (is_final=False), then exactly one aggregated full-text event
    (is_final=True) once the turn completes.

    Uses RunConfig(streaming_mode=StreamingMode.SSE), which makes the runner
    yield event.partial=True chunks for the typewriter effect plus one final
    event.partial=False event with the complete text -- both documented
    directly on google.adk.agents.run_config.StreamingMode.SSE. Partial
    function-call-argument events (tool calls in progress) are skipped; only
    text-carrying events are yielded.
    """
    runner = _build_runner(agent, app_name=app_name)
    session = await runner.session_service.create_session(
        app_name=app_name, user_id=user_id)

    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    run_config = RunConfig(streaming_mode=StreamingMode.SSE)
    async for event in runner.run_async(
        user_id=user_id, session_id=session.id, new_message=content,
        run_config=run_config,
    ):
        if not event.content or not event.content.parts:
            continue
        parts = event.content.parts
        has_function_call = any(p.function_call for p in parts)
        if has_function_call:
            continue
        text = "".join(p.text or "" for p in parts)
        if not text:
            continue
        if event.partial:
            yield False, text
        elif event.is_final_response():
            yield True, text

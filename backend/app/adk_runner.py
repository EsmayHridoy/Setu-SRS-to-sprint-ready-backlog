"""Run a turn against an ADK agent, either all at once or streamed.

Two shapes, because this app has two genuinely different kinds of agent call:

  * run_single_turn / stream_single_turn -- one-shot. Build a throwaway
    session, run the agent once, drop it. Correct for app/github_agent.py and
    app/business_agent.py: vetting a requirement or answering one GitHub
    question has no history to carry, and giving those calls a durable
    session would only leave rows nothing reads.

  * run_turn / stream_turn -- conversational. The caller supplies a Runner it
    owns (with a persistent session service) and the id of an existing
    session, so every turn appends to the same history and the model sees the
    conversation it is in. Used by app/chat_agent.py via app/chat_runtime.py.

Both share the same event-consumption logic below; only session lifetime and
runner ownership differ.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

import httpx
from google.adk.agents import LlmAgent, RunConfig
from google.adk.agents.run_config import StreamingMode
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .config import get_settings


@lru_cache(maxsize=8)
def _model_supports_thinking(model: str, api_base: str) -> bool:
    """Whether Ollama reports "thinking" in this model's own capabilities.

    Models vary (qwen3.x supports it, devstral doesn't -- Ollama rejects the
    whole request outright if you pass `think` to a model that doesn't
    support it), so this is checked rather than assumed. Cached per
    (model, api_base) since it never changes within a run and this app
    switches OLLAMA_MODEL often during local testing.
    """
    try:
        resp = httpx.post(f"{api_base}/api/show", json={"model": model}, timeout=5.0)
        resp.raise_for_status()
        return "thinking" in resp.json().get("capabilities", [])
    except Exception:  # noqa: BLE001 - Ollama unreachable etc.; don't risk an
        return False   # unsupported param on a guess, just skip reasoning_effort


def build_model() -> LiteLlm:
    """The model every agent in this app runs on: a local Ollama model via
    LiteLLM's "ollama_chat" provider (the chat-completions endpoint, which
    supports tools and response_format -- the legacy "ollama" provider using
    /api/generate does not).

    LiteLlm's capabilities declare output_schema_and_tools=True, and
    litellm's ollama_chat transformer supports both `tools` and
    `response_format` (including json_schema), so the structured-output +
    MCP-tools combination app/business_agent.py and app/chat_agent.py rely
    on should work the same way it did on native Gemini.

    reasoning_effort maps to Ollama's own `think` parameter (any of
    "low"/"medium"/"high" just turns it on) -- only passed when the current
    model actually supports it (see _model_supports_thinking). Without it,
    a thinking-capable model like qwen3.5/qwen3.6 writes its reasoning
    inline as ordinary text with no way to separate it back out; with it,
    Ollama returns reasoning in its own `thinking` field, which ADK's
    LiteLlm wrapper keeps out of the final answer text on its own.
    """
    settings = get_settings()
    kwargs = {"model": f"ollama_chat/{settings.ollama_model}",
             "api_base": settings.ollama_api_base}
    if _model_supports_thinking(settings.ollama_model, settings.ollama_api_base):
        kwargs["reasoning_effort"] = "low"
    return LiteLlm(**kwargs)


def _build_runner(agent: LlmAgent, *, app_name: str) -> Runner:
    """A runner whose session store dies with the call. One-shot agents only."""
    return Runner(app_name=app_name, agent=agent,
                  session_service=InMemorySessionService())


async def run_turn(runner: Runner, prompt: str, *, user_id: str,
                   session_id: str) -> str:
    """Run one turn on an existing session and return its final text.

    The runner appends this turn's events (the user message, any tool calls
    and their results, the model's reply) to that session itself, and replays
    them on the next turn -- which is the whole reason a conversation needs a
    session that outlives the request.
    """
    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    final_text = ""
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            final_text = "".join(part.text or "" for part in event.content.parts)

    return final_text


async def stream_turn(runner: Runner, prompt: str, *, user_id: str,
                      session_id: str) -> AsyncIterator[tuple[bool, str]]:
    """Yield (is_final, text) pairs for one turn on an existing session:
    partial text deltas as they're generated (is_final=False), then exactly
    one aggregated full-text event (is_final=True) once the turn completes.

    Uses RunConfig(streaming_mode=StreamingMode.SSE), which makes the runner
    yield event.partial=True chunks for the typewriter effect plus one final
    event.partial=False event with the complete text -- both documented
    directly on google.adk.agents.run_config.StreamingMode.SSE. Partial
    function-call-argument events (tool calls in progress) are skipped; only
    text-carrying events are yielded.
    """
    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    run_config = RunConfig(streaming_mode=StreamingMode.SSE)
    async for event in runner.run_async(
        user_id=user_id, session_id=session_id, new_message=content,
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


async def run_single_turn(agent: LlmAgent, prompt: str, *,
                          app_name: str, user_id: str) -> str:
    """One-shot: throwaway session, one turn, no history kept."""
    runner = _build_runner(agent, app_name=app_name)
    session = await runner.session_service.create_session(
        app_name=app_name, user_id=user_id)
    return await run_turn(runner, prompt, user_id=user_id,
                          session_id=session.id)


async def stream_single_turn(agent: LlmAgent, prompt: str, *,
                             app_name: str, user_id: str,
                             ) -> AsyncIterator[tuple[bool, str]]:
    """One-shot, streamed. Same (is_final, text) contract as stream_turn."""
    runner = _build_runner(agent, app_name=app_name)
    session = await runner.session_service.create_session(
        app_name=app_name, user_id=user_id)
    async for pair in stream_turn(runner, prompt, user_id=user_id,
                                  session_id=session.id):
        yield pair

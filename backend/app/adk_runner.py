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
from google.adk.models.lite_llm import LiteLlm
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from .config import get_settings


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

    reasoning_effort maps to Ollama's own `think` parameter for non-gpt-oss
    models (any of "low"/"medium"/"high" just turns it on). Without it,
    qwen3.5/qwen3.6 write their reasoning inline as ordinary text with no way
    to separate it back out; with it, Ollama returns reasoning in its own
    `thinking` field, which ADK's LiteLlm wrapper keeps out of the final
    answer text on its own.
    """
    settings = get_settings()
    return LiteLlm(model=f"ollama_chat/{settings.ollama_model}",
                   api_base=settings.ollama_api_base,
                   reasoning_effort="low")


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

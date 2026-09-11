"""Conversation memory for the agents.

Every agent call runs on a throwaway ADK session (see app/adk_runner.py), so
nothing carries over from one turn to the next on its own. Instead the
history is rebuilt from the messages table on every turn and folded into the
prompt: the database is already the record of the conversation, it survives
a server restart, and this keeps working whatever ADK does with its session
internals between versions.

A document uploaded in chat is remembered through its business plan rather
than through the reply's text, so a later turn sees the plan as it stands
*now* -- what was extracted, whether the user confirmed or discarded it, and
each item's vetting verdict -- not the draft as it was first presented.
"""
from __future__ import annotations

from collections.abc import Iterable

from .models import BusinessPlan, Message

# Room for roughly the last dozen exchanges. Oldest turns are dropped first.
MAX_HISTORY_CHARS = 24_000
# One long message (an old extracted-text reply, say) must not crowd out the rest.
MAX_MESSAGE_CHARS = 4_000
MAX_NOTE_CHARS = 500

_ROLE_LABELS = {"USER": "User", "ASSISTANT": "Assistant"}

_PLAN_STATUS = {
    "DRAFT": "extracted, waiting for the user to confirm",
    "CONFIRMED": "confirmed by the user, vetting in progress",
    "DONE": "confirmed by the user and vetted against the codebase",
    "DISCARDED": "discarded by the user, not vetted",
}


def _clip(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " …"


def _yes_no(value: bool | None) -> str:
    return "unknown" if value is None else ("yes" if value else "no")


def render_plan(plan: BusinessPlan) -> str:
    """The plan as plain text: every item, and its vetting result if it has
    one -- in BRAC IT's own Change Request / Story template vocabulary
    (User Story, Actors, Pre-condition, Impacted Areas, Requirements,
    Acceptance Criteria, Exceptions, Verdict).
    """
    status = _PLAN_STATUS.get(plan.status, plan.status.lower())
    lines = [f"[Business requirements from {plan.source_filename} -- {status}]"]
    if not plan.items:
        lines.append("(no items)")
    for item in plan.items:
        where = f" ({item.location})" if item.location else ""
        lines.append(f"{item.seq_no}. {item.description}{where}")
        if item.vetting_status == "DONE":
            lines.append(f"   Verdict: {_clip(item.verdict, MAX_NOTE_CHARS)}")
            lines.append(
                f"   Requirement clear: {_yes_no(item.is_requirement_clear)}; "
                f"feasible: {_yes_no(item.is_feasible)}; "
                f"already supported: {_yes_no(item.already_supported)}"
            )
            lines.append(f"   User Story: {_clip(item.user_story, MAX_NOTE_CHARS)}")
            lines.append(f"   Actors: {_clip(item.actors, MAX_NOTE_CHARS)}")
            lines.append(
                f"   Pre-condition: {_clip(item.pre_condition, MAX_NOTE_CHARS)}"
            )
            lines.append(
                f"   Impacted Areas: {_clip(item.impacted_areas, MAX_NOTE_CHARS)}"
            )
            lines.append(
                f"   Requirements: {_clip(item.requirements, MAX_NOTE_CHARS)}"
            )
            lines.append(
                f"   Acceptance Criteria: "
                f"{_clip(item.acceptance_criteria, MAX_NOTE_CHARS)}"
            )
            lines.append(f"   Exceptions: {_clip(item.exceptions, MAX_NOTE_CHARS)}")
        elif item.vetting_status == "ERROR":
            lines.append("   Vetting failed for this item.")
    return "\n".join(lines)


def _render(message: Message) -> str:
    if message.business_plan is not None:
        return render_plan(message.business_plan)
    return _clip(message.content, MAX_MESSAGE_CHARS)


def build_history(messages: Iterable[Message], *,
                  budget: int = MAX_HISTORY_CHARS) -> str:
    """The conversation so far as a "User: / Assistant:" transcript, newest
    turns kept first when it has to be cut. Placeholder replies are skipped:
    they are canned text, not something the agent said.
    """
    turns: list[str] = []
    used = 0
    omitted = False
    for message in reversed(list(messages)):
        label = _ROLE_LABELS.get(message.role)
        if label is None or message.is_placeholder:
            continue
        text = _render(message)
        if not text:
            continue
        turn = f"{label}: {text}"
        if used + len(turn) > budget:
            omitted = True
            break
        turns.append(turn)
        used += len(turn)

    turns.reverse()
    if omitted:
        turns.insert(0, "(Earlier messages omitted.)")
    return "\n\n".join(turns)


def with_history(history: str, prompt: str) -> str:
    """Fold a transcript from build_history() in ahead of the current prompt."""
    if not history:
        return prompt
    return (
        "Conversation so far, oldest first:\n\n"
        f"{history}\n\n"
        "---\n\n"
        f"Current message:\n{prompt}"
    )

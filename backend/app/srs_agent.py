"""The Gemini-backed agent that decides which SRS section each story goes in.

The user's SRS format is their own: its sections may be numbered or not,
called "Functional Requirements" or "System Features" or "Business Rules",
nested three deep or flat. Rather than pattern-match heading names, the
template's heading outline and the vetted stories are handed to the model
together and it returns one section per story.

It is given only the outline and the stories -- no repository access, no
tools -- so this is a single cheap turn, unlike the per-item vetting in
app/business_agent.py.

Choosing nothing is a valid answer: a story that fits no section in the
format comes back with section_index -1, and app/srs_docx.py appends those
under their own heading rather than forcing them somewhere wrong.
"""
from __future__ import annotations

import logging

from google.adk.agents import LlmAgent
from pydantic import BaseModel

from .adk_runner import (
    build_generate_config, build_model, parse_json, run_single_turn,
)
from .srs_docx import FIELD_LABELS, Section, Story

log = logging.getLogger("setu")

APP_NAME = "setu-srs-agent"

_INSTRUCTION = (
    "You are a business analyst filing vetted requirements into a client's "
    "own Software Requirements Specification template.\n\n"
    "You are given SECTIONS -- the template's heading outline, each line "
    "numbered with the index you must refer to it by -- and STORIES, the "
    "vetted requirements to file.\n\n"
    "For every story, choose the ONE section it belongs in and return its "
    "index. Rules:\n"
    "- Choose the most specific section that fits. If a story belongs under "
    "a sub-section (e.g. `3.2 Payment Processing` rather than `3 Functional "
    "Requirements`), pick the sub-section.\n"
    "- Never choose a front-matter or administrative section -- table of "
    "contents, revision history, document conventions, references, "
    "glossary, approval or sign-off -- even if nothing else seems to fit.\n"
    "- Prefer a section describing system behaviour or features for a "
    "functional requirement, and a non-functional/quality section for a "
    "performance, security, availability or usability requirement.\n"
    "- If a story genuinely fits none of the sections offered, return "
    "section_index -1 for it. Do not force a poor match.\n"
    "- Return exactly one entry for every story, using the story's own "
    "seq_no. Do not invent seq_no or section_index values that were not "
    "given to you.\n\n"
    "Keep `reason` to a short phrase naming what made the section right; it "
    "is for the analyst's audit trail, not the document."
)


class SectionChoice(BaseModel):
    seq_no: int
    section_index: int
    reason: str = ""


def _prompt(sections: list[Section], stories: list[Story]) -> str:
    lines = ["SECTIONS", ""]
    for section in sections:
        # The level is worth showing: it is how the model can tell a
        # sub-section from the top-level heading above it.
        lines.append(f"[{section.index}] (level {section.level}) {section.text}")
    lines += ["", "STORIES", ""]
    for story in stories:
        lines.append(f"seq_no {story.seq_no}: {story.title}")
        for key, value in story.fields:
            if value.strip():
                # One line each: the model is choosing a section, not reading
                # the story in full, and a long Requirements list adds little.
                label = FIELD_LABELS.get(key, key)
                lines.append(f"  {label}: {' '.join(value.split())[:400]}")
        lines.append("")
    return "\n".join(lines)


async def assign_sections(sections: list[Section], stories: list[Story], *,
                          user_id: str) -> dict[int, int]:
    """Map each story's seq_no to the index of the section it belongs in.

    Only entries the model returned that name a real story and a real section
    are kept: anything else is left out, and app/srs_docx.py falls back for
    the stories that are missing. Returns {} if the model produced nothing
    usable, which is a degraded result rather than a failure -- every story
    still gets written, just into the fallback section.
    """
    if not sections or not stories:
        return {}

    agent = LlmAgent(
        model=build_model(),
        name="srs_section_matcher",
        instruction=_INSTRUCTION,
        output_schema=list[SectionChoice],
        generate_content_config=build_generate_config(),
    )
    final_text = await run_single_turn(
        agent, _prompt(sections, stories), app_name=APP_NAME, user_id=user_id,
    )

    parsed = parse_json(final_text, "srs section matching")
    if not isinstance(parsed, list):
        return {}

    known_seq = {story.seq_no for story in stories}
    assignments: dict[int, int] = {}
    for entry in parsed:
        try:
            choice = SectionChoice.model_validate(entry)
        except Exception:  # noqa: BLE001 - skip one bad entry, keep the rest
            log.warning("srs_agent: skipping unparseable section choice: %r", entry)
            continue
        if choice.seq_no not in known_seq:
            continue
        # -1 is the documented "fits nowhere"; any other out-of-range index is
        # the model having hallucinated one, and both mean "use the fallback".
        if not 0 <= choice.section_index < len(sections):
            continue
        assignments[choice.seq_no] = choice.section_index
    return assignments

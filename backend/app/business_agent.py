"""Two Gemini-backed ADK agents for the business-plan workflow.

`extract_businesses` reads an uploaded document's text and pulls out a
numbered list of discrete business requirements, each tagged with where in
the document it came from. `vet_business` takes one of those and checks it
against the live GitHub repo (through the same GITHUB_PAT-authenticated MCP
connection app/github_agent.py uses) on the two criteria the review process
cares about: is this a change to an existing business, and if so is it
feasible; and does it impact other related features. It also measures how
similar the requirement is to the closest existing feature, as a percentage
backed by a rule-by-rule MATCHES / DIFFERS / MISSING list.

Both rely on the installed google-adk's documented support for combining
`output_schema` with `tools` -- tools stay available during the reasoning
loop, structure is enforced only on the final answer -- so the result is
parsed straight out of JSON rather than scraped from free text.
"""
from __future__ import annotations

import json
import logging

from google.adk.agents import LlmAgent
from pydantic import BaseModel, Field, field_validator

from . import github_mcp
from .adk_runner import build_generate_config, build_model, run_single_turn
from .chat_memory import with_history

log = logging.getLogger("setu")

APP_NAME = "setu-business-agent"

_EXTRACTION_INSTRUCTION = (
    "You are extracting business requirements from a document for a "
    "business analyst to review. The document is given to you as labelled "
    "chunks, each starting with its location in brackets, e.g. [Page 3] or "
    "[Section: Refund Policy].\n\n"
    "Pull out every distinct, actionable business requirement or rule -- "
    "each as one short, standalone sentence a business analyst would "
    "recognise as a single reviewable item. Do not merge unrelated "
    "requirements together, and do not invent anything that isn't stated or "
    "clearly implied in the text. For each one, set `location` to the label "
    "of the chunk it came from, exactly as it appears inside the brackets "
    "but without the brackets themselves (e.g. `Page 3`, not `[Page 3]`). "
    "If nothing in the document reads as a business requirement, return an "
    "empty list."
)

_VETTING_INSTRUCTION_TEMPLATE = (
    "You are vetting one proposed business requirement against the actual "
    "code in the repository. {repo_hint} {tool_list_hint}\n\n"
    "{procedure}\n\n"
    "First understand the current business the relevant code implements "
    "today. before anything else: read `.agent/context.yaml` then check desired files to answer. "
    "Then answer two questions about the proposed requirement (stated the way a BA or client "
    "would say them, not in technical terms) : \n\n"
    "1. Is this a change to an existing business rule/feature already "
    "implemented in the repository? Work this out carefully by measuring how "
    "similar the requirement is to the closest existing feature (see "
    "'Similarity' below). If yes, is it feasible to incorporate "
    "into the system as it exists today, and does it fit how the current "
    "business works (set `change_feasible`; leave it null if this isn't an "
    "existing-business change)? Put the reasoning in `feasibility_notes`.\n"
    "2. Does this business impact other features related to it elsewhere in "
    "the codebase? Name where the impact lands in `impact_notes`.\n\n"
    "Similarity -- how much of the requested behaviour the closest existing "
    "feature already does. Measure it, don't estimate it from names:\n"
    "- Set `similar_feature` to that closest feature: its name as the code "
    "or `.agent/context.yaml` calls it, then its main files, e.g. `Leave "
    "approval (app/leave/approval.py, app/leave/rules.py)`. Leave it empty "
    "if nothing related exists.\n"
    "- Break the requirement into its individual rules and behaviours. "
    "Check each one against the code you actually read and mark it "
    "MATCHES (the code already does it), DIFFERS (the code does something "
    "related but not this) or MISSING (nothing does it). List every one in "
    "`similarity_notes`, one per line, with the file or function you checked, "
    "e.g. `MATCHES: manager approves the request -- approve() in "
    "app/leave/approval.py`.\n"
    "- Set `similarity_percent` (a whole number, 0-100) from that list -- "
    "the share of the requirement the existing code already satisfies, "
    "counting a DIFFERS as partial. Use this scale so the number means the "
    "same thing every time:\n"
    "  0: you searched and nothing related exists; this is wholly new.\n"
    "  1-25: the area exists (same module or entities), but none of the "
    "requested behaviour.\n"
    "  26-50: some of the behaviour exists; most is missing or works "
    "differently.\n"
    "  51-75: most of it exists; some rules are missing or differ.\n"
    "  76-99: nearly all of it exists; only a small detail differs (a limit, "
    "a threshold, a label).\n"
    "  100: already implemented exactly as asked; nothing to change -- say "
    "so in `verdict`.\n"
    "- If you could not find or read the relevant code, set "
    "`similarity_percent` to null and say what you could not check in "
    "`similarity_notes`. Never guess a number you did not measure.\n"
    "- Keep it consistent with question 1: at 0 this cannot be a change to "
    "an existing business.\n\n"
    "Ground every claim in what you actually found through the tools -- name "
    "the specific file, function or issue/PR you looked at in your notes. "
    "Never claim to have made a change; you are only investigating. "
    "`verdict` should be one short sentence summarising your conclusion.\n\n"
    "The message may open with the conversation this requirement came from, "
    "including the other requirements extracted from the same document. Use "
    "it only as background -- clarifications the user gave, or how this "
    "requirement relates to its siblings -- and vet only the one requirement "
    "under 'Current message'."
)


class ExtractedBusiness(BaseModel):
    description: str
    location: str


class BusinessVetting(BaseModel):
    is_existing_business_change: bool
    change_feasible: bool | None = None
    feasibility_notes: str
    impacts_other_features: bool
    impact_notes: str
    # How much of the requirement the closest existing feature already does;
    # null when the agent couldn't read the code to measure it. Required (no
    # default) so the model must always answer, if only with an explicit null.
    similarity_percent: int | None = Field(ge=0, le=100)
    similar_feature: str
    similarity_notes: str
    verdict: str

    @field_validator("similarity_percent", mode="before")
    @classmethod
    def _clamp_percent(cls, value):
        """Accept 72, 72.4 or "72%", and pull an out-of-range number back to
        0-100 rather than failing the whole item over it."""
        if value is None or value == "":
            return None
        if isinstance(value, str):
            value = value.strip().rstrip("%").strip()
        try:
            return max(0, min(100, round(float(value))))
        except (TypeError, ValueError):
            return None


def _parse_json(final_text: str, label: str):
    try:
        return json.loads(final_text)
    except (json.JSONDecodeError, TypeError):
        log.warning("business_agent: could not parse %s output as JSON: %r",
                   label, final_text[:500])
        return None


async def extract_businesses(chunks: list[tuple[str, str]], *, user_id: str,
                             guidance: str = "") -> list[ExtractedBusiness]:
    """Pull a numbered list of business requirements out of a document.

    `guidance` is whatever the user typed alongside the upload ("only the
    refund rules", say); it steers the extraction but never overrides the
    rule against inventing requirements.

    Returns [] if the model produced nothing usable -- the human reviewing
    the draft can add items by hand before confirming, so this is never a
    hard failure.
    """
    if not chunks:
        return []

    prompt = "\n\n".join(f"[{label}]\n{text}" for label, text in chunks)
    if guidance.strip():
        prompt = (f"The user's note about this document: {guidance.strip()}\n\n"
                  f"{prompt}")
    agent = LlmAgent(
        model=build_model(),
        name="business_extractor",
        instruction=_EXTRACTION_INSTRUCTION,
        output_schema=list[ExtractedBusiness],
        generate_content_config=build_generate_config(),
    )
    final_text = await run_single_turn(
        agent, prompt, app_name=APP_NAME, user_id=user_id,
    )
    parsed = _parse_json(final_text, "extraction")
    if not isinstance(parsed, list):
        return []

    businesses = []
    for item in parsed:
        try:
            businesses.append(ExtractedBusiness.model_validate(item))
        except Exception:  # noqa: BLE001 - skip one bad item, keep the rest
            log.warning("business_agent: skipping unparseable business item: %r", item)
    return businesses


async def vet_business(description: str, *, user_id: str,
                       history: str = "") -> BusinessVetting:
    """Vet one business requirement against the live repo. Raises RuntimeError
    if GITHUB_PAT isn't configured; the caller is responsible
    for turning a per-item failure into a stored ERROR row instead of letting
    it abort the whole vetting stream.

    `history` is the conversation the plan was uploaded in, from
    chat_memory.build_history(); blank for a plan created outside chat.
    """
    github_mcp.require_configured()

    toolset = github_mcp.build_toolset()
    agent = LlmAgent(
        model=build_model(),
        name="business_vetter",
        instruction=_VETTING_INSTRUCTION_TEMPLATE.format(
            repo_hint=github_mcp.repo_hint(),
            tool_list_hint=github_mcp.tool_list_hint(),
            procedure=github_mcp.investigation_procedure(),
        ),
        tools=[toolset],
        output_schema=BusinessVetting,
        generate_content_config=build_generate_config(),
    )
    try:
        final_text = await run_single_turn(
            agent, with_history(history, description),
            app_name=APP_NAME, user_id=user_id,
        )
    finally:
        await toolset.close()

    parsed = _parse_json(final_text, "vetting")
    if parsed is None:
        raise RuntimeError(
            "The vetting agent's response could not be parsed as structured "
            "output."
        )
    return BusinessVetting.model_validate(parsed)

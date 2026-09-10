"""Two Gemini-backed ADK agents for the business-plan workflow.

`extract_businesses` reads an uploaded document's text and pulls out a
numbered list of discrete business requirements, each tagged with where in
the document it came from. `vet_business` takes one of those and checks it
against the live GitHub repo (through the same GITHUB_PAT-authenticated MCP
connection app/github_agent.py uses) on the two criteria the review process
cares about: is this a change to an existing business, and if so is it
feasible; and does it impact other related features.

Both rely on the installed google-adk's documented support for combining
`output_schema` with `tools` -- tools stay available during the reasoning
loop, structure is enforced only on the final answer -- so the result is
parsed straight out of JSON rather than scraped from free text.
"""
from __future__ import annotations

import json
import logging

from google.adk.agents import LlmAgent
from pydantic import BaseModel

from . import github_mcp
from .adk_runner import build_generate_config, build_model, run_single_turn

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
    "today. Then answer two questions about the proposed requirement:\n\n"
    "1. Is this a change to an existing business rule/feature already "
    "implemented in the repository? If yes, is it feasible to incorporate "
    "into the system as it exists today, and does it fit how the current "
    "business works (set `change_feasible`; leave it null if this isn't an "
    "existing-business change)? Put the reasoning in `feasibility_notes`.\n"
    "2. Does this business impact other features related to it elsewhere in "
    "the codebase? Name where the impact lands in `impact_notes`.\n\n"
    "Ground every claim in what you actually found through the tools -- name "
    "the specific file, function or issue/PR you looked at in your notes. "
    "Never claim to have made a change; you are only investigating. "
    "`verdict` should be one short sentence summarising your conclusion."
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
    verdict: str


def _parse_json(final_text: str, label: str):
    try:
        return json.loads(final_text)
    except (json.JSONDecodeError, TypeError):
        log.warning("business_agent: could not parse %s output as JSON: %r",
                   label, final_text[:500])
        return None


async def extract_businesses(chunks: list[tuple[str, str]], *,
                             user_id: str) -> list[ExtractedBusiness]:
    """Pull a numbered list of business requirements out of a document.

    Returns [] if the model produced nothing usable -- the human reviewing
    the draft can add items by hand before confirming, so this is never a
    hard failure.
    """
    if not chunks:
        return []

    prompt = "\n\n".join(f"[{label}]\n{text}" for label, text in chunks)
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


async def vet_business(description: str, *, user_id: str) -> BusinessVetting:
    """Vet one business requirement against the live repo. Raises RuntimeError
    if GITHUB_PAT isn't configured; the caller is responsible
    for turning a per-item failure into a stored ERROR row instead of letting
    it abort the whole vetting stream.
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
            agent, description, app_name=APP_NAME, user_id=user_id,
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

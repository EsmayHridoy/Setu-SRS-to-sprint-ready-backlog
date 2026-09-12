"""Two Gemini-backed ADK agents for the business-plan workflow.

`extract_businesses` reads an uploaded document's text and pulls out a
numbered list of discrete business requirements, each tagged with where in
the document it came from. Documents that follow BRAC IT's own Change
Request template already group each requirement as a "Story" -- User Story,
Actors, Pre-condition, Impacted Areas, Requirements, Acceptance Criteria,
Exceptions -- so one Story is kept as one item, verbatim, rather than
compressed into a single sentence. Free-form SRS prose without that
structure is still distilled into one-sentence items as before.

`vet_business_stream` takes one of those and checks it against the live GitHub repo
(through the same GITHUB_PAT-authenticated MCP connection app/github_agent.py
uses), responding in the BA's own template vocabulary -- User Story, Actors,
Pre-condition, Impacted Areas, Requirements, Acceptance Criteria, Exceptions
-- verified and gap-filled against what the repository actually shows, plus
a Verdict the template itself doesn't have a field for but which is the
whole point of vetting.

Both rely on the installed google-adk's documented support for combining
`output_schema` with `tools` -- tools stay available during the reasoning
loop, structure is enforced only on the final answer -- so the result is
parsed straight out of JSON rather than scraped from free text.
"""
from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from google.adk.agents import LlmAgent
from pydantic import BaseModel

from . import github_mcp
from .adk_runner import (
    FINAL, STATUS, build_generate_config, build_model, iter_turn, parse_json,
    run_single_turn,
)
from .chat_memory import with_history

log = logging.getLogger("setu")

APP_NAME = "setu-business-agent"

# The last pair vet_business_stream yields, carrying the BusinessVetting.
RESULT = "result"

_EXTRACTION_INSTRUCTION = (
    "You are extracting business requirements from a document for a "
    "business analyst to review. The document is given to you as labelled "
    "chunks, each starting with its location in brackets, e.g. [Page 3] or "
    "[Section: Refund Policy].\n\n"
    "Some chunks are already a full \"Story\" from a Change Request template: "
    "they contain their own labelled fields such as `User Story:`, `Actors:`, "
    "`Pre-condition:`, `Impacted Areas:`, `Requirements:`, `Acceptance "
    "Criteria:` and/or `Exceptions:`. When a chunk reads this way, treat the "
    "WHOLE chunk as ONE item: copy its full text into `description` exactly "
    "as written, keeping every one of its labelled fields intact -- do not "
    "compress it down to a single sentence, and do not split its "
    "sub-fields (like individual Requirements lines) into separate items. "
    "That one chunk is one story, and one story is one requirement to vet.\n\n"
    "For any other chunk -- ordinary prose that is not structured this way -- "
    "pull out every distinct, actionable business requirement or rule as one "
    "short, standalone sentence a business analyst would recognise as a "
    "single reviewable item.\n\n"
    "In both cases: do not merge unrelated requirements together, and do not "
    "invent anything that isn't stated or clearly implied in the text. "
    "Skip administrative content that is not itself a requirement -- cover "
    "sheets, revision history, sign-off tables, risk-analysis tables, effort "
    "estimation and accumulation tables. Set `location` to the label of the "
    "chunk it came from, exactly as it appears inside the brackets but "
    "without the brackets themselves (e.g. `Page 3`, not `[Page 3]`). If "
    "nothing in the document reads as a business requirement, return an "
    "empty list."
)

_VETTING_INSTRUCTION_TEMPLATE = (
    "You are vetting one proposed business requirement -- a \"Story\" from "
    "BRAC IT's own Change Request template, or an equivalent requirement -- "
    "acting as a Business Analyst and solutions architect. Respond to the BA "
    "using the SAME field vocabulary their own template already uses, so "
    "your answer reads as a filled-in, verified version of the story they "
    "wrote, not a different framework. Write every field in plain business "
    "language. Do NOT put file paths, class or function names, or database "
    "table/column names anywhere in your answer -- investigate the actual "
    "repository to ground yourself, but translate everything you find into "
    "business terms before writing it down. {repo_hint}\n\n"
    "{procedure}\n\n"
    "The requirement you are given may already state some of these fields "
    "(the BA's own draft) or may be a short plain sentence with none of them "
    "-- either way, fill every field below yourself, grounded in what the "
    "repository actually shows:\n\n"
    "`is_requirement_clear` -- is the requirement itself clear enough to "
    "vet, or is it vague/ambiguous enough that the client should be asked to "
    "clarify it first? If it is not clear, still fill the fields below as "
    "best you can from what is given, but say plainly in `verdict` what is "
    "unclear and what should be asked.\n\n"
    "`user_story` -- restate it in the classic form \"As a <actor> I want to "
    "<action> so I can <outcome>\". If the BA already wrote one, verify it "
    "reads correctly and clean it up; if not, write one from the "
    "requirement.\n\n"
    "`actors` -- who initiates this and who else is involved You can get knowledge from Role. If the BA "
    "listed actors, check them against the roles the system actually "
    "has; correct or add any that don't match.\n\n"
    "`pre_condition` -- what must already be true in the system for this to "
    "work (state, permission, prior step). Ground this in what you actually "
    "find; if the BA listed pre-conditions, verify them.\n\n"
    "`impacted_areas` -- name the actual features, API "
    "business this touches, in the BA's own documents. If the BA listed some, verify each is "
    "really affected and add any real ones they missed.\n\n"
    "`requirements` -- describe what the system already does in this area "
    "today, and exactly what gap remains to fully satisfy the requirement "
    "(nothing missing if it's already fully supported). Describe it in plain "
    "business behaviour -- what a user can do and what the system does in "
    "response -- never by naming the actual endpoint path, method, class or "
    "annotation you found; that applies here just as strictly as everywhere "
    "else in this answer.\n\n"
    "`acceptance_criteria` -- if the BA already gave criteria, check each "
    "one against how the system actually behaves and flag any that don't "
    "hold; then add any criteria a careful BA would expect that are still "
    "missing. If none were given, propose a reasonable set grounded in what "
    "you found.\n\n"
    "`exceptions` -- same pattern as acceptance criteria: verify any "
    "edge cases the BA already listed against what the code actually "
    "handles, and add real edge cases they missed (only ones you can "
    "ground in the code -- never invent a hypothetical one).\n\n"
    "`is_feasible` and `already_supported` -- whether this is feasible to "
    "build given how the system works today, and whether it's already "
    "served by an existing feature.\n\n"
    "`verdict` -- one short paragraph in business language giving your "
    "overall conclusion: feasible or not, already supported or new work, "
    "and the one thing the BA most needs to know.\n\n"
    "Ground every field in what you actually found by investigating the "
    "repository -- never guess, and never claim to have made a change; you "
    "are only investigating.\n\n"
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
    is_requirement_clear: bool
    is_feasible: bool
    already_supported: bool
    user_story: str
    actors: str
    pre_condition: str
    impacted_areas: str
    requirements: str
    acceptance_criteria: str
    exceptions: str
    verdict: str


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
    parsed = parse_json(final_text, "extraction")
    if not isinstance(parsed, list):
        return []

    businesses = []
    for item in parsed:
        try:
            businesses.append(ExtractedBusiness.model_validate(item))
        except Exception:  # noqa: BLE001 - skip one bad item, keep the rest
            log.warning("business_agent: skipping unparseable business item: %r", item)
    return businesses


async def vet_business_stream(description: str, *, user_id: str,
                              history: str = "",
                              ) -> AsyncIterator[tuple[str, str | BusinessVetting]]:
    """Vet one business requirement against the live repo, yielding
    (adk_runner.STATUS, line) progress lines while the agent investigates,
    then (RESULT, BusinessVetting) last. Raises RuntimeError if GITHUB_PAT
    isn't configured; the caller is responsible for turning a per-item
    failure into a stored ERROR row instead of letting it abort the whole
    vetting stream.

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
            procedure=github_mcp.investigation_procedure(),
        ),
        tools=[toolset],
        output_schema=BusinessVetting,
        generate_content_config=build_generate_config(show_thinking=True),
    )
    final_text = ""
    try:
        async for kind, text in iter_turn(
            agent, with_history(history, description),
            app_name=APP_NAME, user_id=user_id,
        ):
            if kind == STATUS:
                yield STATUS, text
            elif kind == FINAL:
                final_text = text
    finally:
        await github_mcp.close_toolset(toolset)

    parsed = parse_json(final_text, "vetting")
    if parsed is None:
        raise RuntimeError(
            "The vetting agent's response could not be parsed as structured "
            "output."
        )
    yield RESULT, BusinessVetting.model_validate(parsed)

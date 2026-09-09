"""Stand-in for the analysis engine.

The real pipeline is: expand the question into likely table and column names,
run the hybrid search from the specification scoped to this project, send the
retrieved chunks to the model, then discard any finding whose quoted text
cannot be found in the artifact it claims to come from.

None of that exists yet. This module fakes the shape of the result so the
interface can be built and reviewed against it. It picks artifacts belonging to
the selected project by keyword overlap, so answers differ per project and the
citations point at rows that really are in the database.

Replacing it means rewriting `answer()` alone. Its return type is what the rest
of the application depends on.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

from sqlalchemy.orm import Session

from .models import Artifact, Project

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "do", "does",
    "did", "how", "what", "when", "where", "which", "who", "why", "can", "we",
    "i", "you", "it", "this", "that", "for", "and", "or", "if", "in", "on",
    "to", "of", "with", "there", "any", "should", "would", "will", "have",
    "has", "our", "us", "me", "my", "about", "from", "at", "by", "not",
}


@dataclass
class DraftCitation:
    kind: str
    source_ref: str
    quoted_span: str
    artifact_id: str | None = None


@dataclass
class Answer:
    content: str
    citations: list[DraftCitation] = field(default_factory=list)


def _terms(text: str) -> set[str]:
    words = re.findall(r"[a-z_]+", text.lower())
    return {w for w in words if len(w) > 2 and w not in _STOPWORDS}


def _rank(artifacts: list[Artifact], question: str) -> list[Artifact]:
    """Crude keyword overlap. The real version is the hybrid SQL query."""
    q = _terms(question)
    scored: list[tuple[int, Artifact]] = []
    for art in artifacts:
        haystack = _terms(f"{art.source_ref} {art.keywords} {art.content}")
        overlap = len(q & haystack)
        if overlap:
            scored.append((overlap, art))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [art for _, art in scored]


def _first_sentence(text: str, limit: int = 180) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    cut = cleaned[:limit]
    return cut.rsplit(" ", 1)[0] + "…"


_NO_EVIDENCE = (
    "I could not find anything in the indexed material for {project} that "
    "relates to that.\n\n"
    "That is a real answer rather than a failure: the index currently covers "
    "only part of this project, so a question about an area that has not been "
    "indexed will come back empty rather than guessed at. Try naming a table, "
    "a screen or a status code, or ask an administrator which parts of "
    "{project} are indexed."
)

_OPENERS = [
    "Here is what the indexed material for {project} shows.",
    "Based on what is indexed for {project}:",
    "Looking at {project} as it is built today:",
]

_CLOSERS = [
    "If this is going into an SRS, the question worth putting to the client is "
    "whether the behaviour above is the behaviour they expect.",
    "Worth confirming with the client before sign-off, since changing it after "
    "development starts is considerably more expensive.",
    "Ask the client to confirm this explicitly. It is the kind of assumption "
    "that usually surfaces during UAT instead.",
]


def answer(db: Session, project: Project, question: str) -> Answer:
    """Produce a placeholder reply grounded in this project's artifacts."""
    artifacts = db.query(Artifact).filter(Artifact.project_id == project.id).all()
    matches = _rank(artifacts, question)

    if not matches:
        return Answer(content=_NO_EVIDENCE.format(project=project.name))

    picked = matches[:3]
    rng = random.Random(f"{project.id}:{question.strip().lower()}")

    lines = [rng.choice(_OPENERS).format(project=project.name), ""]

    for art in picked:
        excerpt = _first_sentence(art.content)
        if art.kind == "SCHEMA":
            lines.append(
                f"The table `{art.source_ref}` is the one that governs this. "
                f"{excerpt}"
            )
        elif art.kind == "CODE":
            lines.append(
                f"The rule is enforced in `{art.source_ref}`. {excerpt}"
            )
        else:
            lines.append(
                f"There is relevant history in {art.source_ref}. {excerpt}"
            )
        lines.append("")

    lines.append(rng.choice(_CLOSERS))

    citations = [
        DraftCitation(
            kind=art.kind,
            source_ref=art.source_ref,
            quoted_span=_first_sentence(art.content, 140),
            artifact_id=art.id,
        )
        for art in picked
    ]

    return Answer(content="\n".join(lines).strip(), citations=citations)


def title_for(question: str) -> str:
    """Name a conversation from its opening message."""
    cleaned = " ".join(question.split())
    if len(cleaned) <= 60:
        return cleaned
    return cleaned[:60].rsplit(" ", 1)[0] + "…"

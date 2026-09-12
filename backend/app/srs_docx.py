"""Writing vetted stories into the user's own SRS format (a .docx).

An SRS format is one of two quite different things, and they need opposite
treatment:

1. A **story template** -- the shape BRAC IT's own SRS_Format.docx has. It is
   blank: `Features` > `<Title of Feature>` > `<Title of Story>`, and under
   each story heading a list of labelled but empty slots (`User Story:`,
   `Actors:`, `Requirements:` ...) with dummy filler under some of them
   ("Step 1", "Line 1", "Criteria 1"). Here a story must be written INTO a
   placeholder block: the heading's `<Title of Story>` replaced, each slot's
   filler replaced by the vetted field of the same name. Appending anything
   is wrong -- the blocks are the document.

2. A **finished specification** with topic sections -- `3.1 Tender
   Management`, `4.2 Security` and so on. There are no slots to fill, so a
   story is added to the section it belongs to, and which section that is
   takes a model to decide (app/srs_agent.py).

`read_shape` decides which of the two a given upload is, and the caller runs
the matching writer: `fill_template` or `build_sections`. Only the second
needs the model, so the common case (1) is deterministic and fast.

Three things about python-docx shape this module:

- It has no public API for inserting at an arbitrary position, only
  `add_paragraph`, which appends to the end of the body. So content is placed
  by moving XML elements: `anchor.addnext(el)`.

- Nothing it exposes can copy a paragraph's list formatting. Filling a slot
  therefore CLONES one of the template's own filler paragraphs
  (`copy.deepcopy`) and swaps the text, which keeps its numbering
  (`w:numPr`), indentation and run formatting exactly. This is why the result
  looks like the template rather than like something pasted in -- and why the
  template's automatic outline numbering still comes out right.

- `Document.paragraphs` skips tables, so a section's extent cannot be
  measured with it. The body's own children are walked instead, counting
  `w:p` and `w:tbl` alike.
"""
from __future__ import annotations

import copy
import io
import re
from dataclasses import dataclass, field

from docx import Document
from docx.oxml.ns import qn
from docx.text.paragraph import Paragraph
from fastapi import HTTPException

# The heading created when a topic-section format has nowhere for a story to
# go. Never used by the story-template path, which always has blocks.
FALLBACK_HEADING = "Vetted Business Requirements"

# The vetted fields that get written, in the order the Change Request / Story
# template itself reads, with the label used when the format has no slot of
# its own to reuse. The verdict and feasibility flags are deliberately absent:
# they are Setu's review of a requirement, not part of a specification.
STORY_FIELDS = (
    ("user_story", "User Story"),
    ("actors", "Actors"),
    ("scope", "Scope"),
    ("pre_condition", "Pre-condition"),
    ("impacted_areas", "Impacted Areas"),
    ("requirements", "Requirements"),
    ("acceptance_criteria", "Acceptance Criteria"),
    ("exceptions", "Exceptions"),
)
FIELD_LABELS = dict(STORY_FIELDS)

# How a slot's label in someone's format maps onto one of those fields. Word
# and wording vary between templates ("Pre-requisite", "Functional
# Requirements"), so the match is by alias rather than by exact text.
_FIELD_ALIASES = {
    "user story": "user_story",
    "user stories": "user_story",
    "story": "user_story",
    "story description": "user_story",
    "description": "user_story",
    "actors": "actors",
    "actor": "actors",
    "actors involved": "actors",
    "stakeholders": "actors",
    "scope": "scope",
    "scope of work": "scope",
    "in scope": "scope",
    "in scope / out of scope": "scope",
    "in-scope / out-of-scope": "scope",
    "pre-condition": "pre_condition",
    "pre condition": "pre_condition",
    "precondition": "pre_condition",
    "pre-conditions": "pre_condition",
    "pre conditions": "pre_condition",
    "preconditions": "pre_condition",
    "pre-requisite": "pre_condition",
    "prerequisite": "pre_condition",
    "prerequisites": "pre_condition",
    "impacted areas": "impacted_areas",
    "impacted area": "impacted_areas",
    "impact areas": "impacted_areas",
    "impacted modules": "impacted_areas",
    "affected areas": "impacted_areas",
    "requirements": "requirements",
    "requirement": "requirements",
    "functional requirements": "requirements",
    "business requirements": "requirements",
    "detailed requirements": "requirements",
    "acceptance criteria": "acceptance_criteria",
    "acceptance criterion": "acceptance_criteria",
    "acceptance criterias": "acceptance_criteria",
    "acceptance": "acceptance_criteria",
    "exceptions": "exceptions",
    "exception": "exceptions",
    "exception handling": "exceptions",
    "exception cases": "exceptions",
}

# A story block must carry at least this many slots that map to a known field
# before the document is treated as a story template. Three keeps an ordinary
# heading followed by a stray "Note:" paragraph from qualifying.
_MIN_KNOWN_SLOTS = 3

# "User Story:" or "User Story: some filler text" -- the label, and whatever
# was written after the colon on the same line.
_LABEL = re.compile(r"^\s*(.{1,60}?)\s*:\s*(.*)$", re.DOTALL)

# `<Title of Story>`, `< Feature ID >`: a slot the template means for a human
# to overwrite. The first one in a heading is the title.
_PLACEHOLDER = re.compile(r"<[^<>]{0,80}>")

_HEADING_LEVEL = re.compile(r"heading\s*(\d+)", re.IGNORECASE)

# Word rejects control characters in document XML; a model-written field can
# carry one and would produce a file Word refuses to open.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# A leading bullet or number typed into a value, when the paragraph it is
# going into is already a bulleted or numbered one.
_LEADING_MARKER = re.compile(r"^\s*(?:[-*•·–—]|\(?\d+[.)]|[a-z][.)])\s+")

# The vetting model sometimes writes a numbered list as one run-on sentence
# ("1. Foo 2. Bar 3. Baz") instead of one point per line. Detected by the
# numbering punctuation alone, so it splits back into points regardless of
# whether the source ever had real line breaks.
_INLINE_NUMBERED_SPLIT = re.compile(r"\s*(?=\d+\.\s)")
_INLINE_NUMBERED_ITEM = re.compile(r"^\d+\.\s")

# Same run-on problem, different shape: the `scope` field is written as
# "In scope: ... Out of scope: ..." in one sentence. Split right before
# "Out of scope" regardless of whether the source had a real line break.
_SCOPE_SPLIT = re.compile(r"\s+(?=out[\s-]of[\s-]scope\s*:)", re.IGNORECASE)

# Headings a requirement belongs under when the matching agent gives no usable
# answer, best first. Substrings of a lowercased heading. Topic mode only.
_DEFAULT_SECTION_HINTS = (
    "functional requirement",
    "system feature",
    "specific requirement",
    "business requirement",
    "requirement",
    "feature",
    "scope",
)

MAX_TEMPLATE_BYTES = 10 * 1024 * 1024  # matches extraction.MAX_UPLOAD_BYTES

TEMPLATE = "TEMPLATE"   # a blank story template: fill the blocks in
SECTIONS = "SECTIONS"   # a finished spec: add to the right topic section


@dataclass(frozen=True)
class Section:
    """One heading in a topic-section format, as offered to the agent."""
    index: int      # position in the outline list; what the agent picks
    text: str       # the heading's own text, e.g. "3.2 Functional Requirements"
    level: int      # 1 for Heading 1, 2 for Heading 2, ...


@dataclass(frozen=True)
class Story:
    """One vetted business item, reduced to what goes in the document."""
    seq_no: int
    title: str
    fields: tuple[tuple[str, str], ...]  # (field key, value), already ordered


@dataclass(frozen=True)
class Shape:
    """What kind of format an upload turned out to be.

    `blocks` counts the placeholder story blocks of a TEMPLATE; `sections` is
    the heading outline a SECTIONS format offers the agent. Each is empty for
    the other mode.
    """
    mode: str
    sections: tuple[Section, ...] = ()
    blocks: int = 0


@dataclass
class _Slot:
    """One labelled field inside a placeholder story block."""
    label_el: object            # the `Label:` paragraph
    key: str | None             # which vetted field it maps to, if any
    label_text: str = ""        # the label as the template writes it
    inline: str = ""            # anything after the colon on the label line
    content: list = field(default_factory=list)  # filler under the label


@dataclass
class _Block:
    """One placeholder story block: a heading plus its labelled slots."""
    heading_el: object
    level: int
    elements: list                       # heading + every element under it
    slots: list[_Slot]
    feature_no: int = 0
    feature_text: str = ""
    block_no: int = 0                    # position within its feature


def _clean(text: str) -> str:
    return _CONTROL_CHARS.sub("", text or "").strip()


def _field_key(label: str) -> str | None:
    """Which vetted field a slot label refers to, or None if it is one of the
    template's own fields that vetting does not produce (User Journey, Scope,
    Mock-up/ Prototypes) -- those are left exactly as they are."""
    normalised = " ".join(_clean(label).lower().split()).strip(" .:-–—")
    return _FIELD_ALIASES.get(normalised)


def _load(data: bytes):
    if not data:
        raise HTTPException(422, "The uploaded file is empty.")
    if len(data) > MAX_TEMPLATE_BYTES:
        raise HTTPException(
            413,
            f"File is larger than the {MAX_TEMPLATE_BYTES // (1024 * 1024)} MB limit.",
        )
    try:
        return Document(io.BytesIO(data))
    except Exception as exc:  # noqa: BLE001 - any parse failure is the same to the caller
        raise HTTPException(
            422,
            "That DOCX could not be read; it may be corrupt, password-protected "
            "or not a real Word document.",
        ) from exc


def _heading_level(paragraph: Paragraph) -> int | None:
    """The paragraph's outline level, or None if it isn't a heading.

    Title counts as level 0 so a document's own title is never mistaken for a
    section to write into.
    """
    name = (paragraph.style.name if paragraph.style else "") or ""
    if name.strip().lower() == "title":
        return 0
    match = _HEADING_LEVEL.match(name.strip())
    return min(int(match.group(1)), 9) if match else None


def _blocks(doc) -> list[tuple[object, int | None]]:
    """The body's children in document order as (element, heading level).

    Level is None for body text and for tables -- both are section content,
    and neither ends a section.
    """
    out: list[tuple[object, int | None]] = []
    for child in doc.element.body.iterchildren():
        if child.tag == qn("w:p"):
            out.append((child, _heading_level(Paragraph(child, doc))))
        elif child.tag == qn("w:tbl"):
            out.append((child, None))
    return out


# --- reading and writing paragraph text --------------------------------------

def _text_of(doc, element) -> str:
    if element.tag != qn("w:p"):
        return ""
    return Paragraph(element, doc).text


def _set_text(doc, element, text: str) -> None:
    """Replace a paragraph's text, keeping the formatting of its first run.

    Writing through the first run rather than adding a fresh one is what
    preserves the template's fonts, sizes and bold: a cloned bullet keeps
    looking like the bullet it was cloned from.
    """
    paragraph = Paragraph(element, doc)
    runs = paragraph.runs
    if runs:
        runs[0].text = text
        for extra in runs[1:]:
            extra._element.getparent().remove(extra._element)
    else:
        paragraph.add_run(text)


def _clone(element):
    """A deep copy of an element, safe to insert elsewhere in the document.

    Bookmarks and comment anchors are stripped: duplicating their ids across
    copies is what makes Word report a document as damaged.
    """
    copied = copy.deepcopy(element)
    for tag in ("w:bookmarkStart", "w:bookmarkEnd",
                "w:commentRangeStart", "w:commentRangeEnd"):
        for node in copied.findall(f".//{qn(tag)}"):
            node.getparent().remove(node)
    return copied


def _unlist(element) -> None:
    """Strip a cloned paragraph's link to its numbered-list instance while
    keeping the rest of its formatting.

    Used when the only available prototype for a field is another field's
    own filler: its indentation (`w:ind`) is normally set directly on the
    paragraph, independent of `w:numPr`, so removing just the numbering
    reference keeps the look of a list item without reusing a list instance
    that belongs to a different field -- which would otherwise make this
    field's points read as a continuation of that field's own numbering.
    """
    pPr = element.find(qn("w:pPr"))
    if pPr is None:
        return
    numPr = pPr.find(qn("w:numPr"))
    if numPr is not None:
        pPr.remove(numPr)


def _remove(element) -> None:
    parent = element.getparent()
    if parent is not None:
        parent.remove(element)


def _lines(value: str) -> list[str]:
    raw = [line for line in (_clean(l) for l in (value or "").splitlines()) if line]
    if len(raw) == 1:
        parts = [p.strip() for p in _INLINE_NUMBERED_SPLIT.split(raw[0]) if p.strip()]
        if len(parts) >= 2 and all(_INLINE_NUMBERED_ITEM.match(p) for p in parts):
            return parts
        parts = [p.strip() for p in _SCOPE_SPLIT.split(raw[0]) if p.strip()]
        if len(parts) == 2:
            return parts
    return raw


# --- recognising a story template --------------------------------------------

def _parse_slots(doc, content: list) -> list[_Slot]:
    """Split a story block's content into its labelled slots.

    Everything before the first label is ignored: it belongs to the heading,
    not to a field.
    """
    slots: list[_Slot] = []
    for element in content:
        if element.tag == qn("w:p"):
            match = _LABEL.match(_text_of(doc, element).strip())
            if match:
                label, inline = match.group(1), match.group(2).strip()
                slots.append(_Slot(label_el=element, key=_field_key(label),
                                   label_text=_clean(label), inline=inline))
                continue
        if slots:
            slots[-1].content.append(element)
    return slots


def _story_blocks(doc, blocks) -> list[_Block]:
    """Every placeholder story block in the document, in order.

    A story block is a heading whose own content -- up to the next heading of
    any level -- holds at least `_MIN_KNOWN_SLOTS` recognised field labels.
    Measuring to the *next* heading rather than to the next same-or-shallower
    one is what separates a story heading from the feature heading above it:
    the feature's own content is empty, because a story heading follows it
    immediately.
    """
    heading_positions = [i for i, (_, level) in enumerate(blocks)
                         if level is not None and level > 0]
    found: list[_Block] = []

    for order, pos in enumerate(heading_positions):
        end = (heading_positions[order + 1] if order + 1 < len(heading_positions)
               else len(blocks))
        content = [element for element, _ in blocks[pos + 1:end]]
        slots = _parse_slots(doc, content)
        if sum(1 for slot in slots if slot.key) < _MIN_KNOWN_SLOTS:
            continue
        found.append(_Block(
            heading_el=blocks[pos][0],
            level=blocks[pos][1],
            elements=[blocks[pos][0], *content],
            slots=slots,
        ))

    if not found:
        return found

    # Which feature each story sits under, for reporting where it went. The
    # feature level is the deepest heading level still above the stories --
    # in `Features > <Title of Feature> > <Title of Story>` that is the
    # middle one, not the "Features" banner above it.
    story_level = min(block.level for block in found)
    above = [level for _, level in blocks
             if level is not None and 0 < level < story_level]
    feature_level = max(above) if above else 0
    by_heading = {id(block.heading_el): block for block in found}
    feature_no = 0
    feature_text = ""
    per_feature = 0
    for element, level in blocks:
        block = by_heading.get(id(element))
        if block is not None:
            per_feature += 1
            block.feature_no = feature_no or 1
            block.feature_text = feature_text
            block.block_no = per_feature
        elif level is not None and level == feature_level:
            feature_no += 1
            per_feature = 0
            feature_text = _clean(_text_of(doc, element))
    return found


def read_shape(data: bytes) -> Shape:
    """Decide whether an upload is a story template or a finished spec."""
    doc = _load(data)
    blocks = _blocks(doc)
    story_blocks = _story_blocks(doc, blocks)
    if story_blocks:
        return Shape(mode=TEMPLATE, blocks=len(story_blocks))
    sections, _ = _outline(doc, blocks)
    return Shape(mode=SECTIONS, sections=tuple(sections))


# --- filling a story template ------------------------------------------------

def _is_listed(element) -> bool:
    """Whether the paragraph is a bulleted or numbered one."""
    pPr = element.find(qn("w:pPr"))
    return pPr is not None and pPr.find(qn("w:numPr")) is not None


def _block_prototypes(block: _Block) -> tuple[object | None, object | None]:
    """Two of the template's own filler paragraphs to clone values from: the
    first bulleted one and the first plain one.

    Some slots come with no filler at all ("Actors:" in SRS_Format.docx is
    followed by nothing), so their value has to borrow formatting from a
    neighbour. Which neighbour matters: a list of exceptions should read as
    the block's bullets, while a one-line value should read as plain text
    like the template's own `User Story` filler.
    """
    listed = plain = None
    for slot in block.slots:
        for element in slot.content:
            if element.tag != qn("w:p"):
                continue
            if _is_listed(element):
                if listed is None:
                    listed = element
            elif plain is None:
                plain = element
    return listed, plain


def _write_title(doc, element, title: str) -> None:
    """Put the story's title into its heading.

    Only the first `<...>` placeholder is replaced -- in
    `<Title of Story> | < Story ID> | <Story JIRA ID>` the ID placeholders are
    left for whoever assigns them, because Setu cannot know a JIRA id.
    """
    current = _text_of(doc, element)
    match = _PLACEHOLDER.search(current)
    if match:
        _set_text(doc, element,
                  current[:match.start()] + title + current[match.end():])
    else:
        _set_text(doc, element, title)


def _fill_slot(doc, slot: _Slot, value: str, prototypes) -> None:
    """Write a vetted field into its slot, replacing the template's filler."""
    lines = _lines(value)
    if not lines:
        return

    listed, plain = prototypes
    own = (slot.content[0]
           if slot.content and slot.content[0].tag == qn("w:p") else None)
    unlist = False
    if own is not None:
        # The slot's own filler: the truest guide to how it should look, and
        # if it carries Word's own list numbering, that numbering is this
        # field's own instance -- safe to keep and to drop the model's own
        # "1./2./3." text markers in favour of it.
        proto, keep_markers = own, False
    elif len(lines) > 1:
        # No filler of its own to borrow formatting from. Prefer `listed` for
        # its indentation (it reads as a bulleted field, matching its
        # siblings), but its w:numPr is a DIFFERENT field's own numbered-list
        # instance -- cloning it as-is risks that field's numbering carrying
        # on instead of this one starting at 1. _unlist() keeps the
        # indentation (set directly on the paragraph) and drops just the
        # numbering link, so the model's own markers become the only, now
        # unambiguous, enumeration.
        proto = listed if listed is not None else plain
        unlist = proto is listed
        keep_markers = True
    else:
        proto, keep_markers = (plain if plain is not None else listed), False

    # No paragraph anywhere to copy the list formatting from: the value goes
    # on the label's own line, which every format can render.
    if proto is None:
        label = slot.label_text or FIELD_LABELS.get(slot.key or "", "")
        _set_text(doc, slot.label_el, f"{label}: {' '.join(lines)}")
        return

    anchor = slot.label_el
    for line in lines:
        element = _clone(proto)
        if unlist:
            _unlist(element)
        text = line if keep_markers else _LEADING_MARKER.sub("", line)
        _set_text(doc, element, text)
        anchor.addnext(element)
        anchor = element

    # The filler is removed only now: it was the prototype.
    for element in slot.content:
        _remove(element)
    # A label that carried its filler inline ("User Story: As a user I want
    # to...") keeps just the label.
    if slot.inline:
        _set_text(doc, slot.label_el, f"{slot.label_text}:")


def _append_field(doc, block: _Block, key: str, value: str, prototypes) -> None:
    """Add a field the format has no slot for, after the block's last slot.

    The new label is cloned from a real one, so it inherits the template's
    outline numbering instead of appearing as unnumbered body text.
    """
    lines = _lines(value)
    if not lines or not block.slots:
        return
    label_proto = block.slots[-1].label_el
    tail = block.slots[-1].content[-1] if block.slots[-1].content else label_proto

    label_el = _clone(label_proto)
    _set_text(doc, label_el, f"{FIELD_LABELS.get(key, key)}:")
    tail.addnext(label_el)

    # This field has no slot of its own anywhere in the template, so there is
    # no "this field's own" numbered-list instance to borrow -- only ever
    # another field's. Same fix as _fill_slot()'s borrowed-prototype path:
    # keep `listed`'s indentation for a consistent look, but strip its
    # w:numPr so this field's points don't read as a continuation of a
    # different field's numbering, and keep the model's own markers instead.
    listed, plain = prototypes
    multi = len(lines) > 1
    if listed is not None and multi:
        proto, keep_markers, unlist = listed, True, True
    elif plain is not None:
        proto, keep_markers, unlist = plain, multi, False
    else:
        proto, keep_markers, unlist = listed, False, False
    anchor = label_el
    for line in lines:
        element = _clone(proto) if proto is not None else _clone(label_proto)
        if unlist:
            _unlist(element)
        text = line if keep_markers else _LEADING_MARKER.sub("", line)
        _set_text(doc, element, text)
        anchor.addnext(element)
        anchor = element
    block.slots.append(_Slot(label_el=label_el, key=key,
                             label_text=FIELD_LABELS.get(key, key)))


def _fill_block(doc, block: _Block, story: Story) -> set[str]:
    """Write one story into one placeholder block.

    Returns the labels left untouched -- the template's own fields that
    vetting does not produce -- so the user can be told what still needs
    filling in by hand.
    """
    _write_title(doc, block.heading_el, story.title)
    prototypes = _block_prototypes(block)
    values = {key: value for key, value in story.fields if (value or "").strip()}

    untouched: set[str] = set()
    for slot in block.slots:
        if slot.key and slot.key in values:
            _fill_slot(doc, slot, values.pop(slot.key), prototypes)
        elif slot.label_text:
            untouched.add(slot.label_text)

    # Anything the format has no slot for is added rather than dropped.
    for key in [k for k, _ in STORY_FIELDS if k in values]:
        _append_field(doc, block, key, values[key], prototypes)
    return untouched


def _drop_empty_features(doc, kept_headings: set[int]) -> None:
    """Remove a feature heading whose stories were all removed.

    Only one still holding a `<placeholder>` is removed: a heading the user
    has already named is theirs, and is left even when empty.
    """
    blocks = _blocks(doc)
    heading_positions = [i for i, (_, level) in enumerate(blocks)
                         if level is not None and level > 0]
    for order, pos in enumerate(heading_positions):
        element, level = blocks[pos]
        if id(element) in kept_headings:
            continue
        if not _PLACEHOLDER.search(_clean(_text_of(doc, element))):
            continue
        # Empty if no deeper heading follows before the next one at this level.
        has_children = False
        for next_pos in heading_positions[order + 1:]:
            next_level = blocks[next_pos][1]
            if next_level <= level:
                break
            has_children = True
            break
        if has_children:
            continue
        for following, following_level in blocks[pos + 1:]:
            if following_level is not None and 0 < following_level <= level:
                break
            _remove(following)
        _remove(element)


def fill_template(data: bytes, stories: list[Story],
                  ) -> tuple[bytes, dict[int, str], str]:
    """Fill a story template's placeholder blocks with the vetted stories.

    One story per block, in plan order. More stories than blocks clones the
    last block (an unfilled copy, taken before anything was written); fewer
    removes the blocks left over, and any feature heading left with no
    stories under it.

    Returns (docx bytes, {seq_no: where it went}, notice).
    """
    if not stories:
        raise HTTPException(422, "There are no vetted businesses to write.")

    doc = _load(data)
    blocks = _story_blocks(doc, _blocks(doc))
    if not blocks:
        raise HTTPException(422, "This format has no story blocks to fill in.")

    original_count = len(blocks)
    # Taken before any filling, so extra stories are cloned from a blank block.
    pristine = [_clone(element) for element in blocks[-1].elements]
    pristine_level = blocks[-1].level

    placements: dict[int, str] = {}
    untouched: set[str] = set()
    cloned = 0

    for index, story in enumerate(stories):
        if index < len(blocks):
            block = blocks[index]
        else:
            # Another copy of the blank block, appended after the last one.
            anchor = blocks[-1].elements[-1]
            fresh = []
            for element in pristine:
                element = _clone(element)
                anchor.addnext(element)
                anchor = element
                fresh.append(element)
            block = _Block(heading_el=fresh[0], level=pristine_level,
                           elements=fresh,
                           slots=_parse_slots(doc, fresh[1:]),
                           feature_no=blocks[-1].feature_no,
                           feature_text=blocks[-1].feature_text,
                           block_no=blocks[-1].block_no + 1)
            blocks.append(block)
            cloned += 1

        untouched |= _fill_block(doc, block, story)
        where = (block.feature_text
                 if block.feature_text and not _PLACEHOLDER.search(block.feature_text)
                 else f"Feature {block.feature_no}")
        placements[story.seq_no] = f"{where} · story block {block.block_no}"

    # Blocks the plan did not need, and any feature heading they emptied.
    spare = blocks[len(stories):]
    for block in spare:
        for element in block.elements:
            _remove(element)
    if spare:
        _drop_empty_features(doc, {id(b.heading_el) for b in blocks[:len(stories)]})

    notes = []
    if cloned:
        notes.append(
            f"Your format had {original_count} story "
            f"{'block' if original_count == 1 else 'blocks'} for "
            f"{len(stories)} stories, so {cloned} more "
            f"{'was' if cloned == 1 else 'were'} added by copying the last one."
        )
    if spare:
        notes.append(
            f"{len(spare)} unused story "
            f"{'block was' if len(spare) == 1 else 'blocks were'} removed."
        )
    if untouched:
        notes.append(
            "Left for you to fill in: " + ", ".join(sorted(untouched))
            + ", and the ID fields in the headings."
        )

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue(), placements, " ".join(notes)


# --- adding to a finished specification --------------------------------------

def _outline(doc, blocks) -> tuple[list[Section], list[int]]:
    """The headings as Sections, plus each one's position in `blocks` so the
    writer can find where its section ends.
    """
    sections: list[Section] = []
    positions: list[int] = []
    for pos, (element, level) in enumerate(blocks):
        # Level 0 is the document Title: not a section to write into.
        if level is None or level == 0:
            continue
        text = _clean(_text_of(doc, element))
        if text:
            sections.append(Section(index=len(sections), text=text, level=level))
            positions.append(pos)
    return sections, positions


def read_outline(data: bytes) -> list[Section]:
    """The format's headings, in document order."""
    doc = _load(data)
    sections, _ = _outline(doc, _blocks(doc))
    return sections


def default_section(sections: list[Section]) -> Section | None:
    """Where a requirement goes when nothing better is known: the first
    heading that reads like a requirements section, shallowest match first.
    """
    for hint in _DEFAULT_SECTION_HINTS:
        matches = [s for s in sections if hint in s.text.lower()]
        if matches:
            return min(matches, key=lambda s: (s.level, s.index))
    return None


class _Writer:
    """Appends paragraphs to the body, then moves them into place."""

    def __init__(self, doc):
        self.doc = doc
        self._styles = {s.name for s in doc.styles}

    def has(self, style: str) -> bool:
        return style in self._styles

    def para(self, anchor, *, style: str | None = None):
        """A new empty paragraph directly after `anchor`, and the new anchor."""
        paragraph = self.doc.add_paragraph(
            style=style if style and self.has(style) else None)
        anchor.addnext(paragraph._p)
        return paragraph, paragraph._p

    def heading_style(self, level: int) -> str | None:
        """`Heading <level>`, falling back up the outline to whatever the
        template defines; None if it defines no heading styles."""
        for candidate in range(min(max(level, 1), 9), 0, -1):
            if self.has(f"Heading {candidate}"):
                return f"Heading {candidate}"
        return None


def _body_style(doc, blocks, start: int, end: int) -> str | None:
    """The style the section's own body text uses, so inserted paragraphs
    match it (formats often set a named body style, not Normal).
    """
    for element, level in blocks[start:end]:
        if level is not None or element.tag != qn("w:p"):
            continue
        paragraph = Paragraph(element, doc)
        if paragraph.text.strip() and paragraph.style and paragraph.style.name:
            return paragraph.style.name
    return None


def _section_extent(blocks, heading_pos: int, level: int) -> int:
    """Index one past the section's last block: everything up to the next
    heading at the same or a shallower level (or the end of the body).
    """
    for pos in range(heading_pos + 1, len(blocks)):
        block_level = blocks[pos][1]
        if block_level is not None and 0 < block_level <= level:
            return pos
    return len(blocks)


def _last_block(doc):
    """The last paragraph or table currently in the body.

    Read when needed rather than taken from a `blocks` list: the appended
    heading has to land after everything, including stories just written into
    the document's final section.
    """
    last = None
    for child in doc.element.body.iterchildren():
        if child.tag in (qn("w:p"), qn("w:tbl")):
            last = child
    return last


def _write_value(writer: _Writer, anchor, label: str, value: str,
                 *, body_style: str | None):
    """One labelled field: `Label: value`, bold label, value as written.

    A multi-line value (Requirements and Acceptance Criteria usually are)
    becomes a bulleted list under the label where the format defines a bullet
    style.
    """
    lines = _lines(value)
    if not lines:
        return anchor

    single = len(lines) == 1
    paragraph, anchor = writer.para(anchor, style=body_style)
    run = paragraph.add_run(f"{label}: " if single else f"{label}:")
    run.bold = True

    if single:
        paragraph.add_run(lines[0])
        return anchor

    bullet = "List Bullet" if writer.has("List Bullet") else body_style
    for line in lines:
        item, anchor = writer.para(anchor, style=bullet)
        item.add_run(_LEADING_MARKER.sub("", line) if bullet == "List Bullet" else line)
    return anchor


def _write_story(writer: _Writer, anchor, story: Story, *,
                 heading_level: int, body_style: str | None):
    style = writer.heading_style(heading_level)
    heading, anchor = writer.para(anchor, style=style)
    run = heading.add_run(f"Story {story.seq_no} — {story.title}")
    if style is None:
        # No heading styles in this format: keep the title visible anyway
        # rather than letting it read as another body paragraph.
        run.bold = True

    for key, value in story.fields:
        anchor = _write_value(writer, anchor, FIELD_LABELS.get(key, key), value,
                              body_style=body_style)
    return anchor


def build_sections(data: bytes, stories: list[Story],
                   assignments: dict[int, int]) -> tuple[bytes, dict[int, str], str]:
    """Add each story to the topic section it was assigned.

    `assignments` maps a story's seq_no to a Section.index from
    `read_outline`. A seq_no that is missing, or points at a section that
    isn't there, falls back to `default_section` and then to a new heading
    appended at the end -- a story is never silently dropped.

    Returns (docx bytes, {seq_no: section it went into}, notice).
    """
    if not stories:
        raise HTTPException(422, "There are no vetted businesses to write.")

    doc = _load(data)
    writer = _Writer(doc)
    blocks = _blocks(doc)
    sections, heading_positions = _outline(doc, blocks)

    fallback = default_section(sections)
    notice = ""
    if not sections:
        notice = (
            f"This format has no Heading-styled sections, so the stories were "
            f"added under a new \"{FALLBACK_HEADING}\" heading at the end of "
            f"the document."
        )

    # Group by target section, keeping plan order within each group, before
    # touching the document: every anchor is then computed on the untouched
    # body.
    grouped: dict[int | None, list[Story]] = {}
    for story in stories:
        index = assignments.get(story.seq_no)
        if index is None or not 0 <= index < len(sections):
            index = fallback.index if fallback else None
        grouped.setdefault(index, []).append(story)

    placements: dict[int, str] = {}

    for index, group in grouped.items():
        if index is None:
            continue
        section = sections[index]
        heading_pos = heading_positions[index]
        extent = _section_extent(blocks, heading_pos, section.level)
        anchor = blocks[extent - 1][0]
        body_style = _body_style(doc, blocks, heading_pos + 1, extent)
        for story in group:
            anchor = _write_story(writer, anchor, story,
                                  heading_level=section.level + 1,
                                  body_style=body_style)
            placements[story.seq_no] = section.text

    orphans = grouped.get(None, [])
    if orphans:
        tail = _last_block(doc)
        if tail is None:
            tail = doc.add_paragraph()._p
        heading, anchor = writer.para(tail, style=writer.heading_style(1))
        heading.add_run(FALLBACK_HEADING).bold = writer.heading_style(1) is None
        body_style = _body_style(doc, blocks, 0, len(blocks))
        for story in orphans:
            anchor = _write_story(writer, anchor, story, heading_level=2,
                                  body_style=body_style)
            placements[story.seq_no] = FALLBACK_HEADING
        if sections and not notice:
            notice = (
                f"{len(orphans)} of {len(stories)} stories did not match any "
                f"section in this format, so they were added under a new "
                f"\"{FALLBACK_HEADING}\" heading at the end."
            )

    buffer = io.BytesIO()
    doc.save(buffer)
    return buffer.getvalue(), placements, notice

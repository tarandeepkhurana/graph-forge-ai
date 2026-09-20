"""Reading a document as a map you could learn from.

This replaces per-chunk triple extraction, which produced the opposite of what
this app is for. Measured on a real 6-page PDF: 82 concepts, 71 relations, 12
disconnected islands, 58 concepts mentioned exactly once, and roughly twenty
section headings stored as if they were ideas. Arranged neatly, it was still
unreadable -- there was nothing there to read.

Three things caused that, and all three are fixed here.

**The document was never seen whole.** That PDF is 1,881 tokens. It was cut
into two windows and each was extracted blind to the other, so nothing could
link across the halves (the islands), nothing could notice it had already named
something (`Recommendation Systems` and `recommendation systems` both exist),
and nothing could keep a relation pointing the same way twice -- the old graph
contains both `A example_of B` and `B example_of A` for the same pair.
Chunking is how you index a corpus for retrieval. This app maps one document.

**The prompt asked for recall.** It said, in as many words, that missing a
concept was worse than including a minor one. That instruction is the 58
one-offs.

**There was nowhere to put detail.** Told not to make "Advantages of X" a
concept, a model will do it anyway, because in a document built from bullet
lists under headings that is most of the content. It stops doing it once the
bullets have somewhere else to live -- so every concept here carries fields,
and the map falls from 82 nodes to 5 without losing anything.

What comes back is a map: a few concepts, what each one is, what you need to
understand before it, what the document claims about them, and the dimensions
it compares them on.
"""

from __future__ import annotations

import json
import logging
import re

from pydantic import BaseModel, Field

from graphforge.core.config import get_settings
from graphforge.extraction.schema import normalize

log = logging.getLogger(__name__)


# A document under this goes to the model in one piece. It is not a context
# limit -- gpt-5-mini takes 400k -- it is where one map stops being the right
# shape for one document. A 100-page paper is about 50k tokens and is still one
# subject; a book is not, and is handled section by section below.
SINGLE_PASS_TOKENS = 50_000

# How many concepts may reach the canvas. The whole point of the exercise: a
# map of eighty items is not a map. Detail lives in the fields, not in nodes.
MAX_CONCEPTS = 14
MAX_CONCEPTS_PER_SECTION = 8

# A node caption, not a definition. The model returns things like
# "Full Retraining (Batch Learning: Retrain from scratch)", which wraps to four
# lines under a 54px circle and collides with its neighbours.
MAX_NAME_CHARS = 46


def estimate_tokens(text: str) -> int:
    """Rough enough to choose a strategy; not worth a tokeniser dependency."""
    return len(text) // 4


# --------------------------------------------------------------- the shape --
class Concept(BaseModel):
    """One thing the document teaches, with what it says about it.

    The list fields are why this works. They are the drain that stops section
    headings from becoming nodes.
    """

    name: str
    one_liner: str = ""
    importance: str = "supporting"
    requires: list[str] = Field(default_factory=list)
    how_it_works: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    when_to_use: list[str] = Field(default_factory=list)
    examples: list[str] = Field(default_factory=list)
    quote: str = ""


class Link(BaseModel):
    """A claim the document makes between two concepts.

    `why` is what makes an edge worth drawing: "suffers from" tells you little,
    "suffers from -- it can forget past patterns" teaches.
    """

    source: str
    target: str
    relation: str
    why: str = ""


class Contrast(BaseModel):
    """One dimension the document compares concepts on.

    Usually the most valuable thing in a document that compares things, and the
    old pipeline threw it away entirely: a comparison table became a scatter of
    unrelated nodes.
    """

    dimension: str
    values: dict[str, str] = Field(default_factory=dict)


class ConceptMap(BaseModel):
    teaches: str = ""
    concepts: list[Concept] = Field(default_factory=list)
    links: list[Link] = Field(default_factory=list)
    contrasts: list[Contrast] = Field(default_factory=list)


# ------------------------------------------------------------- the prompt --
SYSTEM = """You are making a map that helps someone LEARN a document, which they will see as a diagram before they read it.

## Concepts, and what the document says ABOUT them

A concept is something the document teaches: an idea, method, technique, tool
or quantity the reader has to understand.

These are NEVER concepts, however much of the page they occupy. They are FIELDS
ON the concept they describe:
- "Advantages of X" / "Benefits of X"      -> X.strengths
- "Disadvantages of X" / "Limitations of X" -> X.limitations
- "Use Cases of X" / "Applications of X"    -> X.when_to_use
- "Examples of X"                           -> X.examples
- "How X works" / "The X Process" / steps   -> X.how_it_works
- "Comparison of X and Y"                   -> a contrast entry, not a concept

If most of the document is bullet lists under headings like these, that is
expected and correct: the bullets become fields and you will end up with FEW
concepts. A map of five well-described concepts beats a map of forty labels.

Also not concepts: a restatement of another concept ("Online Learning" and
"Online Learning algorithm" are one concept -- use the form the document uses
most), and anything you would have to invent.

## How many

%(cap)s concepts. This is a hard limit. If you are over it you are still
turning headings into concepts; go back and fold them in as fields.

## Prerequisites

`requires` lists concepts FROM YOUR OWN LIST that a reader must understand
first. This is the most valuable thing you produce -- it shows why the document
is ordered the way it is, which is the thing a diagram can show and a page of
text cannot. Be strict: a genuine dependency, never "these are near each
other". Prerequisites must not form a cycle. Parallel alternatives do not
require each other; leave the list empty rather than invent an order.

## Links

A link is a claim the document actually makes, between two concepts on your
list. Use a short verb phrase in the document's own terms -- "trains on",
"discards", "adapts to". Never a vague connector like "related to" or
"associated with": if you cannot say what the relationship IS, leave it out.
Do not link a concept to its own fields. `why` quotes or closely paraphrases
the sentence that supports the claim, in 15 words or fewer.

## Contrasts

Where the document compares concepts, record the dimension compared and what
each concept does on it. This is how a reader tells things apart and it is
usually the point of a document that compares things.

## Grounding

`quote` is up to 25 words copied VERBATIM from the document, the passage that
best supports the concept. Never paraphrase it, and never write a quote for
something the document does not say.

Return ONLY JSON in this shape:
{
 "teaches": "one sentence: what a reader gets from this document",
 "concepts": [{
   "name": "short name in the document's own wording, at most 46 characters, no parenthetical explanation",
   "one_liner": "what it is, <=20 words, in the document's terms",
   "importance": "core" | "supporting",
   "requires": ["concept names from this list"],
   "how_it_works": ["short steps"], "strengths": ["..."],
   "limitations": ["..."], "when_to_use": ["..."], "examples": ["..."],
   "quote": "<=25 words verbatim from the document"
 }],
 "links": [{"from": "...", "to": "...", "relation": "verb phrase", "why": "<=15 words"}],
 "contrasts": [{"dimension": "e.g. update frequency", "values": {"ConceptName": "what it does"}}]
}"""


def _system(cap: str) -> str:
    return SYSTEM % {"cap": cap}


# ----------------------------------------------------------------- calling --
def _client():
    key = get_settings().openai_api_key
    if not key or key.startswith("sk-placeholder"):
        raise RuntimeError("OPENAI_API_KEY is missing from app/.env — extraction cannot run")
    from openai import OpenAI

    return OpenAI(api_key=key)


def _model() -> str:
    return get_settings().extraction_openai_model


def _ask(system: str, user: str) -> dict:
    response = _client().chat.completions.create(
        model=_model(),
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_object"},
    )
    return json.loads(response.choices[0].message.content or "{}")


def _parse(raw: dict) -> ConceptMap:
    """Build a map from model output, tolerating the shapes it actually uses.

    `from`/`to` are reserved-ish in Python and read badly as field names, but
    they are the natural words in the JSON, so the rename happens here.
    """
    concepts = [Concept(**c) for c in raw.get("concepts", []) if c.get("name")]
    links = []
    for raw_link in raw.get("links", []):
        source = raw_link.get("from") or raw_link.get("source")
        target = raw_link.get("to") or raw_link.get("target")
        if source and target and raw_link.get("relation"):
            links.append(
                Link(
                    source=source,
                    target=target,
                    relation=raw_link["relation"],
                    why=raw_link.get("why", ""),
                )
            )
    contrasts = [
        Contrast(dimension=c.get("dimension", ""), values=c.get("values", {}) or {})
        for c in raw.get("contrasts", [])
        if c.get("values")
    ]
    return ConceptMap(
        teaches=raw.get("teaches", ""),
        concepts=concepts,
        links=links,
        contrasts=contrasts,
    )


# ------------------------------------------------------------------- tidy --
_VAGUE = {"related_to", "related to", "associated with", "relates to", "linked to"}


def tidy(cmap: ConceptMap, cap: int = MAX_CONCEPTS) -> ConceptMap:
    """Make the map self-consistent before it reaches the graph.

    The model is good but not reliable, and every one of these checks is here
    because the old pipeline shipped the failure straight to the canvas: an
    edge to a concept that was never created, a prerequisite pointing at
    itself, the same concept under two capitalisations.
    """
    # Shorten first, then fold: two names can shorten to the same concept, and
    # links written against the long form have to follow it to the short one.
    alias: dict[str, str] = {}
    by_key: dict[str, Concept] = {}
    for concept in cmap.concepts:
        original = normalize(concept.name)
        concept.name = _short_name(concept.name)
        key = normalize(concept.name)
        if not key:
            continue
        alias[original] = key
        if key in by_key:
            by_key[key] = _merge(by_key[key], concept)
        else:
            by_key[key] = concept

    # Core concepts first, so the cap cuts detail rather than the subject.
    ordered = sorted(
        by_key.values(), key=lambda c: (c.importance != "core", -_weight(c))
    )[:cap]
    kept = {normalize(c.name) for c in ordered}
    name_of = {normalize(c.name): c.name for c in ordered}

    def resolve(value: str) -> str:
        """Map any form of a name -- long, short, mis-cased -- to its key."""
        key = normalize(value)
        return alias.get(key, key)

    core = {normalize(c.name) for c in ordered if c.importance == "core"}
    for concept in ordered:
        # A prerequisite must be a concept on the map, and nothing requires
        # itself -- both of which the model does occasionally produce.
        #
        # It also must not run from a core concept down to a supporting one.
        # Supporting concepts are the techniques and details *inside* the
        # subject, so that direction is always the model getting the arrow
        # backwards: one run produced "Batch Machine Learning requires Full
        # retraining" while also saying "Full retraining implements Batch
        # Machine Learning". Direction is the whole value of a prerequisite,
        # and a reader sent to the details first is worse served than one sent
        # nowhere, so the doubtful edge is dropped rather than reversed.
        is_core = normalize(concept.name) in core
        concept.requires = _unique(
            name_of[resolve(r)]
            for r in concept.requires
            if resolve(r) in kept
            and resolve(r) != normalize(concept.name)
            and not (is_core and resolve(r) not in core)
        )

    links = []
    seen: set[tuple[str, str, str]] = set()
    for link in cmap.links:
        source, target = resolve(link.source), resolve(link.target)
        relation = (link.relation or "").strip()
        if source not in kept or target not in kept or source == target:
            continue
        if not relation or relation.lower() in _VAGUE:
            continue
        signature = (source, target, relation.lower())
        if signature in seen:
            continue
        seen.add(signature)
        links.append(
            Link(
                source=name_of[source],
                target=name_of[target],
                relation=relation,
                why=link.why,
            )
        )

    contrasts = []
    for contrast in cmap.contrasts:
        values = {
            name_of[resolve(k)]: v
            for k, v in contrast.values.items()
            if resolve(k) in kept
        }
        # A contrast of one thing is not a contrast.
        if len(values) >= 2 and contrast.dimension.strip():
            contrasts.append(Contrast(dimension=contrast.dimension.strip(), values=values))

    return ConceptMap(
        teaches=cmap.teaches.strip(),
        concepts=_break_cycles(ordered),
        links=links,
        contrasts=contrasts,
    )


def _short_name(name: str) -> str:
    """Trim a concept name down to something that fits under a node.

    A parenthetical is nearly always the model explaining itself -- "partial_fit
    () / SGD (incremental update API)" -- and the part before it is the name.
    """
    name = " ".join(name.split())
    # Always, not only when over the limit: "partial_fit() / SGD (incremental
    # update API)" is 44 characters and slips under any sane cap while still
    # being unreadable as a caption. What precedes the bracket is the name.
    if "(" in name:
        head = name.split("(")[0].strip(" -–—:/,")
        if len(head) >= 3:
            name = head
    if len(name) > MAX_NAME_CHARS:
        name = name[:MAX_NAME_CHARS].rsplit(" ", 1)[0].rstrip(" ,;:-/")
    return name


def _weight(concept: Concept) -> int:
    """How much the document actually says about a concept."""
    return sum(
        len(getattr(concept, field))
        for field in ("how_it_works", "strengths", "limitations", "when_to_use", "examples")
    )


def _merge(a: Concept, b: Concept) -> Concept:
    for field in ("how_it_works", "strengths", "limitations", "when_to_use", "examples", "requires"):
        setattr(a, field, _unique(getattr(a, field) + getattr(b, field)))
    a.one_liner = a.one_liner or b.one_liner
    a.quote = a.quote or b.quote
    if b.importance == "core":
        a.importance = "core"
    return a


def _unique(values) -> list[str]:
    seen, out = set(), []
    for value in values:
        key = normalize(str(value))
        if key and key not in seen:
            seen.add(key)
            out.append(str(value).strip())
    return out


def _break_cycles(concepts: list[Concept]) -> list[Concept]:
    """Drop the prerequisite edges that close a loop.

    A cycle makes the reading order meaningless -- "read A first, but read B
    before A" -- and the flow view would have no root to start from. The edge
    that closes the loop is the one dropped, so the rest of the order survives.
    """
    requires = {normalize(c.name): [normalize(r) for r in c.requires] for c in concepts}
    state: dict[str, int] = {}
    removed: set[tuple[str, str]] = set()

    def visit(node: str) -> None:
        state[node] = 1  # on the current path
        for need in list(requires.get(node, [])):
            if state.get(need) == 1:
                removed.add((node, need))
            elif state.get(need, 0) == 0:
                visit(need)
        state[node] = 2  # finished

    for key in requires:
        if state.get(key, 0) == 0:
            visit(key)

    if removed:
        log.info("dropped %s prerequisite(s) that formed a cycle", len(removed))
        for concept in concepts:
            key = normalize(concept.name)
            concept.requires = [
                r for r in concept.requires if (key, normalize(r)) not in removed
            ]
    return concepts


# -------------------------------------------------------------- strategies --
def build_map(text: str) -> ConceptMap:
    """Read a whole document as one map.

    Small documents -- which is nearly everything anyone uploads -- go in one
    piece, because that is the only way the model can see that two sections are
    talking about the same thing.
    """
    text = text.strip()
    if not text:
        return ConceptMap()
    if estimate_tokens(text) <= SINGLE_PASS_TOKENS:
        return tidy(_parse(_ask(_system(f"Between 5 and {MAX_CONCEPTS}"), text)))
    return _build_sectioned(text)


_HEADING = re.compile(r"^(#{1,6} .*|.+\n[=-]{3,})$", re.MULTILINE)


def _sections(text: str) -> list[str]:
    """Split on the document's own headings, never mid-sentence.

    The old chunker cut every 2,000 characters regardless of what was there,
    which is how a definition ended up in one window and its explanation in
    another. A heading is where the author already decided one thing ends.
    """
    cuts = sorted({0, len(text), *(m.start() for m in _HEADING.finditer(text))})
    parts = [text[a:b].strip() for a, b in zip(cuts, cuts[1:]) if text[a:b].strip()]
    return parts or [text]


def _batches(parts: list[str], budget_tokens: int) -> list[str]:
    """Group whole sections up to a budget, so a call still sees context."""
    out: list[str] = []
    current: list[str] = []
    size = 0
    for part in parts:
        cost = estimate_tokens(part)
        if current and size + cost > budget_tokens:
            out.append("\n\n".join(current))
            current, size = [], 0
        current.append(part)
        size += cost
    if current:
        out.append("\n\n".join(current))
    return out


def _build_sectioned(text: str) -> ConceptMap:
    """A document too long to be one map: read it a section at a time.

    Each call still gets two things the old chunked extraction never had -- the
    document's outline, so it knows where this passage sits in the whole, and
    the concepts already found, so it reuses a name instead of coining a second
    one for the same idea. Then one consolidation pass runs over just the
    concept list, which is small however long the document was.
    """
    outline = "\n".join(m.group(0).strip() for m in _HEADING.finditer(text)) or "(no headings)"
    batches = _batches(_sections(text), SINGLE_PASS_TOKENS // 2)
    log.info("document too long for one pass; reading %s section group(s)", len(batches))

    merged = ConceptMap()
    for index, batch in enumerate(batches, 1):
        known = ", ".join(c.name for c in merged.concepts) or "(none yet)"
        prompt = (
            f"DOCUMENT OUTLINE (for context; you are reading part {index} of {len(batches)}):\n"
            f"{outline}\n\n"
            f"CONCEPTS ALREADY ON THE MAP -- reuse these names exactly rather than "
            f"coining a new name for the same idea:\n{known}\n\n"
            f"PASSAGE:\n{batch}"
        )
        try:
            part = _parse(_ask(_system(f"At most {MAX_CONCEPTS_PER_SECTION}"), prompt))
        except Exception:
            # One bad section must not lose the rest of a long document.
            log.exception("section %s of %s failed; continuing", index, len(batches))
            continue
        merged.teaches = merged.teaches or part.teaches
        merged.concepts += part.concepts
        merged.links += part.links
        merged.contrasts += part.contrasts

    merged = tidy(merged, cap=MAX_CONCEPTS * 2)
    return _consolidate(merged)


def _consolidate(cmap: ConceptMap) -> ConceptMap:
    """One last pass over the concept list alone, not the document.

    Sections read separately still produce near-duplicates and miss the
    dependencies that run between them. This input is a list of names and
    one-liners, so it stays small no matter how long the document was.
    """
    if len(cmap.concepts) <= MAX_CONCEPTS:
        return cmap

    listing = "\n".join(
        f"- {c.name} ({c.importance}): {c.one_liner}" for c in cmap.concepts
    )
    system = (
        "You are consolidating a concept map assembled from separate sections of "
        "one document. Merge concepts that are the same idea under different "
        f"names, drop any that are section headings rather than ideas, keep at "
        f"most {MAX_CONCEPTS}, and set `requires` across the whole document. "
        'Return ONLY JSON: {"keep": [{"name": "canonical name", '
        '"merge": ["other names for this same concept"], '
        '"importance": "core"|"supporting", "requires": ["names"]}]}'
    )
    try:
        decision = _ask(system, listing)
    except Exception:
        log.exception("consolidation failed; keeping the merged map as-is")
        return ConceptMap(
            teaches=cmap.teaches,
            concepts=cmap.concepts[:MAX_CONCEPTS],
            links=cmap.links,
            contrasts=cmap.contrasts,
        )

    by_key = {normalize(c.name): c for c in cmap.concepts}
    rename: dict[str, str] = {}
    kept: list[Concept] = []
    for entry in decision.get("keep", []):
        canonical = by_key.get(normalize(entry.get("name", "")))
        if canonical is None:
            continue
        for other in entry.get("merge", []):
            duplicate = by_key.get(normalize(other))
            if duplicate is not None and duplicate is not canonical:
                canonical = _merge(canonical, duplicate)
                rename[normalize(other)] = canonical.name
        canonical.importance = entry.get("importance", canonical.importance)
        canonical.requires = entry.get("requires", canonical.requires)
        kept.append(canonical)

    if not kept:
        return cmap

    # Links written against a merged-away name have to follow it to its new one.
    for link in cmap.links:
        link.source = rename.get(normalize(link.source), link.source)
        link.target = rename.get(normalize(link.target), link.target)

    return tidy(
        ConceptMap(
            teaches=cmap.teaches, concepts=kept, links=cmap.links, contrasts=cmap.contrasts
        )
    )

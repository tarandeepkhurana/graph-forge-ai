"""Entity/relation types, the default ontology, and output cleanup.

The ontology is fixed rather than free-form for two reasons: it keeps Cypher
labels off user-controlled strings, and it gives the zero-shot model a closed
set to choose from instead of inventing relation names per chunk.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Ontology: suggestions, not a cage
#
# These lists were once enforced. That was wrong for entities: a clinical paper
# needs Drug and Dosage, a contract needs Clause and Party, and flattening them
# all into "concept" throws away the most useful thing the model noticed.
#
# The original reason for freezing them was that Cypher cannot parameterise a
# label or relationship type, so a model-invented type would mean building query
# text from model output. That no longer applies: relations are stored as
# `REL {type: "..."}` -- a parameterised property -- so a type is data, never
# query structure.
#
# **Entities are open.** The model names the type from the document's domain.
#
# **Relations lean on the core set below.** Not for safety, but for readability:
# left completely free, one relationship arrives as "uses", "utilises",
# "employs" and "makes use of" across four chunks, and edges stop being
# comparable or filterable. The model is asked to prefer these and coin a new
# one only when none fits.
# ---------------------------------------------------------------------------

# Suggested starting points. The model may return anything.
ENTITY_TYPES: list[str] = [
    "person",
    "organization",
    "location",
    "concept",
    "technology",
    "method",
    "metric",
    "event",
    "product",
    "date",
    "code_artifact",
]

# Strongly preferred, so the same relationship is not spelled four ways.
RELATION_TYPES: list[str] = [
    "is_a",
    "part_of",
    "uses",
    "causes",
    "created_by",
    "works_for",
    "located_in",
    "defined_as",
    "measures",
    "depends_on",
    "precedes",
    "example_of",
    "contradicts",
    "related_to",
]

# Node SHAPE carries entity type in the editor, not colour.
#
# Colour is spent on something scarcer and more useful: how much to trust an
# edge (draft / firm / inked). Eleven types cannot be given eleven
# distinguishable hues on a canvas where any node may sit beside any other --
# past about three, the pairs fall below the perceptual floor and the colours
# become decoration. Shape has no such ceiling, and every node also carries its
# label, so identity is never colour-alone.
ENTITY_SHAPES: dict[str, str] = {
    "person": "ellipse",
    "organization": "round-rectangle",
    "location": "diamond",
    "concept": "hexagon",
    "technology": "octagon",
    "method": "rhomboid",
    "metric": "barrel",
    "event": "triangle",
    "product": "tag",
    "date": "cut-rectangle",
    "code_artifact": "rectangle",
}
DEFAULT_SHAPE = "hexagon"

# Shapes an unfamiliar type can be given. Distinct enough to tell apart on a
# canvas; assignment is by a hash of the name, so "drug" is the same shape on
# every load and in every workspace.
_SPARE_SHAPES: list[str] = [
    "hexagon", "ellipse", "round-rectangle", "diamond", "octagon",
    "rhomboid", "barrel", "triangle", "tag", "cut-rectangle", "rectangle",
    "pentagon", "star", "vee", "concave-hexagon",
]

_TYPE_CHARS = re.compile(r"[^a-z0-9_]+")


def normalize_type(value: str | None, fallback: str = "concept") -> str:
    """Fold a model-supplied type into a stable key.

    "Drug Dosage", "drug-dosage" and "DRUG_DOSAGE" must be one type, or the
    resolution stage blocks them separately and never compares them.
    """
    if not value:
        return fallback
    key = _TYPE_CHARS.sub("_", str(value).strip().lower()).strip("_")
    return (key[:40] or fallback)


def shape_for(entity_type: str | None) -> str:
    """A canvas shape for any type, known or invented."""
    key = normalize_type(entity_type)
    if key in ENTITY_SHAPES:
        return ENTITY_SHAPES[key]
    # Deterministic, so a type keeps its shape between sessions.
    digest = int(hashlib.sha1(key.encode()).hexdigest()[:8], 16)
    return _SPARE_SHAPES[digest % len(_SPARE_SHAPES)]


class Entity(BaseModel):
    name: str
    type: str = "concept"

    def key(self) -> str:
        return f"{normalize(self.name)}|{self.type}"


class Triple(BaseModel):
    subject: str
    predicate: str
    object: str
    subject_type: str = "concept"
    object_type: str = "concept"
    confidence: float = 1.0
    # Which chunk this came from -- the grounding link the side panel needs.
    chunk_id: str | None = None
    # Where inside that chunk the model actually found each end, as character
    # offsets. The extractor reports these and they used to be discarded, which
    # is why a node could only ever show its whole chunk -- often thousands of
    # characters -- instead of the sentence it came from.
    subject_span: tuple[int, int] | None = None
    object_span: tuple[int, int] | None = None

    def key(self) -> tuple[str, str, str]:
        return (normalize(self.subject), normalize(self.predicate), normalize(self.object))

    def pretty(self) -> str:
        return f"({self.subject}) -[{self.predicate}]-> ({self.object})"


class ExtractionResult(BaseModel):
    """One extractor's output for one chunk, plus what it cost to produce."""

    chunk_id: str
    triples: list[Triple] = Field(default_factory=list)
    seconds: float = 0.0
    error: str | None = None

    @property
    def entities(self) -> list[Entity]:
        seen: dict[str, Entity] = {}
        for t in self.triples:
            for name, etype in ((t.subject, t.subject_type), (t.object, t.object_type)):
                e = Entity(name=name, type=etype)
                seen.setdefault(e.key(), e)
        return list(seen.values())


_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]")
_LEADING_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


def clean_triples(triples: list[Triple]) -> list[Triple]:
    """Shared post-processing applied to every extractor's raw output.

    Run for all candidates so the comparison stays fair -- these are failure modes
    of the extraction *format*, not of any one model, and fixing them centrally
    stops us crediting a model for cleanup we did ourselves.

    Observed in the first gliner-relex run on the sample document:
      * entity spans crossing a line break -> "Nobel Prize in Physics in\\n1903"
      * the same edge emitted twice at different confidences
      * "The extraction pipeline" and "extraction pipeline" as separate entities
      * self-referential edges like (The benchmark) -> (benchmark)

    All four are pure precision loss, and the editor would make the user delete
    them by hand. Cheaper to fix here.
    """
    best: dict[tuple[str, str, str], Triple] = {}

    for t in triples:
        subject = _clean_name(t.subject)
        object_ = _clean_name(t.object)
        if not subject or not object_ or len(subject) < 2 or len(object_) < 2:
            continue
        if normalize(subject) == normalize(object_):
            continue  # self-loop: no information

        # Offsets keep pointing at the original span in the document even when
        # the displayed name is tidied ("The benchmark" -> "benchmark"): the
        # name is for the canvas, the span is for finding it in the source.
        t = t.model_copy(update={"subject": subject, "object": object_})
        key = (normalize(subject), normalize(t.predicate), normalize(object_))
        # Same edge found twice -> keep the more confident reading.
        if key not in best or t.confidence > best[key].confidence:
            best[key] = t

    return list(best.values())


def _clean_name(name: str) -> str:
    """Collapse internal whitespace and drop a leading article."""
    return _LEADING_ARTICLE.sub("", _WS.sub(" ", name).strip()).strip()


def normalize(s: str) -> str:
    """Casefold, strip accents and punctuation, collapse whitespace.

    Used for both dedup and eval matching, so "Marie Curie" and "marie  curie."
    are the same string before rapidfuzz ever sees them.
    """
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _PUNCT.sub(" ", s.casefold())
    return _WS.sub(" ", s).strip()

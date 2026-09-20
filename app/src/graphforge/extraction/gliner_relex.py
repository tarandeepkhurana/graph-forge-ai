"""GLiNER-Relex -- joint zero-shot NER + relation extraction in one pass.

knowledgator/gliner-relex-large-v1.0, DeBERTa-v3-large backbone (~430M), CC BY 4.0.
The current front-runner: in its own 2026 paper it beat GPT-5-mini on DocRED
(31.3 vs 18.6 F1) and CrossRE, at roughly 70x the throughput. Zero-shot against
our ontology, so no fine-tuning needed to try it.
"""

from __future__ import annotations

from graphforge.extraction.base import Extractor, register
from graphforge.extraction.schema import ENTITY_TYPES, RELATION_TYPES, Triple

MODEL_ID = "knowledgator/gliner-relex-large-v1.0"

# Zero-shot models key off the wording of the label, so natural-language phrasings
# score better than our snake_case identifiers. We ask in English, store in ontology.
RELATION_PROMPTS = {
    "is a": "is_a",
    "part of": "part_of",
    "uses": "uses",
    "causes": "causes",
    "created by": "created_by",
    "works for": "works_for",
    "located in": "located_in",
    "defined as": "defined_as",
    "measures": "measures",
    "depends on": "depends_on",
    "precedes": "precedes",
    "example of": "example_of",
    "contradicts": "contradicts",
    "related to": "related_to",
}


@register("gliner_relex")
class GlinerRelexExtractor(Extractor):
    description = "knowledgator/gliner-relex-large-v1.0 joint NER+RE (~430M, CPU)"

    def __init__(self, threshold: float = 0.3, relation_threshold: float = 0.5) -> None:
        self.threshold = threshold
        self.relation_threshold = relation_threshold
        self._model = None

    def load(self) -> None:
        from gliner import GLiNER

        self._model = GLiNER.from_pretrained(MODEL_ID)

    def extract(self, text: str) -> list[Triple]:
        if self._model is None:
            self.load()

        _entities, relations = self._model.inference(
            texts=[text],
            labels=ENTITY_TYPES,
            relations=list(RELATION_PROMPTS),
            threshold=self.threshold,
            relation_threshold=self.relation_threshold,
            return_relations=True,
            flat_ner=False,
        )

        triples: list[Triple] = []
        for rel in relations[0] if relations else []:
            head, tail = rel.get("head", {}), rel.get("tail", {})
            if not head.get("text") or not tail.get("text"):
                continue
            triples.append(
                Triple(
                    subject=head["text"],
                    subject_type=_as_entity_type(head.get("type") or head.get("label")),
                    predicate=RELATION_PROMPTS.get(rel.get("relation", ""), "related_to"),
                    object=tail["text"],
                    object_type=_as_entity_type(tail.get("type") or tail.get("label")),
                    confidence=float(rel.get("score", 0.0)),
                    # The model reports exactly where it found each end. Keeping
                    # it is what lets the side panel open on the right sentence
                    # rather than the whole chunk.
                    subject_span=_span(head),
                    object_span=_span(tail),
                )
            )
        return triples


def _span(end: dict) -> tuple[int, int] | None:
    """Character offsets of one end of a relation, within the chunk text."""
    start, stop = end.get("start"), end.get("end")
    if start is None or stop is None or stop <= start:
        return None
    return int(start), int(stop)


def _as_entity_type(label: str | None) -> str:
    """Map a predicted label back onto the ontology; unknown labels become concept."""
    if not label:
        return "concept"
    key = label.strip().casefold().replace(" ", "_")
    return key if key in ENTITY_TYPES else "concept"


assert set(RELATION_PROMPTS.values()) <= set(RELATION_TYPES)

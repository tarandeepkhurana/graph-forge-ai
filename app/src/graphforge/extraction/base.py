"""The one interface every candidate implements.

Adding a candidate to the bake-off means writing one subclass and registering it.
Nothing else in the lab changes -- and in Phase 2 the winner is imported into the
app through this same interface, so swapping models later stays a one-line change.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod


from graphforge.extraction.schema import ExtractionResult, Triple, clean_triples

_REGISTRY: dict[str, type["Extractor"]] = {}


def register(key: str):
    def deco(cls: type["Extractor"]) -> type["Extractor"]:
        cls.key = key
        _REGISTRY[key] = cls
        return cls

    return deco


def available() -> dict[str, type["Extractor"]]:
    """Import every extractor module so the registry is populated."""
    from graphforge.extraction import (  # noqa: F401
        gliner_relex,
        openai_llm,
    )

    return dict(_REGISTRY)


def get(key: str) -> "Extractor":
    reg = available()
    if key not in reg:
        raise KeyError(f"unknown extractor {key!r}; available: {sorted(reg)}")
    return reg[key]()


class Extractor(ABC):
    """Turn a chunk of text into triples."""

    key: str = "base"
    # Set False for candidates that need a GPU, so `bench` can skip them locally.
    cpu_friendly: bool = True
    description: str = ""

    def load(self) -> None:
        """Load weights. Called once before the first extract; timed separately."""

    @abstractmethod
    def extract(self, text: str) -> list[Triple]:
        """Return triples for one chunk. Raise on failure; the runner records it."""

    def run(self, chunk_id: str, text: str) -> ExtractionResult:
        """Extract from one chunk, timed, never raising.

        A chunk that fails must not abort a 300-chunk ingestion job, so the
        error is recorded on the result and the pipeline carries on with the
        rest of the document.
        """
        started = time.perf_counter()
        try:
            # Cleanup is inside the timed section on purpose: it is part of the
            # real cost of using this extractor.
            triples = clean_triples(self.extract(text))
            error = None
        except Exception as exc:
            triples, error = [], f"{type(exc).__name__}: {exc}"
        seconds = time.perf_counter() - started

        # Stamp the source chunk on every triple: this is what the side panel
        # follows back to the original passage.
        for t in triples:
            t.chunk_id = chunk_id

        return ExtractionResult(
            chunk_id=chunk_id, triples=triples, seconds=seconds, error=error
        )

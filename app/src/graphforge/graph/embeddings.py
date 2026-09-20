"""Sentence embeddings for entity resolution.

Uses `sentence-transformers/all-MiniLM-L6-v2` (88 MB) through plain
`transformers`, which is already a dependency -- the `sentence-transformers`
package would add seven more for what amounts to a mean-pool over token vectors.

**What embeddings are and are not good for here.** Measured on a real workspace,
cosine similarity over bare entity names catches genuine duplicates that string
matching misses (`fork` ~ `forking` at 0.93) but also scores things that are
merely *about the same subject*:

    0.65   GraphForge  ~  Graph Databases     <- a product and a category

Merging those would corrupt the graph. So embeddings are used to *shortlist*,
never to decide on their own: high scores merge, a middle band is handed to a
language model for an actual identity judgement, and the rest is left alone.
"""

from __future__ import annotations

import logging
import threading

import numpy as np

log = logging.getLogger(__name__)

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
MAX_TOKENS = 32  # entity names are short; longer inputs are padding waste

_model = None
_tokenizer = None
_lock = threading.Lock()


def _load():
    """Load once per process, preferring the local cache.

    `local_files_only=True` is tried first because otherwise every load makes a
    round trip to the Hub to check for updates -- which turned a ~3 s warm start
    into 65 s on a slow connection.
    """
    global _model, _tokenizer
    with _lock:
        if _model is not None:
            return _tokenizer, _model

        import torch
        from transformers import AutoModel, AutoTokenizer

        for local_only in (True, False):
            try:
                _tokenizer = AutoTokenizer.from_pretrained(
                    MODEL_ID, local_files_only=local_only
                )
                _model = AutoModel.from_pretrained(
                    MODEL_ID, local_files_only=local_only
                ).eval()
                break
            except Exception:
                if local_only:
                    log.info("embedding model not cached; downloading %s", MODEL_ID)
                    continue
                raise

        torch.set_num_threads(max(1, (torch.get_num_threads() or 4) // 2))
        return _tokenizer, _model


def embed(texts: list[str]) -> np.ndarray:
    """Return L2-normalised embeddings, one row per input.

    Normalised so a dot product *is* the cosine similarity -- the whole
    similarity matrix is then a single matrix multiply.
    """
    if not texts:
        return np.zeros((0, 384), dtype=np.float32)

    import torch

    tokenizer, model = _load()
    encoded = tokenizer(
        texts, padding=True, truncation=True, max_length=MAX_TOKENS, return_tensors="pt"
    )
    with torch.no_grad():
        hidden = model(**encoded).last_hidden_state

    # Mean-pool over real tokens only; padding must not drag vectors toward zero.
    mask = encoded["attention_mask"].unsqueeze(-1).float()
    pooled = (hidden * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
    return torch.nn.functional.normalize(pooled, dim=1).numpy()


def similarity_matrix(texts: list[str]) -> np.ndarray:
    vectors = embed(texts)
    return vectors @ vectors.T

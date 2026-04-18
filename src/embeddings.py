"""Local text embedding with BAAI/bge-small-en-v1.5. Cross-platform, no API key."""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable

from fastembed import TextEmbedding

MODEL_NAME = "BAAI/bge-small-en-v1.5"
EMBED_DIM = 384


@lru_cache(maxsize=1)
def _model() -> TextEmbedding:
    return TextEmbedding(model_name=MODEL_NAME)


def embed(texts: Iterable[str]) -> list[list[float]]:
    return [list(map(float, vec)) for vec in _model().embed(list(texts))]

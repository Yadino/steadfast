"""Local text embedding with BAAI/bge-small-en-v1.5. Cross-platform, no API key."""

from __future__ import annotations

from functools import lru_cache
from typing import Iterable

from fastembed import TextEmbedding

from src.config import EMBED_DIM, EMBED_MODEL_NAME

# Re-export for modules that previously imported these from here.
MODEL_NAME = EMBED_MODEL_NAME
__all__ = ["EMBED_DIM", "MODEL_NAME", "embed"]


@lru_cache(maxsize=1)
def _model() -> TextEmbedding:
    return TextEmbedding(model_name=EMBED_MODEL_NAME)


def embed(texts: Iterable[str]) -> list[list[float]]:
    return [list(map(float, vec)) for vec in _model().embed(list(texts))]

"""Postgres connection helper. Reads DATABASE_URL from .env or environment."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import psycopg

DEFAULT_DSN = "postgresql://steadfast:steadfast@localhost:5433/steadfast"
_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_env_once() -> None:
    env_path = _REPO_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def dsn() -> str:
    _load_env_once()
    return os.getenv("DATABASE_URL", DEFAULT_DSN)


@contextmanager
def connect() -> Iterator[psycopg.Connection]:
    with psycopg.connect(dsn()) as conn:
        yield conn


def vector_literal(values: list[float]) -> str:
    """Format a Python list as a pgvector text literal."""
    return "[" + ",".join(f"{v:.7g}" for v in values) + "]"

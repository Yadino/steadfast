"""
Central configuration for the Steadfast pipeline.

Every tunable (LLM model, proxy URL, temperatures, top-k, embedding model,
batch sizes, etc.) lives here. Values can be overridden via environment
variables or a `.env` file at the repo root. Consumers import constants
directly, e.g.:

    from src.config import LLM_MODEL, TOP_K, proxy_config

and build a proxy client with `proxy_config()` or `proxy_config(RESPONSE_MODEL)`.
"""

from __future__ import annotations

import os
from pathlib import Path

from tools.proxy_chat import ProxyConfig, load_env_file

REPO_ROOT = Path(__file__).resolve().parent.parent
load_env_file(REPO_ROOT / ".env")


def _get_str(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v is not None and v != "" else default


def _get_int(name: str, default: int) -> int:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return int(v)
    except ValueError:
        return default


def _get_float(name: str, default: float) -> float:
    v = os.getenv(name)
    if v is None or v == "":
        return default
    try:
        return float(v)
    except ValueError:
        return default


def _get_optional_float(name: str, default: float | None) -> float | None:
    """Like _get_float, but returns None if the env var is set to one of
    'off', 'none', 'null', or 'disable' (case-insensitive). Use this for
    knobs that some models refuse to accept (e.g. temperature on
    claude-opus-4-7)."""
    v = os.getenv(name)
    if v is None:
        return default
    stripped = v.strip()
    if stripped == "":
        return default
    if stripped.lower() in ("off", "none", "null", "disable", "disabled"):
        return None
    try:
        return float(stripped)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# LLM proxy
# ---------------------------------------------------------------------------

LLM_BASE_URL: str = _get_str(
    "ANTHROPIC_BASE_URL",
    _get_str(
        "LSP_BASE_URL",
        _get_str(
            "OPENAI_BASE_URL",
            "https://lsp-proxy.cave.latent.build/v1",
        ),
    ),
).rstrip("/")

LLM_API_KEY: str = _get_str(
    "ANTHROPIC_API_KEY",
    _get_str("LSP_API_KEY", _get_str("OPENAI_API_KEY", "")),
)

LLM_MODEL: str = _get_str("LLM_MODEL", "claude-sonnet-4-6")

# Optional override: run the customer-facing response generation on a
# different (e.g. higher-quality) model than the rest of the pipeline.
# Falls back to LLM_MODEL if unset.
RESPONSE_MODEL: str = _get_str("RESPONSE_MODEL", LLM_MODEL)

LLM_TIMEOUT_S: float = _get_float("LLM_TIMEOUT_S", 90.0)

# Per-stage sampling temperatures. Set a stage's temperature to "off" in
# .env (e.g. TEMPERATURE_RESPONSE=off) to omit the field entirely — some
# newer models (claude-opus-4-7) deprecated the parameter and reject it.
# TEMPERATURE_RESPONSE defaults to None because the response stage runs on
# RESPONSE_MODEL, which is typically opus and doesn't accept temperature.
TEMPERATURE_CLASSIFY: float | None = _get_optional_float("TEMPERATURE_CLASSIFY", 0.0)
TEMPERATURE_RETRIEVAL_QUERY: float | None = _get_optional_float(
    "TEMPERATURE_RETRIEVAL_QUERY", 0.0
)
TEMPERATURE_RESPONSE: float | None = _get_optional_float("TEMPERATURE_RESPONSE", None)

# ---------------------------------------------------------------------------
# Retrieval / RAG
# ---------------------------------------------------------------------------

TOP_K: int = _get_int("TOP_K", 5)

# ---------------------------------------------------------------------------
# Embeddings (local fastembed / ONNX)
# ---------------------------------------------------------------------------

EMBED_MODEL_NAME: str = _get_str("EMBED_MODEL_NAME", "BAAI/bge-small-en-v1.5")
EMBED_DIM: int = _get_int("EMBED_DIM", 384)

# ---------------------------------------------------------------------------
# KB seed (scripts/seed_kb.py)
# ---------------------------------------------------------------------------

SEED_BATCH_SIZE: int = _get_int("SEED_BATCH_SIZE", 64)

# ---------------------------------------------------------------------------
# KB audit (tools/llm_kb_audit.py)
# ---------------------------------------------------------------------------

AUDIT_BATCH_SIZE: int = _get_int("AUDIT_BATCH_SIZE", 20)
AUDIT_BATCH_DELAY_S: float = _get_float("AUDIT_BATCH_DELAY_S", 0.2)
AUDIT_MIN_CONFIDENCE: float = _get_float("AUDIT_MIN_CONFIDENCE", 0.8)
AUDIT_TEMPERATURE: float = _get_float("AUDIT_TEMPERATURE", 0.0)


def proxy_config(model: str | None = None) -> ProxyConfig:
    """Build a ProxyConfig from the central settings.

    Pass `model=RESPONSE_MODEL` (or any other override) if a specific
    stage wants a non-default model. Defaults to `LLM_MODEL`.
    """
    if not LLM_API_KEY:
        raise RuntimeError(
            "Missing API key. Set ANTHROPIC_API_KEY (or LSP_API_KEY / "
            "OPENAI_API_KEY) in .env or the environment."
        )
    return ProxyConfig(
        api_key=LLM_API_KEY,
        base_url=LLM_BASE_URL,
        model=model or LLM_MODEL,
        timeout_s=LLM_TIMEOUT_S,
    )

"""Stage 3: LLM classification + RAG-grounded response generation."""

from __future__ import annotations

import json
from typing import Any

from src.config import (
    RESPONSE_MODEL,
    TEMPERATURE_CLASSIFY,
    TEMPERATURE_RESPONSE,
    TEMPERATURE_RETRIEVAL_QUERY,
    TOP_K,
    proxy_config,
)
from src.db import connect, vector_literal
from src.embeddings import embed
from src.prompts import (
    CLASSIFICATION_SYSTEM_PROMPT,
    RETRIEVAL_QUERY_SYSTEM_PROMPT,
    RESPONSE_SYSTEM_PROMPT,
)
from tools.proxy_chat import complete_chat

ALLOWED_CATEGORIES = {
    "billing", "bug", "feature_request", "account",
    "integration", "onboarding", "security", "performance",
}
ALLOWED_PRIORITIES = {"low", "medium", "high", "critical"}
ALLOWED_MODES = {"answer_found", "needs_human_check", "no_relevant_answer"}

_cfg = None
_response_cfg = None


def _cfg_once():
    global _cfg
    if _cfg is None:
        _cfg = proxy_config()
    return _cfg


def _response_cfg_once():
    """Separate cfg so the response stage can use a different model
    (e.g. opus) without changing the model used by classify / retrieval-query."""
    global _response_cfg
    if _response_cfg is None:
        _response_cfg = proxy_config(RESPONSE_MODEL)
    return _response_cfg


def _extract_json(text: str) -> dict[str, Any]:
    """Parse JSON from an LLM reply, tolerating code fences or leading prose."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            return json.loads(text[start : end + 1])
        raise


def _clamp01(x: Any) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, v))


def classify(ticket: dict) -> dict:
    user = (
        f"[{ticket.get('ticket_id','')}] subject: {ticket.get('subject','')}\n"
        f"body: {ticket.get('body','')}"
    )
    text = complete_chat(
        _cfg_once(),
        [
            {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=TEMPERATURE_CLASSIFY,
    )
    try:
        data = _extract_json(text)
    except json.JSONDecodeError:
        return {
            "category": "unknown",
            "priority": "low",
            "classification_confidence": 0.0,
            "classification_flags": ["escalate_to_human"],
            "raw": text,
        }

    category = data.get("category") or "unknown"
    priority = data.get("priority") or "low"
    if category not in ALLOWED_CATEGORIES:
        category = "unknown"
    if priority not in ALLOWED_PRIORITIES:
        priority = "low"

    flags = [f for f in (data.get("flags") or []) if isinstance(f, str)]
    return {
        "category": category,
        "priority": priority,
        "classification_confidence": _clamp01(data.get("confidence", 0.0)),
        "classification_flags": flags,
    }


def build_retrieval_query(ticket: dict, classification: dict) -> str:
    payload = json.dumps(
        {
            "subject": ticket.get("subject", ""),
            "body": ticket.get("body", ""),
            "category": classification.get("category"),
            "priority": classification.get("priority"),
        },
        ensure_ascii=False,
    )
    text = complete_chat(
        _cfg_once(),
        [
            {"role": "system", "content": RETRIEVAL_QUERY_SYSTEM_PROMPT},
            {"role": "user", "content": payload},
        ],
        temperature=TEMPERATURE_RETRIEVAL_QUERY,
    )
    return text.strip().strip('"').strip("`").strip()


def retrieve(query: str, k: int = TOP_K) -> list[dict]:
    vec = embed([query])[0]
    lit = vector_literal(vec)
    with connect() as conn, conn.cursor() as cur:
        cur.execute(
            """
            SELECT ticket_id, category, priority, subject, body, resolution,
                   1 - (embedding <=> %s::vector) AS score
            FROM kb_tickets
            ORDER BY embedding <=> %s::vector
            LIMIT %s;
            """,
            (lit, lit, k),
        )
        rows = cur.fetchall()
        cols = [d.name for d in cur.description]
    return [dict(zip(cols, r)) for r in rows]


def _format_retrieved(retrieved: list[dict]) -> str:
    chunks = []
    for r in retrieved:
        chunks.append(
            f"[{r['ticket_id']}] (category={r['category']}, "
            f"priority={r['priority']}, score={r['score']:.3f})\n"
            f"Subject: {r['subject']}\n"
            f"Body: {r['body']}\n"
            f"Resolution: {r['resolution']}"
        )
    return "\n\n".join(chunks) if chunks else "(none)"


def generate_response(
    ticket: dict, classification: dict, retrieved: list[dict]
) -> dict:
    payload = {
        "ticket": {
            "ticket_id": ticket.get("ticket_id", ""),
            "subject": ticket.get("subject", ""),
            "body": ticket.get("body", ""),
            "customer_name": ticket.get("customer_name"),
            "plan": ticket.get("plan"),
        },
        "classification": {
            "category": classification.get("category"),
            "priority": classification.get("priority"),
        },
        "retrieved_kb": _format_retrieved(retrieved),
    }
    text = complete_chat(
        _response_cfg_once(),
        [
            {"role": "system", "content": RESPONSE_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        temperature=TEMPERATURE_RESPONSE,
    )
    try:
        data = _extract_json(text)
    except json.JSONDecodeError:
        return {
            "response": text.strip(),
            "confidence": 0.2,
            "mode": "no_relevant_answer",
            "flags": ["escalate_to_human"],
        }

    mode = data.get("mode") or "no_relevant_answer"
    if mode not in ALLOWED_MODES:
        mode = "no_relevant_answer"
    flags = [f for f in (data.get("flags") or []) if isinstance(f, str)]
    return {
        "response": (data.get("response") or "").strip(),
        "confidence": _clamp01(data.get("confidence", 0.0)),
        "mode": mode,
        "flags": flags,
    }

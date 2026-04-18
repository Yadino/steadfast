"""
Stage 6: Evaluation.

Two kinds of metrics:
  1. Label accuracy: exact match of `category` and `priority` against
     `expected_category` / `expected_priority` on the input ticket
     (present in data/eval_set.json).
  2. Response quality: an LLM judge scores the generated reply against
     the ticket and the retrieved evidence (no expected labels needed).

Plus aggregation helpers used by the API UI (running totals) and the
CLI (final report + error analysis).
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from src.agent import _cfg_once, _clamp01, _extract_json, _format_retrieved
from src.prompts import RESPONSE_JUDGE_SYSTEM_PROMPT
from tools.proxy_chat import complete_chat

# Judge always uses a deterministic temperature. Kept separate from
# TEMPERATURE_CLASSIFY so we can tune it later without affecting classify.
JUDGE_TEMPERATURE: float = 0.0


def has_expected_labels(ticket: dict) -> bool:
    """Does this ticket carry gold labels we can compare against?"""
    return bool(ticket.get("expected_category") or ticket.get("expected_priority"))


def dataset_has_expected_labels(tickets: list[dict]) -> bool:
    """True if any ticket in the dataset has expected_category/priority."""
    return any(has_expected_labels(t) for t in tickets)


def check_labels(ticket: dict, classification: dict) -> dict:
    """Compare predicted labels to the ticket's expected_* fields.
    Each `*_correct` is True/False when the expected label is present,
    otherwise None."""
    exp_cat = ticket.get("expected_category")
    exp_pri = ticket.get("expected_priority")
    pred_cat = classification.get("category")
    pred_pri = classification.get("priority")
    return {
        "expected_category": exp_cat,
        "expected_priority": exp_pri,
        "category_correct": (exp_cat == pred_cat) if exp_cat else None,
        "priority_correct": (exp_pri == pred_pri) if exp_pri else None,
    }


def judge_response(ticket: dict, retrieved: list[dict], response: str) -> dict:
    """LLM judge: score the generated reply against ticket + retrieved KB.
    Returns {"score": 0..1, "reason": str}. Never raises."""
    payload = {
        "ticket": {
            "subject": ticket.get("subject", ""),
            "body": ticket.get("body", ""),
        },
        "retrieved_kb": _format_retrieved(retrieved),
        "response": response or "",
    }
    try:
        text = complete_chat(
            _cfg_once(),
            [
                {"role": "system", "content": RESPONSE_JUDGE_SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            temperature=JUDGE_TEMPERATURE,
        )
        data = _extract_json(text)
    except Exception as exc:
        return {"score": 0.0, "reason": f"(judge error: {type(exc).__name__})"}
    return {
        "score": _clamp01(data.get("score", 0.0)),
        "reason": str(data.get("reason", "")).strip(),
    }


def evaluate_ticket(
    ticket: dict, classification: dict, retrieved: list[dict], response: str
) -> dict:
    """Full per-ticket evaluation: label checks + LLM judge."""
    labels = check_labels(ticket, classification)
    judge = judge_response(ticket, retrieved, response)
    return {
        **labels,
        "response_score": judge["score"],
        "response_score_reason": judge["reason"],
    }


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _acc(correct: int, total: int) -> float | None:
    if total <= 0:
        return None
    return round(correct / total, 4)


def summarize(results: list[dict]) -> dict:
    """Aggregate per-ticket results into a report.

    `results` is a list of per-ticket dicts produced by the pipeline;
    each should contain the final fields (category/priority/response)
    plus an `evaluation` sub-object when eval mode was on.
    """
    n = len(results)
    n_cat_total = n_cat_ok = 0
    n_pri_total = n_pri_ok = 0
    n_judge = 0
    sum_judge = 0.0

    by_cat_total: dict[str, int] = defaultdict(int)
    by_cat_ok: dict[str, int] = defaultdict(int)
    by_pri_total: dict[str, int] = defaultdict(int)
    by_pri_ok: dict[str, int] = defaultdict(int)

    for r in results:
        ev = r.get("evaluation") or {}
        cat_ok = ev.get("category_correct")
        pri_ok = ev.get("priority_correct")
        exp_cat = ev.get("expected_category")
        exp_pri = ev.get("expected_priority")

        if cat_ok is not None and exp_cat:
            n_cat_total += 1
            by_cat_total[exp_cat] += 1
            if cat_ok:
                n_cat_ok += 1
                by_cat_ok[exp_cat] += 1
        if pri_ok is not None and exp_pri:
            n_pri_total += 1
            by_pri_total[exp_pri] += 1
            if pri_ok:
                n_pri_ok += 1
                by_pri_ok[exp_pri] += 1

        score = ev.get("response_score")
        if isinstance(score, (int, float)):
            n_judge += 1
            sum_judge += float(score)

    return {
        "n": n,
        "category_accuracy": _acc(n_cat_ok, n_cat_total),
        "priority_accuracy": _acc(n_pri_ok, n_pri_total),
        "response_score_avg": round(sum_judge / n_judge, 4) if n_judge else None,
        "category_accuracy_n": n_cat_total,
        "priority_accuracy_n": n_pri_total,
        "response_score_n": n_judge,
        "by_category": {
            k: {
                "total": by_cat_total[k],
                "correct": by_cat_ok[k],
                "accuracy": _acc(by_cat_ok[k], by_cat_total[k]),
            }
            for k in sorted(by_cat_total)
        },
        "by_priority": {
            k: {
                "total": by_pri_total[k],
                "correct": by_pri_ok[k],
                "accuracy": _acc(by_pri_ok[k], by_pri_total[k]),
            }
            for k in sorted(by_pri_total)
        },
    }


def error_analysis(results: list[dict]) -> dict:
    """Pull out misclassifications and low-scoring responses for Stage 7."""
    cat_mismatches: list[dict[str, Any]] = []
    pri_mismatches: list[dict[str, Any]] = []
    low_scores: list[dict[str, Any]] = []

    for r in results:
        ev = r.get("evaluation") or {}
        base = {
            "ticket_id": r.get("ticket_id"),
            "subject": r.get("subject"),
        }
        if ev.get("category_correct") is False:
            cat_mismatches.append(
                {
                    **base,
                    "expected": ev.get("expected_category"),
                    "predicted": r.get("category"),
                }
            )
        if ev.get("priority_correct") is False:
            pri_mismatches.append(
                {
                    **base,
                    "expected": ev.get("expected_priority"),
                    "predicted": r.get("priority"),
                }
            )
        score = ev.get("response_score")
        if isinstance(score, (int, float)) and score < 0.5:
            low_scores.append(
                {
                    **base,
                    "score": score,
                    "reason": ev.get("response_score_reason"),
                    "response": r.get("response"),
                }
            )

    return {
        "category_mismatches": cat_mismatches,
        "priority_mismatches": pri_mismatches,
        "low_score_responses": sorted(low_scores, key=lambda x: x["score"]),
    }

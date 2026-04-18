"""
Stage 4: Validate LLM output.

Checks every assembled per-ticket result for:
  - required fields present (ticket_id, category, priority, response)
  - `category` and `priority` are allowed enum values
  - `response` is a non-empty string
  - `confidence` (if present) is a float in [0, 1]
  - `flags` (if present) is a list of strings

On failure, falls back to safe defaults (`category="unknown"`,
`priority="low"`) and adds an `escalate_to_human` flag so a human can
review. Returns the sanitized dict plus a list of issue strings.

`failure_rate(results)` computes the fraction of validated results that
had at least one issue -- use it in eval reports.
"""

from __future__ import annotations

from typing import Any

ALLOWED_CATEGORIES = {
    "billing", "bug", "feature_request", "account",
    "integration", "onboarding", "security", "performance",
}
ALLOWED_PRIORITIES = {"low", "medium", "high", "critical"}

REQUIRED_FIELDS = ("ticket_id", "category", "priority", "response")


def validate_output(final: Any) -> tuple[dict, list[str]]:
    """Validate one assembled per-ticket result.

    Returns (sanitized_dict, issues). `issues` is empty iff the input was
    already valid. On any issue we fall back to safe defaults and add an
    `escalate_to_human` flag.
    """
    issues: list[str] = []

    if not isinstance(final, dict):
        return (
            {
                "ticket_id": "",
                "category": "unknown",
                "priority": "low",
                "response": "",
                "confidence": 0.0,
                "flags": ["escalate_to_human"],
            },
            ["not_a_dict"],
        )

    out: dict[str, Any] = dict(final)

    for field in REQUIRED_FIELDS:
        if field not in out:
            issues.append(f"missing_{field}")

    ticket_id = out.get("ticket_id", "")
    if not isinstance(ticket_id, str) or not ticket_id.strip():
        issues.append("invalid_ticket_id")
        out["ticket_id"] = str(ticket_id or "")

    category = out.get("category")
    if category not in ALLOWED_CATEGORIES:
        issues.append("invalid_category")
        out["category"] = "unknown"

    priority = out.get("priority")
    if priority not in ALLOWED_PRIORITIES:
        issues.append("invalid_priority")
        out["priority"] = "low"

    response = out.get("response")
    if not isinstance(response, str) or not response.strip():
        issues.append("empty_response")
        out["response"] = ""

    if "confidence" in out:
        try:
            c = float(out["confidence"])
        except (TypeError, ValueError):
            issues.append("invalid_confidence")
            c = 0.0
        out["confidence"] = max(0.0, min(1.0, c))

    flags = out.get("flags") or []
    if not isinstance(flags, list) or not all(isinstance(f, str) for f in flags):
        issues.append("invalid_flags")
        flags = [f for f in (flags if isinstance(flags, list) else []) if isinstance(f, str)]
    if issues and "escalate_to_human" not in flags:
        flags.append("escalate_to_human")
    out["flags"] = sorted(set(flags))

    return out, issues


def failure_rate(results: list[dict]) -> dict:
    """Aggregate validation stats across a batch of pipeline results.

    Each `result` may be a raw final dict, or may carry a `validation`
    sub-dict produced by `validate_output` (see pipeline wiring). A result
    is counted as a failure if its validation issue list is non-empty.
    """
    total = len(results)
    failed = 0
    issue_counts: dict[str, int] = {}
    for r in results:
        issues = (r.get("validation") or {}).get("issues") if isinstance(r, dict) else None
        if issues is None:
            _, issues = validate_output(r)
        if issues:
            failed += 1
            for i in issues:
                issue_counts[i] = issue_counts.get(i, 0) + 1
    return {
        "total": total,
        "failed": failed,
        "failure_rate": (failed / total) if total else 0.0,
        "issues": dict(sorted(issue_counts.items(), key=lambda kv: -kv[1])),
    }

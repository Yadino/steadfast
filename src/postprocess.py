"""
Stage 5: Heuristic post-processing on top of the LLM pipeline.

The classifier tends to over-escalate priority. Two cheap rules over
the lowercased customer message fix the common cases:

  1. Question-shaped tickets get capped at `low`.
  2. `high`/`critical` tickets without any urgency word get downgraded
     to `medium`.
"""

from __future__ import annotations

URGENCY_SIGNALS = (
    "blocking", "blocked", "can't", "cannot", "unable", "locked out",
    "urgent", "asap", "immediately", "outage", "down", "broken",
    "critical", "deadline", "breach", "data loss",
)

QUESTION_SIGNALS = (
    "how do", "how can", "curious", "wondering", "question about",
    "where do i find", "is it possible", "could you explain",
)


def postprocess(ticket: dict, final: dict) -> tuple[dict, list[str]]:
    """Return (adjusted_final, adjustments). Never raises priority."""
    out = dict(final)
    text = f"{ticket.get('subject', '')} {ticket.get('body', '')}".lower()
    priority = out.get("priority", "low")
    adjustments: list[str] = []

    if priority != "low" and any(s in text for s in QUESTION_SIGNALS):
        adjustments.append(f"question_cap: {priority} -> low")
        priority = "low"

    if priority in ("high", "critical") and not any(s in text for s in URGENCY_SIGNALS):
        adjustments.append(f"no_urgency_downgrade: {priority} -> medium")
        priority = "medium"

    out["priority"] = priority
    return out, adjustments

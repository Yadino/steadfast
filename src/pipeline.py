"""
Steadfast Support Ticket Triage Pipeline — thin orchestrator.

Runs on any CSV or JSON of input tickets. Each ticket must have at least
`ticket_id`, `subject`, and `body` (and may optionally have `customer_name`
and `plan`). Produces the assignment output per ticket:

  {"ticket_id", "category", "priority", "response", "confidence", "flags"}

Usage:
  python -m src.pipeline --input data/eval_set.json --limit 5
  python -m src.pipeline --input data/knowledge_base.csv --limit 3 --internal
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.agent import (  # noqa: E402
    build_retrieval_query,
    classify,
    generate_response,
    retrieve,
)

DEFAULT_INPUT = REPO_ROOT / "data/eval_set.json"


def process_ticket(ticket: dict) -> dict:
    """Run the full pipeline on one ticket. Returns {"final": ..., "internal": ...}."""
    classification = classify(ticket)
    query = build_retrieval_query(ticket, classification)
    retrieved = retrieve(query, k=5)
    resp = generate_response(ticket, classification, retrieved)

    flags = sorted(set(classification["classification_flags"] + resp["flags"]))

    final = {
        "ticket_id": ticket.get("ticket_id", ""),
        "category": classification["category"],
        "priority": classification["priority"],
        "response": resp["response"],
        "confidence": resp["confidence"],
        "flags": flags,
    }
    internal = {
        **final,
        "classification_confidence": classification["classification_confidence"],
        "response_mode": resp["mode"],
        "retrieval_query": query,
        "retrieved": [
            {
                "ticket_id": r["ticket_id"],
                "category": r["category"],
                "priority": r["priority"],
                "subject": r["subject"],
                "score": round(float(r["score"]), 4),
            }
            for r in retrieved
        ],
    }
    return {"final": final, "internal": internal}


def load_tickets(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--internal",
        action="store_true",
        help="Print the internal (debug) object instead of the assignment output.",
    )
    args = ap.parse_args()

    tickets = load_tickets(args.input)
    if args.limit:
        tickets = tickets[: args.limit]

    print(f"Processing {len(tickets)} tickets from {args.input}", file=sys.stderr)
    for t in tickets:
        result = process_ticket(t)
        out = result["internal"] if args.internal else result["final"]
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()

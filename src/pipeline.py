"""
Steadfast Support Ticket Triage Pipeline — thin orchestrator.

Runs on any CSV or JSON of input tickets. Each ticket must have at least
`ticket_id`, `subject`, and `body` (and may optionally have `customer_name`
and `plan`). Produces the assignment output per ticket:

  {"ticket_id", "category", "priority", "response", "confidence", "flags"}

Usage:
  # Just run the pipeline on the eval set, print one JSON object per line:
  python -m src.pipeline --input data/eval_set.json --limit 5

  # Run + evaluate (category/priority accuracy + LLM response judge),
  # write output/eval_results.json and output/error_analysis.json:
  python -m src.pipeline --eval

  # Inspect internals (retrieval, chunks, evaluation):
  python -m src.pipeline --input data/knowledge_base_fixed.csv --limit 3 --internal
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
from src.evaluate import (  # noqa: E402
    dataset_has_expected_labels,
    error_analysis,
    evaluate_ticket,
    summarize,
)
from src.postprocess import postprocess  # noqa: E402
from src.validate import failure_rate as validation_failure_rate  # noqa: E402
from src.validate import validate_output  # noqa: E402

DEFAULT_INPUT = REPO_ROOT / "data/eval_set.json"
OUTPUT_DIR = REPO_ROOT / "output"


def process_ticket(ticket: dict, *, evaluate: bool = False) -> dict:
    """Run the full pipeline on one ticket. Returns {"final": ..., "internal": ...}.

    When `evaluate=True`, also runs Stage 6 (label check + LLM judge) and
    attaches the result as `evaluation` on the internal payload.
    """
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

    final, postprocess_adjustments = postprocess(ticket, final)
    final, validation_issues = validate_output(final)

    internal = {
        **final,
        "subject": ticket.get("subject", ""),
        "classification_confidence": classification["classification_confidence"],
        "response_mode": resp["mode"],
        "postprocess": {"adjustments": postprocess_adjustments},
        "validation": {"issues": validation_issues, "ok": not validation_issues},
        "retrieval_query": query,
        "retrieved": [
            {
                "ticket_id": r["ticket_id"],
                "category": r["category"],
                "priority": r["priority"],
                "subject": r["subject"],
                "body": r.get("body", ""),
                "resolution": r.get("resolution", ""),
                "score": round(float(r["score"]), 4),
            }
            for r in retrieved
        ],
    }
    if evaluate:
        internal["evaluation"] = evaluate_ticket(
            ticket, classification, retrieved, resp["response"]
        )
    return {"final": final, "internal": internal}


def load_tickets(path: Path) -> list[dict]:
    if path.suffix.lower() == ".json":
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _run(tickets: list[dict], *, evaluate: bool) -> list[dict]:
    """Run the pipeline and collect internal records (used by --eval)."""
    out = []
    for i, t in enumerate(tickets, 1):
        try:
            internal = process_ticket(t, evaluate=evaluate)["internal"]
        except Exception as exc:
            internal = {
                "ticket_id": t.get("ticket_id", ""),
                "subject": t.get("subject", ""),
                "error": f"{type(exc).__name__}: {exc}",
            }
        out.append(internal)
        print(f"[{i}/{len(tickets)}] {internal.get('ticket_id')}", file=sys.stderr)
    return out


def _write_eval_outputs(results: list[dict]) -> tuple[Path, Path]:
    OUTPUT_DIR.mkdir(exist_ok=True)
    summary = summarize(results)
    summary["validation"] = validation_failure_rate(results)
    errors = error_analysis(results)

    eval_path = OUTPUT_DIR / "eval_results.json"
    with eval_path.open("w", encoding="utf-8") as f:
        json.dump({"summary": summary, "results": results}, f, ensure_ascii=False, indent=2)

    err_path = OUTPUT_DIR / "error_analysis.json"
    with err_path.open("w", encoding="utf-8") as f:
        json.dump(errors, f, ensure_ascii=False, indent=2)

    return eval_path, err_path


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument(
        "--internal",
        action="store_true",
        help="Print the internal (debug) object instead of the assignment output.",
    )
    ap.add_argument(
        "--eval",
        action="store_true",
        help="Run Stage 6 evaluation and write output/eval_results.json "
        "+ output/error_analysis.json.",
    )
    args = ap.parse_args()

    tickets = load_tickets(args.input)
    if args.limit:
        tickets = tickets[: args.limit]

    print(f"Processing {len(tickets)} tickets from {args.input}", file=sys.stderr)

    if args.eval:
        has_gold = dataset_has_expected_labels(tickets)
        if not has_gold:
            print(
                "note: dataset has no expected_category/expected_priority; "
                "label accuracy will be skipped but the LLM judge will still run.",
                file=sys.stderr,
            )
        results = _run(tickets, evaluate=True)
        eval_path, err_path = _write_eval_outputs(results)
        summary = summarize(results)
        summary["validation"] = validation_failure_rate(results)
        print("\n=== Evaluation summary ===", file=sys.stderr)
        print(json.dumps(summary, indent=2), file=sys.stderr)
        print(f"wrote {eval_path.relative_to(REPO_ROOT)}", file=sys.stderr)
        print(f"wrote {err_path.relative_to(REPO_ROOT)}", file=sys.stderr)
        return

    for t in tickets:
        result = process_ticket(t)
        out = result["internal"] if args.internal else result["final"]
        print(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()

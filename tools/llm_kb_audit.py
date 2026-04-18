"""
LLM-assisted KB audit: Claude via proxy POST .../chat/completions (httpx).

Input:  <repo>/data/knowledge_base.csv
Output: <repo>/data/knowledge_base_llm_flagged.csv

The output CSV keeps the original columns and appends audit columns such as
suspect flags, suggested labels, confidence, reason, and the model used.
Paths are fixed relative to this file's repo root unless overridden by CLI args.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from pathlib import Path
from typing import Any

from proxy_chat import ProxyConfig, complete_chat, load_proxy_config

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KB_CSV = _REPO_ROOT / "data/knowledge_base.csv"
DEFAULT_OUTPUT_CSV = _REPO_ROOT / "data/knowledge_base_llm_flagged.csv"
DEFAULT_BATCH_SIZE = 20
DEFAULT_BATCH_DELAY_S = 0.2

DEFAULT_BASE_URL = "https://lsp-proxy.cave.latent.build/v1"
DEFAULT_LLM_MODEL = "claude-sonnet-4-6"
DEFAULT_LLM_TIMEOUT_S = 90
DEFAULT_LLM_TEMPERATURE = 0.0
DEFAULT_MIN_SUSPECT_CONFIDENCE = 0.8

ALLOWED_CATEGORIES = (
    "billing",
    "bug",
    "feature_request",
    "account",
    "integration",
    "onboarding",
    "security",
    "performance",
)
ALLOWED_PRIORITIES = ("low", "medium", "high", "critical")
PRIORITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}

LLM_AUDIT_COLUMNS = (
    "suspect_by_llm",
    "suspect_category",
    "suspect_priority",
    "suggested_category",
    "suggested_priority",
    "suspect_confidence",
    "suspect_reason",
    "llm_model",
)


def _resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else (_REPO_ROOT / path)


def _proxy_config(model_override: str | None = None) -> ProxyConfig:
    cfg = load_proxy_config(
        default_base_url=DEFAULT_BASE_URL,
        default_model=DEFAULT_LLM_MODEL,
        default_timeout_s=float(DEFAULT_LLM_TIMEOUT_S),
    )
    if not model_override:
        return cfg
    return ProxyConfig(
        api_key=cfg.api_key,
        base_url=cfg.base_url,
        model=model_override,
        timeout_s=cfg.timeout_s,
    )


def build_batch_user_content(rows: list[dict[str, str]]) -> str:
    rows_payload = [
        {
            "ticket_id": row.get("ticket_id", ""),
            "category": row.get("category", ""),
            "priority": row.get("priority", ""),
            "subject": row.get("subject", ""),
            "body": row.get("body", ""),
            "resolution": row.get("resolution", ""),
        }
        for row in rows
    ]
    instructions = (
        "You are auditing historical support tickets for label quality. "
        "Return strict JSON only.\n\n"
        "Be strict: only list tickets where the assigned category or priority is clearly inconsistent "
        "with the subject/body/resolution. Skip anything defensible, ambiguous, or merely debatable. "
        "An empty suspect_rows list is normal.\n\n"
        "If you flag a category problem, include suggested_category using one of these values: "
        + ", ".join(ALLOWED_CATEGORIES)
        + ". "
        "If you flag a priority problem, include suggested_priority using one of these values: "
        + ", ".join(ALLOWED_PRIORITIES)
        + ". "
        "If a label is not suspected, leave the corresponding suggested_* field empty.\n\n"
        "Return a JSON object with key suspect_rows only. "
        "Each suspect row must contain: ticket_id (string), suspect_category (bool), "
        "suspect_priority (bool), suggested_category (string), suggested_priority (string), "
        "confidence (0-1), reason (one short sentence).\n\n"
        "Data (JSON):\n"
    )
    return instructions + json.dumps({"rows": rows_payload}, ensure_ascii=False)


def _normalized_label(value: Any, allowed: tuple[str, ...]) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else ""


def parse_suspects(raw_content: str) -> list[dict[str, Any]]:
    candidate = raw_content.strip()
    if "```" in candidate:
        candidate = candidate.replace("```json", "").replace("```", "").strip()
    data = json.loads(candidate)
    suspects = data.get("suspect_rows", [])
    if not isinstance(suspects, list):
        return []
    out: list[dict[str, Any]] = []
    for item in suspects:
        if not isinstance(item, dict):
            continue
        ticket_id = str(item.get("ticket_id", "")).strip()
        if not ticket_id:
            continue
        out.append(
            {
                "ticket_id": ticket_id,
                "suspect_category": bool(item.get("suspect_category", False)),
                "suspect_priority": bool(item.get("suspect_priority", False)),
                "suggested_category": _normalized_label(
                    item.get("suggested_category"), ALLOWED_CATEGORIES
                ),
                "suggested_priority": _normalized_label(
                    item.get("suggested_priority"), ALLOWED_PRIORITIES
                ),
                "confidence": float(item.get("confidence", 0.0)),
                "reason": str(item.get("reason", "")).strip(),
            }
        )
    return out


def run_audit(
    kb_csv: Path,
    batch_size: int,
    output_csv: Path,
    *,
    min_suspect_confidence: float = DEFAULT_MIN_SUSPECT_CONFIDENCE,
    batch_delay_s: float = DEFAULT_BATCH_DELAY_S,
    model_override: str | None = None,
) -> None:
    cfg = _proxy_config(model_override=model_override)
    output_csv.parent.mkdir(parents=True, exist_ok=True)

    with kb_csv.open("r", encoding="utf-8", newline="") as infile:
        reader = csv.DictReader(infile)
        input_fields = list(reader.fieldnames or [])
        base_fields = [f for f in input_fields if f not in LLM_AUDIT_COLUMNS]
        rows = list(reader)

    rows_sorted = sorted(
        rows,
        key=lambda r: (
            r.get("category", "").strip().lower(),
            PRIORITY_ORDER.get(r.get("priority", "").strip().lower(), 999),
            r.get("ticket_id", ""),
        ),
    )
    batches = [
        rows_sorted[i : i + batch_size]
        for i in range(0, len(rows_sorted), batch_size)
    ]

    suspect_by_ticket: dict[str, dict[str, Any]] = {}
    total = len(batches)
    for idx, batch in enumerate(batches, start=1):
        user_content = build_batch_user_content(batch)
        raw = complete_chat(
            cfg,
            messages=[{"role": "user", "content": user_content}],
            temperature=DEFAULT_LLM_TEMPERATURE,
        )

        try:
            suspects = parse_suspects(raw)
        except json.JSONDecodeError as exc:
            print(f"[WARN] Batch {idx}/{total} invalid JSON: {exc}")
            suspects = []

        for suspect in suspects:
            if suspect["confidence"] < min_suspect_confidence:
                continue
            if not suspect["suspect_category"] and not suspect["suspect_priority"]:
                continue
            tid = suspect["ticket_id"]
            prev = suspect_by_ticket.get(tid)
            if prev is None or suspect["confidence"] > prev["confidence"]:
                suspect_by_ticket[tid] = suspect

        print(f"Processed batch {idx}/{total}, suspects so far={len(suspect_by_ticket)}")
        time.sleep(batch_delay_s)

    flagged_rows: list[dict[str, Any]] = []
    out_fields = list(base_fields) + list(LLM_AUDIT_COLUMNS)

    for row in rows:
        ticket_id = row.get("ticket_id", "")
        suspect = suspect_by_ticket.get(ticket_id)
        suspect_category = bool(suspect and suspect["suspect_category"])
        suspect_priority = bool(suspect and suspect["suspect_priority"])
        is_suspect = bool(suspect_category or suspect_priority)

        row_out: dict[str, Any] = {k: row.get(k, "") for k in base_fields}
        row_out["suspect_by_llm"] = is_suspect
        row_out["suspect_category"] = suspect_category
        row_out["suspect_priority"] = suspect_priority
        row_out["suggested_category"] = suspect["suggested_category"] if suspect else ""
        row_out["suggested_priority"] = suspect["suggested_priority"] if suspect else ""
        row_out["suspect_confidence"] = suspect["confidence"] if suspect else 0.0
        row_out["suspect_reason"] = suspect["reason"] if suspect else ""
        row_out["llm_model"] = cfg.model
        flagged_rows.append(row_out)

    n_suspect = sum(1 for r in flagged_rows if r.get("suspect_by_llm"))
    with output_csv.open("w", encoding="utf-8", newline="") as outfile:
        w = csv.DictWriter(outfile, fieldnames=out_fields)
        w.writeheader()
        w.writerows(flagged_rows)

    out_abs = output_csv.resolve()
    print(f"Wrote {out_abs} ({len(rows)} rows, {n_suspect} flagged suspect_by_llm)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit KB labels with Claude")
    parser.add_argument("--input", type=Path, default=DEFAULT_KB_CSV, dest="kb_csv")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_CSV, dest="output_csv")
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument(
        "--min-confidence",
        type=float,
        default=DEFAULT_MIN_SUSPECT_CONFIDENCE,
        dest="min_confidence",
    )
    parser.add_argument("--batch-delay-s", type=float, default=DEFAULT_BATCH_DELAY_S)
    parser.add_argument("--model", default=None)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    kb_csv = _resolve_path(args.kb_csv)
    output_csv = _resolve_path(args.output_csv)
    print(f"Input CSV: {kb_csv.resolve()}")
    print(f"Output CSV: {output_csv.resolve()}")
    run_audit(
        kb_csv=kb_csv,
        batch_size=args.batch_size,
        output_csv=output_csv,
        min_suspect_confidence=args.min_confidence,
        batch_delay_s=args.batch_delay_s,
        model_override=args.model,
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

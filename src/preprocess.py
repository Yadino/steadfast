"""
Stage 2: Preprocess the knowledge base.

Ensures the canonical `data/knowledge_base_fixed.csv` exists, generating
any missing intermediate artifacts on demand:

  data/knowledge_base.csv
      -> tools/llm_kb_audit.py  -> data/knowledge_base_llm_flagged.csv
      -> tools/dedup_kb.py      -> data/knowledge_base_fixed.csv

If the final CSV already exists, this is a no-op. If only the flagged CSV
is missing, the LLM audit runs first; the dedup always runs whenever the
fixed CSV is missing.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from tools import dedup_kb, llm_kb_audit  # noqa: E402

KB_CSV = _REPO_ROOT / "data/knowledge_base.csv"
KB_FLAGGED_CSV = _REPO_ROOT / "data/knowledge_base_llm_flagged.csv"
KB_FIXED_CSV = _REPO_ROOT / "data/knowledge_base_fixed.csv"


def ensure_flagged_kb(
    kb_csv: Path = KB_CSV,
    flagged_csv: Path = KB_FLAGGED_CSV,
) -> Path:
    """Run the LLM audit if the flagged CSV is missing."""
    if flagged_csv.exists():
        return flagged_csv
    if not kb_csv.exists():
        raise FileNotFoundError(f"Source KB not found: {kb_csv}")
    print(f"[preprocess] {flagged_csv.name} missing; running tools/llm_kb_audit.py")
    llm_kb_audit.run_audit(
        kb_csv=kb_csv,
        batch_size=llm_kb_audit.DEFAULT_BATCH_SIZE,
        output_csv=flagged_csv,
    )
    return flagged_csv


def ensure_fixed_kb(
    flagged_csv: Path = KB_FLAGGED_CSV,
    fixed_csv: Path = KB_FIXED_CSV,
) -> Path:
    """Run the dedup if the fixed CSV is missing."""
    if fixed_csv.exists():
        return fixed_csv
    ensure_flagged_kb(flagged_csv=flagged_csv)
    print(f"[preprocess] {fixed_csv.name} missing; running tools/dedup_kb.py")
    dedup_kb.run(input_csv=flagged_csv, output_csv=fixed_csv)
    return fixed_csv


def run() -> Path:
    """Ensure all preprocessed artifacts exist. Returns the fixed KB path."""
    return ensure_fixed_kb()


if __name__ == "__main__":
    try:
        path = run()
        print(f"[preprocess] ready: {path}")
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

"""
Dedup the KB CSV by subject, while keeping the most likely category and priority.

Input:  <repo>/data/knowledge_base_llm_flagged.csv
Output: <repo>/data/knowledge_base_fixed.csv

The output is the canonical KB used for the RAG and the rest of the
pipeline (`scripts/seed_kb.py`, `tools/explorer_ui.py`, etc.).

Tickets sharing an identical `subject` string are treated as duplicates.
Within each duplicate group we pick a single representative row:

  1. Drop rows where `suspect_by_llm` is true (if that empties the group,
     fall back to the original group so we still keep one row).
  2. Compute the most common `category` and `priority` among remaining rows.
  3. Prefer a row that matches both the modal category and modal priority.
     Otherwise, prefer one matching the modal category.
     Otherwise, prefer one matching the modal priority.
     Otherwise, take the first remaining row.

Singleton subjects are kept as-is.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_INPUT_CSV = _REPO_ROOT / "data/knowledge_base_llm_flagged.csv"
DEFAULT_OUTPUT_CSV = _REPO_ROOT / "data/knowledge_base_fixed.csv"

SUBJECT_COL = "subject"
SUSPECT_COL = "suspect_by_llm"
CATEGORY_COL = "category"
PRIORITY_COL = "priority"


def _resolve_path(path: Path) -> Path:
    return path if path.is_absolute() else (_REPO_ROOT / path)


def _coerce_bool(series: pd.Series) -> pd.Series:
    """Coerce a column written by csv (strings like 'True'/'False') to bool."""
    if series.dtype == bool:
        return series
    return series.astype(str).str.strip().str.lower().eq("true")


def _mode_or_none(series: pd.Series) -> str | None:
    # Drop empty/NaN values before computing the mode so they don't win.
    cleaned = series.dropna()
    cleaned = cleaned[cleaned.astype(str).str.strip() != ""]
    if cleaned.empty:
        return None
    modes = cleaned.mode()
    return None if modes.empty else modes.iloc[0]


def _pick_representative(group: pd.DataFrame) -> pd.Series:
    """Return the single row to keep for one subject group."""
    suspect_mask = _coerce_bool(group[SUSPECT_COL])
    candidates = group[~suspect_mask]
    if candidates.empty:
        # Every row was flagged suspect; fall back to the full group so we
        # still emit one representative.
        candidates = group

    modal_category = _mode_or_none(candidates[CATEGORY_COL])
    modal_priority = _mode_or_none(candidates[PRIORITY_COL])

    if modal_category is not None and modal_priority is not None:
        both = candidates[
            (candidates[CATEGORY_COL] == modal_category)
            & (candidates[PRIORITY_COL] == modal_priority)
        ]
        if not both.empty:
            return both.iloc[0]

    if modal_category is not None:
        cat_only = candidates[candidates[CATEGORY_COL] == modal_category]
        if not cat_only.empty:
            return cat_only.iloc[0]

    if modal_priority is not None:
        pri_only = candidates[candidates[PRIORITY_COL] == modal_priority]
        if not pri_only.empty:
            return pri_only.iloc[0]

    return candidates.iloc[0]


def dedup_by_subject(df: pd.DataFrame) -> pd.DataFrame:
    for col in (SUBJECT_COL, SUSPECT_COL, CATEGORY_COL, PRIORITY_COL):
        if col not in df.columns:
            raise ValueError(f"Input CSV is missing required column: {col!r}")

    keep_indices: list[int] = []
    for _subject, group in df.groupby(SUBJECT_COL, sort=False, dropna=False):
        if len(group) == 1:
            keep_indices.append(group.index[0])
            continue
        rep = _pick_representative(group)
        keep_indices.append(rep.name)

    return df.loc[keep_indices].reset_index(drop=True)


def run(input_csv: Path, output_csv: Path) -> None:
    df = pd.read_csv(input_csv)
    n_in = len(df)
    n_subjects = df[SUBJECT_COL].nunique(dropna=False)

    deduped = dedup_by_subject(df)
    n_out = len(deduped)

    output_csv.parent.mkdir(parents=True, exist_ok=True)
    deduped.to_csv(output_csv, index=False)

    print(f"Input:  {input_csv.resolve()} ({n_in} rows, {n_subjects} unique subjects)")
    print(f"Output: {output_csv.resolve()} ({n_out} rows, {n_in - n_out} dropped)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Dedup KB CSV by subject")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_CSV, dest="input_csv")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_CSV, dest="output_csv")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    run(_resolve_path(args.input_csv), _resolve_path(args.output_csv))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

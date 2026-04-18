from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_KB_CSV = REPO_ROOT / "data/knowledge_base.csv"
DEFAULT_AUDIT_CSV = REPO_ROOT / "data/knowledge_base_llm_flagged.csv"
DEFAULT_EVAL_JSON = REPO_ROOT / "data/eval_set.json"
AUDIT_SCRIPT = REPO_ROOT / "tools/llm_kb_audit.py"


def normalize_text(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def boolish(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "y"}


@st.cache_data
def load_csv_rows(path_str: str) -> list[dict[str, str]]:
    path = Path(path_str)
    with path.open("r", encoding="utf-8", newline="") as infile:
        return list(csv.DictReader(infile))


@st.cache_data
def load_eval_rows(path_str: str) -> list[dict[str, Any]]:
    return json.loads(Path(path_str).read_text(encoding="utf-8"))


def summarize_issue_group(rows: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "ticket_ids": ", ".join(r["ticket_id"] for r in rows),
        "customers": ", ".join(sorted({r["customer_name"] for r in rows})),
        "plans": ", ".join(sorted({r["plan"] for r in rows})),
        "categories": ", ".join(sorted({r["category"] for r in rows})),
        "priorities": ", ".join(sorted({r["priority"] for r in rows})),
        "subject": rows[0]["subject"],
        "body_preview": rows[0]["body"][:180],
        "rows": rows,
    }


def exact_issue_groups(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    buckets: dict[tuple[str, str], list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        key = (normalize_text(row["subject"]), normalize_text(row["body"]))
        buckets[key].append(row)
    groups = [summarize_issue_group(group) for group in buckets.values() if len(group) > 1]
    groups.sort(key=lambda g: (-g["count"], g["subject"]))
    return groups


def label_drift_groups(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    groups = []
    for group in exact_issue_groups(rows):
        raw_rows = group["rows"]
        if len({r["category"] for r in raw_rows}) > 1 or len({r["priority"] for r in raw_rows}) > 1:
            groups.append(group)
    return groups


def repeated_customers(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    buckets: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        buckets[row["customer_name"]].append(row)
    groups = []
    for customer, customer_rows in buckets.items():
        if len(customer_rows) < 2:
            continue
        groups.append(
            {
                "customer_name": customer,
                "ticket_count": len(customer_rows),
                "plans": ", ".join(sorted({r["plan"] for r in customer_rows})),
                "categories": ", ".join(sorted({r["category"] for r in customer_rows})),
                "priorities": ", ".join(sorted({r["priority"] for r in customer_rows})),
                "ticket_ids": ", ".join(r["ticket_id"] for r in customer_rows),
            }
        )
    groups.sort(key=lambda g: (-g["ticket_count"], g["customer_name"]))
    return groups


def counter_rows(counter: Counter[str], *, label_name: str) -> list[dict[str, Any]]:
    return [{label_name: key, "count": count} for key, count in counter.most_common()]


def filter_rows(
    rows: list[dict[str, str]],
    *,
    query: str,
    categories: set[str],
    priorities: set[str],
    plans: set[str],
    suspect_only: bool,
) -> list[dict[str, str]]:
    q = normalize_text(query)
    out = []
    for row in rows:
        if categories and row.get("category") not in categories:
            continue
        if priorities and row.get("priority") not in priorities:
            continue
        if plans and row.get("plan") not in plans:
            continue
        if suspect_only and not boolish(row.get("suspect_by_llm")):
            continue
        if q:
            hay = " ".join(
                [
                    row.get("ticket_id", ""),
                    row.get("customer_name", ""),
                    row.get("subject", ""),
                    row.get("body", ""),
                    row.get("resolution", ""),
                    row.get("suspect_reason", ""),
                ]
            )
            if q not in normalize_text(hay):
                continue
        out.append(row)
    return out


def run_audit_in_ui(
    *,
    kb_csv: Path,
    output_csv: Path,
    model: str,
    batch_size: int,
    min_confidence: float,
    batch_delay_s: float,
    log_placeholder: Any,
) -> tuple[int, str]:
    cmd = [
        sys.executable,
        str(AUDIT_SCRIPT),
        "--input",
        str(kb_csv),
        "--output",
        str(output_csv),
        "--batch-size",
        str(batch_size),
        "--min-confidence",
        str(min_confidence),
        "--batch-delay-s",
        str(batch_delay_s),
    ]
    if model.strip():
        cmd.extend(["--model", model.strip()])

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    proc = subprocess.Popen(
        cmd,
        cwd=str(REPO_ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
    )

    lines: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        lines.append(line.rstrip())
        log_placeholder.code("\n".join(lines[-250:]), language="text")
    proc.wait()
    return proc.returncode, "\n".join(lines)


def render_group_table(groups: list[dict[str, Any]], *, key_prefix: str) -> None:
    display_rows = [{k: v for k, v in group.items() if k != "rows"} for group in groups]
    st.dataframe(display_rows, width="stretch", hide_index=True)
    if not groups:
        return
    options = {
        f"{group['count']}x | {group['ticket_ids']} | {group['subject'][:80]}": group
        for group in groups
    }
    selected = st.selectbox(
        "Inspect group",
        options=list(options.keys()),
        key=f"{key_prefix}_group_select",
    )
    chosen = options[selected]
    st.dataframe(chosen["rows"], width="stretch", hide_index=True)


def main() -> None:
    if get_script_run_ctx() is None:
        print(
            "This is a Streamlit app. Run it with:\n"
            "  .venv/bin/python -m streamlit run tools/explorer_ui.py"
        )
        return

    st.set_page_config(page_title="Steadfast KB Explorer", layout="wide")
    st.title("Steadfast KB Explorer")
    st.caption(
        "Explore the knowledge base, inspect duplicate issues and label drift, "
        "and run the LLM audit from inside the UI."
    )

    kb_path = Path(st.sidebar.text_input("KB CSV", str(DEFAULT_KB_CSV)))
    audit_path = Path(st.sidebar.text_input("Audit CSV", str(DEFAULT_AUDIT_CSV)))
    eval_path = Path(st.sidebar.text_input("Eval JSON", str(DEFAULT_EVAL_JSON)))

    if not kb_path.exists():
        st.error(f"KB CSV not found: {kb_path}")
        st.stop()

    kb_rows = load_csv_rows(str(kb_path))
    duplicate_groups = exact_issue_groups(kb_rows)
    drift_groups = label_drift_groups(kb_rows)
    customer_groups = repeated_customers(kb_rows)

    eval_rows: list[dict[str, Any]] = []
    if eval_path.exists():
        eval_rows = load_eval_rows(str(eval_path))

    tabs = st.tabs(["Overview", "Duplicates", "Row Review", "LLM Audit", "Eval"])

    with tabs[0]:
        st.subheader("Dataset shape")
        col1, col2, col3, col4, col5 = st.columns(5)
        col1.metric("KB rows", len(kb_rows))
        col2.metric("Exact duplicate groups", len(duplicate_groups))
        col3.metric("Rows in duplicate groups", sum(g["count"] for g in duplicate_groups))
        col4.metric("Label drift groups", len(drift_groups))
        col5.metric("Repeat customers", len(customer_groups))

        left, middle, right = st.columns(3)
        left.write("**Category distribution**")
        left.dataframe(
            counter_rows(Counter(r["category"] for r in kb_rows), label_name="category"),
            width="stretch",
            hide_index=True,
        )
        middle.write("**Priority distribution**")
        middle.dataframe(
            counter_rows(Counter(r["priority"] for r in kb_rows), label_name="priority"),
            width="stretch",
            hide_index=True,
        )
        right.write("**Plan distribution**")
        right.dataframe(
            counter_rows(Counter(r["plan"] for r in kb_rows), label_name="plan"),
            width="stretch",
            hide_index=True,
        )

        st.subheader("What looks useful to explore")
        st.markdown(
            "- Exact duplicate issues with different priorities or categories.\n"
            "- Repeat customers hitting similar issue families.\n"
            "- Suspect labels from the LLM audit, especially when it suggests an alternative label.\n"
            "- Ticket text search for product terms, integrations, failure modes, or urgency language."
        )

        st.subheader("Top repeat customers")
        st.dataframe(customer_groups[:25], width="stretch", hide_index=True)

    with tabs[1]:
        st.subheader("Duplicate issue review")
        mode = st.radio(
            "Duplicate view",
            options=["Exact duplicate issues", "Only label drift"],
            horizontal=True,
        )
        groups = duplicate_groups if mode == "Exact duplicate issues" else drift_groups
        render_group_table(groups, key_prefix="dupes")

    with tabs[2]:
        st.subheader("Row inspection")
        categories = sorted({r["category"] for r in kb_rows})
        priorities = sorted({r["priority"] for r in kb_rows}, key=lambda x: ["low", "medium", "high", "critical"].index(x))
        plans = sorted({r["plan"] for r in kb_rows})
        audit_rows = load_csv_rows(str(audit_path)) if audit_path.exists() else []
        review_rows = audit_rows if audit_rows else kb_rows

        query = st.text_input("Search text", placeholder="ticket id, subject, body, resolution, suspect reason...")
        sel_categories = set(st.multiselect("Category", categories))
        sel_priorities = set(st.multiselect("Priority", priorities))
        sel_plans = set(st.multiselect("Plan", plans))
        suspect_only = st.checkbox("Only suspect rows", value=False, disabled=not audit_rows)

        filtered = filter_rows(
            review_rows,
            query=query,
            categories=sel_categories,
            priorities=sel_priorities,
            plans=sel_plans,
            suspect_only=suspect_only,
        )
        st.caption(f"{len(filtered)} rows matched")
        st.dataframe(filtered[:500], width="stretch", hide_index=True)

        if filtered:
            options = {
                f"{row['ticket_id']} | {row['subject'][:90]}": row
                for row in filtered[:500]
            }
            selected = st.selectbox("Inspect ticket", list(options.keys()))
            row = options[selected]
            left, right = st.columns(2)
            left.write(
                {
                    "ticket_id": row.get("ticket_id"),
                    "customer_name": row.get("customer_name"),
                    "plan": row.get("plan"),
                    "category": row.get("category"),
                    "priority": row.get("priority"),
                    "suspect_by_llm": row.get("suspect_by_llm", ""),
                    "suggested_category": row.get("suggested_category", ""),
                    "suggested_priority": row.get("suggested_priority", ""),
                    "suspect_confidence": row.get("suspect_confidence", ""),
                }
            )
            right.write("**Subject**")
            right.write(row.get("subject", ""))
            st.write("**Body**")
            st.code(row.get("body", ""), language="text")
            st.write("**Resolution**")
            st.code(row.get("resolution", ""), language="text")
            if row.get("suspect_reason"):
                st.write("**LLM suspect reason**")
                st.code(row["suspect_reason"], language="text")

    with tabs[3]:
        st.subheader("Run LLM audit")
        st.caption("This runs `tools/llm_kb_audit.py` and streams stdout below.")

        col1, col2, col3 = st.columns(3)
        model = col1.text_input("Model", value="claude-sonnet-4-6")
        batch_size = col2.number_input("Batch size", min_value=1, max_value=100, value=20, step=1)
        min_confidence = col3.slider("Min confidence", min_value=0.0, max_value=1.0, value=0.8, step=0.01)
        output_text = st.text_input("Output CSV", value=str(audit_path))
        batch_delay_s = st.number_input("Batch delay (seconds)", min_value=0.0, max_value=10.0, value=0.2, step=0.1)

        log_placeholder = st.empty()
        if st.button("Run audit now", type="primary"):
            code, log_text = run_audit_in_ui(
                kb_csv=kb_path,
                output_csv=Path(output_text),
                model=model,
                batch_size=int(batch_size),
                min_confidence=float(min_confidence),
                batch_delay_s=float(batch_delay_s),
                log_placeholder=log_placeholder,
            )
            st.session_state["audit_exit_code"] = code
            st.session_state["audit_log"] = log_text
            load_csv_rows.clear()

        if "audit_exit_code" in st.session_state:
            if st.session_state["audit_exit_code"] == 0:
                st.success("Audit finished successfully.")
            else:
                st.error(f"Audit failed with exit code {st.session_state['audit_exit_code']}.")

        live_audit_path = Path(output_text)
        if live_audit_path.exists():
            audit_rows = load_csv_rows(str(live_audit_path))
            suspect_rows = [row for row in audit_rows if boolish(row.get("suspect_by_llm"))]
            col1, col2 = st.columns(2)
            col1.metric("Audit rows", len(audit_rows))
            col2.metric("Suspect rows", len(suspect_rows))
            st.dataframe(suspect_rows[:250], width="stretch", hide_index=True)

    with tabs[4]:
        st.subheader("Eval set")
        if not eval_rows:
            st.info("Eval file not found.")
        else:
            col1, col2, col3 = st.columns(3)
            col1.metric("Eval rows", len(eval_rows))
            col2.metric("Distinct categories", len({r["expected_category"] for r in eval_rows}))
            col3.metric("Distinct priorities", len({r["expected_priority"] for r in eval_rows}))
            left, right = st.columns(2)
            left.write("**Expected category distribution**")
            left.dataframe(
                counter_rows(
                    Counter(r["expected_category"] for r in eval_rows),
                    label_name="expected_category",
                ),
                width="stretch",
                hide_index=True,
            )
            right.write("**Expected priority distribution**")
            right.dataframe(
                counter_rows(
                    Counter(r["expected_priority"] for r in eval_rows),
                    label_name="expected_priority",
                ),
                width="stretch",
                hide_index=True,
            )
            st.dataframe(eval_rows, width="stretch", hide_index=True)


if __name__ == "__main__":
    main()

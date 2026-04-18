"""
Seed the local Postgres + pgvector DB from data/knowledge_base_fixed.csv.

This is the cleaned/deduped KB produced by `tools/dedup_kb.py` and is
the canonical source of truth for the RAG.

Usage:
  docker compose up -d db
  .venv/bin/pip install -r requirements.txt
  .venv/bin/python scripts/seed_kb.py

Reads DATABASE_URL from .env (see .env.example). Re-runs are idempotent
(upsert on ticket_id), so re-seeding after CSV changes just works.
"""

from __future__ import annotations

import csv
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.config import EMBED_DIM, SEED_BATCH_SIZE
from src.db import connect, vector_literal
from src.embeddings import embed

KB_CSV = REPO_ROOT / "data/knowledge_base_fixed.csv"
BATCH_SIZE = SEED_BATCH_SIZE

SCHEMA_SQL = f"""
CREATE TABLE IF NOT EXISTS kb_tickets (
    ticket_id     TEXT PRIMARY KEY,
    created_at    TIMESTAMPTZ,
    customer_name TEXT,
    plan          TEXT,
    subject       TEXT,
    body          TEXT,
    category      TEXT,
    priority      TEXT,
    resolution    TEXT,
    resolved_at   TIMESTAMPTZ,
    search_text   TEXT NOT NULL,
    embedding     vector({EMBED_DIM})
);

CREATE INDEX IF NOT EXISTS kb_tickets_embedding_idx
    ON kb_tickets USING hnsw (embedding vector_cosine_ops);
"""

UPSERT_SQL = """
INSERT INTO kb_tickets (
    ticket_id, created_at, customer_name, plan,
    subject, body, category, priority,
    resolution, resolved_at, search_text, embedding
)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::vector)
ON CONFLICT (ticket_id) DO UPDATE SET
    created_at    = EXCLUDED.created_at,
    customer_name = EXCLUDED.customer_name,
    plan          = EXCLUDED.plan,
    subject       = EXCLUDED.subject,
    body          = EXCLUDED.body,
    category      = EXCLUDED.category,
    priority      = EXCLUDED.priority,
    resolution    = EXCLUDED.resolution,
    resolved_at   = EXCLUDED.resolved_at,
    search_text   = EXCLUDED.search_text,
    embedding     = EXCLUDED.embedding;
"""


def build_search_text(row: dict[str, str]) -> str:
    return (
        f"Subject: {row['subject'].strip()}\n"
        f"Body: {row['body'].strip()}\n"
        f"Resolution: {row['resolution'].strip()}"
    )


def chunked(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def ensure_extension() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        conn.commit()


def ensure_schema() -> None:
    with connect() as conn, conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
        conn.commit()


def load_rows() -> list[dict[str, str]]:
    with KB_CSV.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["_search_text"] = build_search_text(r)
    return rows


def upsert(rows: list[dict[str, str]]) -> None:
    inserted = 0
    total = len(rows)
    with connect() as conn:
        for batch in chunked(rows, BATCH_SIZE):
            vectors = embed(r["_search_text"] for r in batch)
            with conn.cursor() as cur:
                for r, v in zip(batch, vectors):
                    cur.execute(
                        UPSERT_SQL,
                        (
                            r["ticket_id"],
                            r["created_at"] or None,
                            r["customer_name"],
                            r["plan"],
                            r["subject"],
                            r["body"],
                            r["category"],
                            r["priority"],
                            r["resolution"],
                            r["resolved_at"] or None,
                            r["_search_text"],
                            vector_literal(v),
                        ),
                    )
            conn.commit()
            inserted += len(batch)
            print(f"  upserted {inserted}/{total}")


def main() -> None:
    print(f"Knowledge base CSV: {KB_CSV}")
    if not KB_CSV.exists():
        raise SystemExit(f"Missing CSV: {KB_CSV}")

    print("Ensuring pgvector extension...")
    ensure_extension()
    print("Ensuring schema...")
    ensure_schema()

    print("Loading CSV...")
    rows = load_rows()
    print(f"Loaded {len(rows)} rows.")

    print(f"Embedding (model: BAAI/bge-small-en-v1.5, dim: {EMBED_DIM})...")
    upsert(rows)

    print("Done.")


if __name__ == "__main__":
    main()

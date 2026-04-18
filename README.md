# Steadfast Triage — Implementation README

> # ⚠️ LOOKING FOR THE ASSIGNMENT BRIEF?
> # → See **[`README_INSTRUCTIONS.md`](./README_INSTRUCTIONS.md)** ←
>
> The original problem statement, pipeline spec, output format, rules,
> deliverables, and FAQ all live there. **This file is implementation
> notes only** — how to set things up and run them as they exist today.

---

## What's in here so far

| Piece | What it does | Where |
|---|---|---|
| Postgres + pgvector (Docker) | Local vector DB, port `5433` | `docker-compose.yml` |
| KB seed script | Loads `data/knowledge_base.csv`, embeds rows, upserts into Postgres | `scripts/seed_kb.py` |
| DB helper | `dsn()`, `connect()`, `vector_literal()` | `src/db.py` |
| Embedding helper | `embed(texts)` using `BAAI/bge-small-en-v1.5` (384-dim, ONNX, local) | `src/embeddings.py` |
| LLM proxy client | OpenAI-compatible chat completions over `httpx` | `tools/proxy_chat.py` |
| KB audit (LLM) | Flags suspicious rows in the KB; outputs `data/knowledge_base_llm_flagged.csv` | `tools/llm_kb_audit.py` |
| Explorer UI | Streamlit ticket browser + suspect filter + audit runner | `tools/explorer_ui.py` |

The full pipeline (classification → retrieval → response → eval) is **not**
built yet. This file will grow as each stage lands.

---

## Prerequisites

- Docker Desktop (running)
- Python 3.13 (a `.venv` in repo root is fine)
- An `.env` with the LLM proxy creds — copy from `.env.example`:

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL
```

---

## Setup: database + seed

The DB lives in a single Docker container with the `pgvector` extension
pre-installed. Data persists in a named volume (`pgdata`), so stopping
the container does not wipe the rows.

### 1. Start Postgres

```bash
docker compose up -d db
```

What this does:
- Pulls `pgvector/pgvector:pg16` (first time only, ~110 MB).
- Starts a container named `steadfast-db` exposing **port 5433** on the
  host (5432 inside the container — picked 5433 to avoid clashing with a
  local Postgres).
- Creates DB `steadfast`, user `steadfast`, password `steadfast`.
- Waits for the healthcheck (`pg_isready`) before reporting "healthy".

Connection string (already in `.env.example`):
```
DATABASE_URL=postgresql://steadfast:steadfast@localhost:5433/steadfast
```

### 2. Install Python deps

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`fastembed` will lazily download the embedding model (`BAAI/bge-small-en-v1.5`,
~90 MB) on first use. Subsequent runs are instant.

### 3. Seed the knowledge base

```bash
.venv/bin/python scripts/seed_kb.py
```

What this does:
1. `CREATE EXTENSION IF NOT EXISTS vector;`
2. Creates the `kb_tickets` table (one row per KB ticket) and an
   **HNSW cosine** index on the `embedding` column.
3. Reads `data/knowledge_base.csv`.
4. Builds a `search_text` per row (`Subject: ... \n Body: ... \n Resolution: ...`).
5. Embeds in batches of 64 with `fastembed`.
6. Upserts (`INSERT ... ON CONFLICT (ticket_id) DO UPDATE`) so re-runs
   after CSV edits just refresh the rows.

You should see:
```
Loaded 308 rows.
Embedding (model: BAAI/bge-small-en-v1.5, dim: 384)...
  upserted 64/308
  ...
  upserted 308/308
Done.
```

### 4. Verify it works

Row count + category breakdown:
```bash
docker exec steadfast-db psql -U steadfast -d steadfast \
  -c "SELECT COUNT(*) FROM kb_tickets;" \
  -c "SELECT category, COUNT(*) FROM kb_tickets GROUP BY 1 ORDER BY 2 DESC;"
```

Top-5 nearest tickets to a query:
```bash
.venv/bin/python -c "
from src.db import connect, vector_literal
from src.embeddings import embed
v = embed(['My dashboard charts are not loading'])[0]
with connect() as c, c.cursor() as cur:
    cur.execute(
      'SELECT ticket_id, category, priority, subject, '
      '1 - (embedding <=> %s::vector) AS score '
      'FROM kb_tickets ORDER BY embedding <=> %s::vector LIMIT 5;',
      (vector_literal(v), vector_literal(v)))
    for r in cur.fetchall(): print(r)
"
```

---

## DB schema

```sql
CREATE TABLE kb_tickets (
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
    search_text   TEXT NOT NULL,        -- subject + body + resolution
    embedding     vector(384)           -- bge-small-en-v1.5
);

CREATE INDEX kb_tickets_embedding_idx
    ON kb_tickets USING hnsw (embedding vector_cosine_ops);
```

Notes:
- All original CSV columns are preserved verbatim — the model can later
  filter / weight by `plan`, `priority`, etc.
- `embedding` uses cosine distance (`<=>` operator with
  `vector_cosine_ops`).
- HNSW index works without pre-population, unlike IVFFlat.

---

## Useful commands

```bash
# Status
docker compose ps
docker logs -f steadfast-db

# Stop (data persists in the pgdata volume)
docker compose stop db

# Stop + remove container, keep data
docker compose down

# Nuke everything, including data
docker compose down -v

# Open a psql shell
docker exec -it steadfast-db psql -U steadfast -d steadfast

# Re-seed after editing the CSV
.venv/bin/python scripts/seed_kb.py
```

---

## Known data quirks (from earlier exploration)

The KB is intentionally noisy. Things already observed and that the
retrieval / response stages will need to handle:

- **Exact duplicates with conflicting labels.** Multiple tickets share
  the same subject/body but disagree on `category` or `priority`
  (e.g., `TK-0044`, `TK-0215`, `TK-0424` are all "Dashboard takes 45+
  seconds to load" with priorities `high`, `low`, `low`).
- **Possible label drift.** `data/knowledge_base_llm_flagged.csv` is the
  output of the LLM audit (`tools/llm_kb_audit.py`) — rows with
  `suspect_by_llm = true` are the ones the audit flagged as having
  inconsistent category/priority for their content.
- **Repeated customers / templated bodies** — see the explorer UI's
  Duplicates and Row Review tabs.

---

## What's next (will update as it lands)

- `src/agent.py` — classification stage using the prompt in `src/prompts.py`,
  strict JSON output, `classification_confidence` tracked separately.
- `src/agent.py` (cont.) — retrieval-query generation, vector search over
  `kb_tickets`, context assembly, 3-mode response generation
  (answer / suspect / no-answer).
- `src/pipeline.py` — thin orchestrator producing the final per-ticket JSON
  (`ticket_id`, `category`, `priority`, `response`, `confidence`, `flags`).
- `src/evaluate.py` + `src/analyze.py` — eval on `data/eval_set.json` and
  error analysis.

# Steadfast Triage — Implementation README

> # ⚠️ LOOKING FOR THE ASSIGNMENT BRIEF?
> # → See **[`README_INSTRUCTIONS.md`](./README_INSTRUCTIONS.md)** ←
>
> The original problem statement, pipeline spec, output format, rules,
> deliverables, and FAQ all live there. **This file is implementation
> notes only** — how to set things up and run them as they exist today.

---

## What's in here

| Piece | What it does | Where |
|---|---|---|
| Postgres + pgvector (Docker) | Local vector DB, port `5433` | `docker-compose.yml` |
| FastAPI service (Docker) | HTTP API + tiny HTML page to run the pipeline, port `8000` | `Dockerfile`, `src/api.py` |
| KB seed script | Reads `data/knowledge_base_fixed.csv`, embeds rows, upserts into Postgres | `scripts/seed_kb.py` |
| KB dedup tool | Cleans `knowledge_base_llm_flagged.csv` → `knowledge_base_fixed.csv` (one row per subject) | `tools/dedup_kb.py` |
| Pipeline orchestrator | `process_ticket(...)` — classify → retrieval query → RAG → response | `src/pipeline.py` |
| Agent stages | `classify`, `build_retrieval_query`, `retrieve`, `generate_response` | `src/agent.py` |
| Prompts | Classification, retrieval-query, response-generation | `src/prompts.py` |
| DB helpers | `dsn()`, `connect()`, `vector_literal()` | `src/db.py` |
| Embeddings | `embed(texts)` using `BAAI/bge-small-en-v1.5` (384-dim, ONNX, local) | `src/embeddings.py` |
| LLM proxy client | OpenAI-compatible `chat/completions`; Anthropic `system` handled | `tools/proxy_chat.py` |
| KB audit (LLM) | Flags suspicious rows in the KB (separate tool) | `tools/llm_kb_audit.py` |
| Explorer UI | Streamlit ticket browser + suspect filter + audit runner | `tools/explorer_ui.py` |

Validation / heuristics / eval / analysis stages are still TODO.

---

## Prerequisites

- Docker Desktop (running)
- Python 3.13 (for running the seed script and local CLI; venv in repo root is fine)
- An `.env` with LLM proxy creds — copy from `.env.example`:

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL
```

---

## End-to-end setup

```bash
# 1. Bring up Postgres + pgvector
docker compose up -d db

# 2. Install Python deps in a local venv (used by the seed script)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Seed the KB into the DB (runs on the host, talks to localhost:5433)
.venv/bin/python scripts/seed_kb.py

# 4. Build and start the API container
docker compose up -d api

# 5. Open the UI
open http://localhost:8000
```

The HTML page at `http://localhost:8000/` has a dropdown that lists every
CSV/JSON under `data/` — pick one, set a limit, hit **Run**.

---

## Architecture

```
┌─────────────────┐        ┌─────────────────┐
│  Host (you)     │        │  Docker network │
│                 │        │                 │
│  browser  ──────┼────────▶  steadfast-api  │
│  (port 8000)    │        │  (FastAPI)      │
│                 │        │        │        │
│  seed_kb.py ────┼──┐     │        │ SQL    │
│                 │  │     │        ▼        │
│                 │  │     │  steadfast-db   │
│                 │  └─────▶  (pgvector pg16)│
│                 │        │                 │
│                 │        └─────────────────┘
│                 │
│  LLM proxy ◀────┼────(HTTPS, httpx)────┐
│  (remote)       │                       │
└─────────────────┘                       │
                     steadfast-api calls ─┘
```

Two containers:
- `steadfast-db` — `pgvector/pgvector:pg16`, port `5433` on host.
- `steadfast-api` — built from `Dockerfile`, port `8000` on host, talks
  to `db:5432` on the internal network, reads `.env` for the LLM proxy,
  mounts `./data:/app/data:ro` so adding a new test CSV on the host
  appears immediately in the dropdown.

---

## The pipeline (`src/pipeline.py` → `process_ticket`)

Three LLM calls + one vector search per ticket:

1. **Classify** (`src/agent.py :: classify`)
   Uses `CLASSIFICATION_SYSTEM_PROMPT`. Returns `category`, `priority`,
   `classification_confidence`, `classification_flags`. Enum values are
   coerced to the allowed set; invalid JSON falls back to
   `unknown`/`low` + `escalate_to_human`.

2. **Build retrieval query** (`build_retrieval_query`)
   Uses `RETRIEVAL_QUERY_SYSTEM_PROMPT` to distill subject + body +
   classification into a short, concrete search query.

3. **Retrieve** (`retrieve`)
   Embeds the query with `fastembed` and runs a cosine-similarity
   lookup against `kb_tickets` (HNSW index), top-K = 5.

4. **Generate response** (`generate_response`)
   Uses `RESPONSE_SYSTEM_PROMPT`. Picks one of three modes:
   - `answer_found` — KB covers this, confident reply (0.75–0.95)
   - `needs_human_check` — KB related but not conclusive (0.4–0.7)
   - `no_relevant_answer` — KB doesn't cover this (0.1–0.4)

### Output contract (per ticket)

```json
{
  "ticket_id": "EVAL-001",
  "category": "bug",
  "priority": "high",
  "response": "Hi Cirrus Cloud Inc. team, thank you for reaching out — ...",
  "confidence": 0.52,
  "flags": ["ambiguous_category", "escalate_to_human"]
}
```

`confidence` here is the **response-generation** confidence. The
`classification_confidence` is tracked internally (see `--internal` /
`include_internal` below).

Extended internal object also includes:
- `classification_confidence`
- `response_mode` (`answer_found` / `needs_human_check` / `no_relevant_answer`)
- `retrieval_query`
- `retrieved` — top-K KB matches with `ticket_id`, `category`,
  `priority`, `subject`, `score`.

---

## API reference

All endpoints served from `http://localhost:8000`.

| Method | Path | Description |
|---|---|---|
| `GET`  | `/` | Minimal HTML UI (dataset picker + single-ticket form) |
| `GET`  | `/health` | `{"ok": true}` |
| `GET`  | `/datasets` | Lists `.csv` and `.json` files under `data/` |
| `POST` | `/run` | Run pipeline on a dataset file |
| `POST` | `/ticket` | Run pipeline on one ad-hoc ticket |

### `POST /run`

Body:
```json
{ "path": "data/eval_set.json", "limit": 3, "include_internal": false }
```
- `path` — relative to the repo root (must live under `data/`).
- `limit` — optional, cap on how many tickets to process.
- `include_internal` — if `true`, return the debug object
  (adds `classification_confidence`, `response_mode`, `retrieval_query`,
  `retrieved`).

Response:
```json
{ "source": "/app/data/eval_set.json", "count": 3, "results": [ {...}, ... ] }
```

### `POST /ticket`

Body:
```json
{
  "ticket_id": "AD-HOC-1",
  "subject": "Dashboard very slow today",
  "body": "...",
  "customer_name": "Acme",
  "plan": "Growth"
}
```
Returns both the `final` object and the `internal` debug object.

---

## Running from the CLI (without the API)

The pipeline also runs standalone, useful for scripted eval on the host:

```bash
.venv/bin/python -m src.pipeline --input data/eval_set.json --limit 5
.venv/bin/python -m src.pipeline --input data/eval_set.json --limit 5 --internal
```

Outputs one JSON line per ticket on stdout.

---

## File selection in the UI

The `data/` directory is mounted read-only into the API container:

```yaml
volumes:
  - ./data:/app/data:ro
```

So if you drop another `my_test.csv` (or `.json`) into `data/` on the
host, it will appear in the dropdown at `http://localhost:8000/`
on the next page load — **no container restart required**. Input tickets
need at least `ticket_id`, `subject`, and `body`; `customer_name` and
`plan` are used if present.

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

The seed script is idempotent (`INSERT ... ON CONFLICT (ticket_id) DO
UPDATE`), so re-running it after editing the CSV just refreshes rows.

---

## Useful commands

```bash
# Status
docker compose ps
docker logs -f steadfast-api
docker logs -f steadfast-db

# Stop (data + model cache persist in volumes)
docker compose stop

# Stop + remove containers (volumes persist)
docker compose down

# Nuke everything including data + model cache
docker compose down -v

# Open a psql shell
docker exec -it steadfast-db psql -U steadfast -d steadfast

# Rebuild the api after code changes
docker compose build api && docker compose up -d api

# Re-seed after editing data/knowledge_base_fixed.csv
.venv/bin/python scripts/seed_kb.py
```

---

## KB cleaning pipeline

Two tools turn the raw `data/knowledge_base.csv` into the canonical
`data/knowledge_base_fixed.csv` used by the seeder and the rest of the app.

### `tools/llm_kb_audit.py` — flag suspect labels

1. Read `data/knowledge_base.csv`.
2. Sort rows by `(category, priority, ticket_id)` and chunk into batches
   (default 20).
3. For each batch, send `ticket_id / subject / body / resolution /
   category / priority` to the LLM (Claude via the proxy) with a strict
   "only flag clearly wrong labels" prompt and parse the JSON response.
4. Keep flags above the confidence threshold (default `0.8`); on ties,
   keep the highest-confidence verdict per ticket.
5. Write `data/knowledge_base_llm_flagged.csv`: original columns plus
   `suspect_by_llm`, `suspect_category`, `suspect_priority`,
   `suggested_category`, `suggested_priority`, `suspect_confidence`,
   `suspect_reason`, `llm_model`.

### `tools/dedup_kb.py` — pick one row per subject

1. Read `data/knowledge_base_llm_flagged.csv` with pandas.
2. Group rows by exact `subject` string. Singletons pass through.
3. For each duplicate group, drop rows where `suspect_by_llm = true`
   (if that empties the group, fall back to the original group).
4. Compute the modal `category` and modal `priority` among what remains.
5. Pick the representative: first row matching **both** modes, else the
   modal `category`, else the modal `priority`, else the first row.
6. Write `data/knowledge_base_fixed.csv` (one row per subject).

---

## Known data quirks (from earlier exploration)

The KB is intentionally noisy. Things already observed that retrieval /
response must tolerate:

- **Exact duplicates with conflicting labels.** Multiple tickets share
  the same subject/body but disagree on `category` or `priority`
  (e.g., `TK-0044`, `TK-0215`, `TK-0424` are all "Dashboard takes 45+
  seconds to load" with priorities `high`, `low`, `low`). Retrieval
  top-K often surfaces 4–5 near-identical rows with different labels.
- **Possible label drift.** `data/knowledge_base_llm_flagged.csv` is the
  intermediate output of the LLM audit (`tools/llm_kb_audit.py`) — rows
  with `suspect_by_llm = true` are the ones the audit flagged as having
  inconsistent category/priority for their content. `tools/dedup_kb.py`
  consumes that file and writes `data/knowledge_base_fixed.csv`, which is
  the canonical KB used by the seed script and the rest of the pipeline.
- **Repeated customers / templated bodies** — see the explorer UI's
  Duplicates and Row Review tabs.

---

## What's next (will update as it lands)

- `src/validate.py` — strict schema validation as a standalone stage.
- `src/postprocess.py` — small, data-justified heuristic rules (e.g.
  force `escalate_to_human` on lockout / data-loss keywords; flag
  `possible_duplicate` when top-K is dominated by near-identical rows).
- `src/evaluate.py` — category accuracy, priority accuracy, response-quality
  proxy on `data/eval_set.json`, per-category/priority breakdowns.
- `src/analyze.py` — error bucketing (classification miss vs retrieval
  miss vs weak synthesis vs noisy-KB).
- Wire retrieval evidence back into `tools/explorer_ui.py`.

# Steadfast Support Triage

A classification + RAG pipeline for incoming customer support tickets.
Each ticket is classified (category + priority), a retrieval query is
distilled from it, the top-K nearest past tickets are pulled from a
pgvector KB, and an LLM drafts a first-line customer reply grounded in
that evidence. The whole thing is exposed as a FastAPI service with a
streaming web UI and a CLI, and ships with a built-in evaluation stage.

> Assignment brief lives in [`README_INSTRUCTIONS.md`](./README_INSTRUCTIONS.md).
> This file is implementation notes only.

Current results on `data/eval_set.json` (n=46):

| Metric | Value |
|---|---|
| Category accuracy | **0.93** |
| Priority accuracy | **0.78** |
| LLM response-judge avg | **0.89** |

---

## Repository layout

| Path | What it is |
|---|---|
| `docker-compose.yml`, `Dockerfile` | Two-container stack: `pgvector/pgvector:pg16` + FastAPI service |
| `src/api.py` | FastAPI app — `/`, `/health`, `/datasets`, `/run`, `/run_stream`, `/ticket`, `/outputs/{name}` |
| `src/static/index.html` | Streaming web UI (dataset picker, live results, summary panel) |
| `src/pipeline.py` | `process_ticket(...)` orchestrator + `python -m src.pipeline` CLI |
| `src/agent.py` | LLM stages: `classify`, `build_retrieval_query`, `retrieve`, `generate_response` |
| `src/prompts.py` | System prompts for every LLM stage (classify / retrieval / response / judge) |
| `src/preprocess.py` | Stage 2: ensure canonical KB exists (runs audit + dedup on demand) |
| `src/postprocess.py` | Stage 5: priority heuristics (question-cap, no-urgency downgrade) |
| `src/validate.py` | Stage 4: schema validation with safe fallbacks + `escalate_to_human` |
| `src/evaluate.py` | Stage 6: label accuracy + LLM response judge + aggregation |
| `src/config.py` | Central config (models, temperatures, top-K, embeddings) from env / `.env` |
| `src/db.py`, `src/embeddings.py` | Postgres helpers + `fastembed` (BAAI/bge-small-en-v1.5, 384-dim) |
| `tools/proxy_chat.py` | OpenAI-compatible `chat/completions` client (Anthropic `system` handled) |
| `tools/llm_kb_audit.py` | Flags rows in the raw KB whose category/priority looks wrong |
| `tools/dedup_kb.py` | Collapses duplicate-subject rows into one representative row |
| `tools/explorer_ui.py` | Streamlit KB/data explorer (duplicates, row review, audit runner) |
| `scripts/seed_kb.py` | Embeds `data/knowledge_base_fixed.csv` and upserts into Postgres |
| `data/` | Inputs (`knowledge_base*.csv`, `eval_set.json`) |
| `output/` | Evaluation artifacts (`eval_results.json`, `error_analysis.json`) |

---

## Prerequisites

- Docker Desktop
- Python 3.13 (for the host-side seed script and CLI)
- A `.env` with LLM proxy credentials:

```bash
cp .env.example .env
# fill in ANTHROPIC_API_KEY and ANTHROPIC_BASE_URL
```

`src/config.py` also accepts `LLM_MODEL`, `RESPONSE_MODEL`, `TOP_K`,
`TEMPERATURE_CLASSIFY`, `TEMPERATURE_RETRIEVAL_QUERY`,
`TEMPERATURE_RESPONSE` (set any temperature to `off` to omit the field —
some newer models reject it), and `EMBED_MODEL_NAME` / `EMBED_DIM`.

---

## Setup

```bash
# 1. Start Postgres + pgvector
docker compose up -d db

# 2. Python venv (used by the seed script and the CLI)
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 3. Seed the KB (runs on the host, talks to localhost:5433)
.venv/bin/python scripts/seed_kb.py

# 4. Start the API
docker compose up -d api

# 5. Open the UI
open http://localhost:8000
```

The UI has a dropdown of every `.csv` / `.json` under `data/`. Pick one,
optionally toggle **Evaluate**, set a **Limit**, and hit **Run** — results
stream in as each ticket finishes, and a summary panel updates live when
evaluation is on. The `data/` and `output/` directories are volume-mounted,
so adding a dataset to `data/` shows up in the dropdown without a restart,
and `output/eval_results.json` and `output/error_analysis.json` are
written back to the host on every run.

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
│  (venv)         │  │     │        ▼        │
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

- `steadfast-db` — `pgvector/pgvector:pg16`, port `5433` on host.
- `steadfast-api` — built from `Dockerfile`, port `8000` on host. Mounts
  `./data:/app/data:ro` and `./output:/app/output`, reads `.env` for the
  LLM proxy, talks to `db:5432` on the internal network.

---

## Pipeline stages

`src/pipeline.py :: process_ticket(ticket, *, evaluate=False)` runs:

1. **Classify** (`src/agent.py :: classify`)
   `CLASSIFICATION_SYSTEM_PROMPT` → `{category, priority, confidence, flags}`.
   Enums are coerced to the allowed set; invalid JSON falls back to
   `unknown` / `low` with `escalate_to_human`.

2. **Retrieval query** (`build_retrieval_query`)
   Distills subject + body + classification into a short 6-20 word search query.

3. **Retrieve** (`retrieve`)
   Embeds the query with `fastembed` (BAAI/bge-small-en-v1.5, 384-dim) and
   runs a cosine-similarity lookup against `kb_tickets` (HNSW index),
   `TOP_K = 5`.

4. **Generate response** (`generate_response`)
   `RESPONSE_SYSTEM_PROMPT` picks one of three modes and drafts a short reply:
   - `answer_found` (conf 0.75–0.95) — KB clearly covers this.
   - `needs_human_check` (0.4–0.7) — KB is related but not conclusive.
   - `no_relevant_answer` (0.1–0.4) — KB doesn't cover this.

5. **Postprocess** (`src/postprocess.py :: postprocess`)
   Two cheap rules over the customer text to counter the classifier's
   tendency to over-escalate:
   - Question-shaped tickets ("how do", "wondering", …) are capped at `low`.
   - `high` / `critical` tickets with no urgency signal ("blocking", "outage",
     "can't", "breach", "data loss", …) are downgraded to `medium`.

6. **Validate** (`src/validate.py :: validate_output`)
   Required fields present, `category`/`priority` in the allowed enum,
   non-empty `response`, clamped `confidence`, string-list `flags`. On any
   issue, falls back to safe defaults and appends `escalate_to_human`.

7. **Evaluate** (optional, `src/evaluate.py :: evaluate_ticket`)
   - Label check against `expected_category` / `expected_priority` if
     present on the input ticket.
   - LLM response judge (`RESPONSE_JUDGE_SYSTEM_PROMPT`, temperature 0)
     scores the draft against the ticket and the retrieved KB on a 0–1
     scale with a short reason.

---

## Output contract

Per ticket, the public (assignment) shape is:

```json
{
  "ticket_id": "EVAL-001",
  "category": "integration",
  "priority": "high",
  "response": "Hi Cirrus Cloud — ...",
  "confidence": 0.85,
  "flags": ["escalate_to_human"]
}
```

`confidence` is the **response-generation** confidence. The internal
(debug) shape — returned when `include_internal=true`, from `/ticket`, or
from the CLI's `--internal` — additionally includes:

- `subject`
- `classification_confidence`
- `response_mode` (`answer_found` / `needs_human_check` / `no_relevant_answer`)
- `retrieval_query`
- `retrieved` — top-K KB matches with `ticket_id`, `category`, `priority`,
  `subject`, `body`, `resolution`, `score`
- `postprocess.adjustments` — list of human-readable adjustments applied
- `validation.issues`, `validation.ok`
- `evaluation` (only when `evaluate=true`) — `expected_category`,
  `expected_priority`, `category_correct`, `priority_correct`,
  `response_score`, `response_score_reason`

---

## API

All endpoints at `http://localhost:8000`.

| Method | Path | Description |
|---|---|---|
| `GET`  | `/` | Streaming web UI |
| `GET`  | `/health` | `{"ok": true}` |
| `GET`  | `/datasets` | Lists `.csv` and `.json` files under `data/` |
| `POST` | `/run` | Run pipeline on a dataset, return all results at once |
| `POST` | `/run_stream` | Same, but stream NDJSON per ticket |
| `POST` | `/ticket` | Run pipeline on one ad-hoc ticket |
| `GET`  | `/outputs/{name}` | Download a file from `output/` (e.g. `eval_results.json`) |

### `POST /run` / `POST /run_stream`

Request body:

```json
{
  "path": "data/eval_set.json",
  "limit": 100,
  "include_internal": false,
  "evaluate": true
}
```

- `path` — relative to repo root; must live under `data/`.
- `limit` — optional cap on ticket count (`null` or `0`-or-less means all).
- `include_internal` — if `true`, `/run` returns the internal/debug object per ticket.
- `evaluate` — request evaluation. Silently turned off if the dataset has
  no `expected_category` / `expected_priority` labels.

`/run` returns `{source, count, evaluated, results, summary, output}` in
one shot. Both endpoints always persist the run to `output/eval_results.json`
(and `output/error_analysis.json` when evaluation ran).

`/run_stream` emits newline-delimited JSON (`application/x-ndjson`):

```
{"event":"start", "source":..., "count":..., "evaluate":...}
{"event":"result", "elapsed_ms":..., "ticket":{...}, "result":{...}, "running":{...}}
{"event":"error",  "elapsed_ms":..., "ticket":{...}, "error":"..."}
{"event":"done",   "count":..., "output":{...}, "summary":{...}}
```

`running` is the rolling evaluation summary, only present when
`evaluate=true`.

### `POST /ticket`

```json
{
  "ticket_id": "AD-HOC-1",
  "subject": "Dashboard very slow today",
  "body": "...",
  "customer_name": "Acme",
  "plan": "Growth"
}
```

Returns both the `final` (assignment-shape) object and the `internal` (debug) object, plus `elapsed_ms`.

---

## CLI

```bash
# One JSON object per line on stdout (public shape):
.venv/bin/python -m src.pipeline --input data/eval_set.json --limit 5

# Internal debug object per ticket:
.venv/bin/python -m src.pipeline --input data/eval_set.json --limit 5 --internal

# Full evaluation — writes output/eval_results.json + output/error_analysis.json
# and prints the summary to stderr:
.venv/bin/python -m src.pipeline --eval
```

`output/eval_results.json` has `{source, count, results[], summary}`.
`summary` contains overall `category_accuracy`, `priority_accuracy`,
`response_score_avg`, per-category and per-priority breakdowns, and a
`validation` sub-block with the schema-validation failure rate.
`output/error_analysis.json` buckets mismatches and low-score responses
for quick inspection.

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

`scripts/seed_kb.py` is idempotent (`INSERT ... ON CONFLICT (ticket_id) DO
UPDATE`), so re-running it refreshes rows in place.

---

## KB cleaning

Two tools turn the raw `data/knowledge_base.csv` into the canonical
`data/knowledge_base_fixed.csv` consumed by the seeder and the pipeline.
`src/preprocess.py` runs them on demand if their outputs are missing.

### `tools/llm_kb_audit.py` — flag suspect labels

1. Read `data/knowledge_base.csv`.
2. Sort by `(category, priority, ticket_id)` and batch (default 20).
3. Send each batch to the LLM with a strict "only flag clearly wrong
   labels" prompt, parse the JSON response.
4. Keep flags above `AUDIT_MIN_CONFIDENCE` (default 0.8); on ties, keep
   the highest-confidence verdict per ticket.
5. Write `data/knowledge_base_llm_flagged.csv` — original columns plus
   `suspect_by_llm`, `suspect_category`, `suspect_priority`,
   `suggested_category`, `suggested_priority`, `suspect_confidence`,
   `suspect_reason`, `llm_model`.

### `tools/dedup_kb.py` — one row per subject

1. Group rows in the flagged CSV by exact `subject` string.
2. In each duplicate group, drop rows where `suspect_by_llm = true`
   (fall back to the original group if that empties it).
3. Compute the modal `category` and modal `priority` among what remains.
4. Pick the representative row: first match on **both** modes, else
   modal category, else modal priority, else the first row.
5. Write `data/knowledge_base_fixed.csv`.

---

## Useful commands

```bash
# Status / logs
docker compose ps
docker logs -f steadfast-api
docker logs -f steadfast-db

# Stop (data + model cache persist in volumes)
docker compose stop

# Stop + remove containers (volumes persist)
docker compose down

# Nuke everything including data + model cache
docker compose down -v

# psql shell
docker exec -it steadfast-db psql -U steadfast -d steadfast

# Rebuild api after code changes
docker compose build api && docker compose up -d api

# Re-seed after editing data/knowledge_base_fixed.csv
.venv/bin/python scripts/seed_kb.py

# Explorer UI (Streamlit)
.venv/bin/streamlit run tools/explorer_ui.py
```

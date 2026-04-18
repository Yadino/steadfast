"""
FastAPI wrapper around the triage pipeline.

Endpoints:
  GET  /               simple HTML page with a CSV/JSON picker + run button
  GET  /health         readiness probe
  GET  /datasets       list CSV/JSON files under data/
  POST /run            run the pipeline on a dataset file
  POST /ticket         run the pipeline on a single ticket payload

Run locally:
  uvicorn src.api:app --reload
Run in Docker:
  docker compose up api
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.pipeline import load_tickets, process_ticket  # noqa: E402

DATA_DIR = REPO_ROOT / "data"

app = FastAPI(title="Steadfast Triage API", version="0.1.0")


class RunRequest(BaseModel):
    path: str
    limit: Optional[int] = None
    include_internal: bool = False


class TicketRequest(BaseModel):
    ticket_id: str
    subject: str
    body: str
    customer_name: Optional[str] = None
    plan: Optional[str] = None


def _resolve_dataset_path(raw: str) -> Path:
    p = Path(raw)
    if not p.is_absolute():
        p = REPO_ROOT / p
    p = p.resolve()
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Not found: {p}")
    if DATA_DIR.resolve() not in p.parents and p.parent.resolve() != DATA_DIR.resolve():
        raise HTTPException(status_code=400, detail=f"Path must be inside data/: {p}")
    if p.suffix.lower() not in (".csv", ".json"):
        raise HTTPException(status_code=400, detail=f"Only .csv and .json: {p}")
    return p


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/datasets")
def datasets() -> dict:
    if not DATA_DIR.exists():
        return {"datasets": []}
    items = []
    for p in sorted(DATA_DIR.iterdir()):
        if p.suffix.lower() in (".csv", ".json") and p.is_file():
            items.append(
                {
                    "name": p.name,
                    "path": str(p.relative_to(REPO_ROOT)),
                    "size": p.stat().st_size,
                }
            )
    return {"datasets": items}


@app.post("/run")
def run(req: RunRequest) -> dict:
    path = _resolve_dataset_path(req.path)
    tickets = load_tickets(path)
    if req.limit is not None and req.limit >= 0:
        tickets = tickets[: req.limit]

    results = []
    for t in tickets:
        try:
            processed = process_ticket(t)
            results.append(
                processed["internal"] if req.include_internal else processed["final"]
            )
        except Exception as exc:
            results.append(
                {
                    "ticket_id": t.get("ticket_id"),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
    return {"source": str(path), "count": len(results), "results": results}


@app.post("/ticket")
def ticket(req: TicketRequest) -> dict:
    return process_ticket(req.model_dump())


INDEX_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8"/>
  <title>Steadfast Triage</title>
  <style>
    :root { color-scheme: light dark; }
    body { font: 14px/1.4 system-ui, -apple-system, sans-serif; margin: 24px; max-width: 1100px; }
    h1 { margin: 0 0 8px 0; font-size: 18px; }
    .muted { opacity: 0.7; font-size: 12px; }
    fieldset { border: 1px solid #8884; padding: 12px 16px; margin: 12px 0; }
    legend { padding: 0 6px; font-weight: 600; }
    label { display: inline-block; margin-right: 16px; }
    select, input[type=number], input[type=text] { font: inherit; padding: 4px 6px; }
    button { font: inherit; padding: 6px 14px; cursor: pointer; }
    pre { background: #0001; padding: 10px; border-radius: 6px; overflow: auto; max-height: 70vh; }
    .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
    .status { margin-left: 8px; }
  </style>
</head>
<body>
  <h1>Steadfast Triage</h1>
  <div class="muted">Pick a dataset under <code>data/</code>, run the pipeline, inspect results.</div>

  <fieldset>
    <legend>Run on dataset</legend>
    <div class="row">
      <label>Dataset: <select id="ds"></select></label>
      <label>Limit: <input id="limit" type="number" min="0" value="3" style="width: 70px"/></label>
      <label><input id="internal" type="checkbox"/> include internal (debug) fields</label>
      <button onclick="runDataset()">Run</button>
      <span id="status" class="status muted"></span>
    </div>
  </fieldset>

  <fieldset>
    <legend>Run on a single ad-hoc ticket</legend>
    <div class="row">
      <label>ID: <input id="tid" type="text" value="AD-HOC-1" style="width: 120px"/></label>
    </div>
    <div style="margin-top: 6px">
      <input id="tsub" type="text" placeholder="subject" style="width: 100%; margin-bottom: 4px"/>
      <textarea id="tbody" rows="4" placeholder="body" style="width: 100%"></textarea>
    </div>
    <div class="row" style="margin-top: 6px">
      <button onclick="runTicket()">Run single</button>
    </div>
  </fieldset>

  <h3>Output</h3>
  <pre id="out">(waiting)</pre>

<script>
async function loadDatasets() {
  const r = await fetch('/datasets');
  const { datasets } = await r.json();
  const sel = document.getElementById('ds');
  sel.innerHTML = datasets.map(d =>
    `<option value="${d.path}">${d.name} (${d.size} bytes)</option>`
  ).join('');
}
async function runDataset() {
  const path = document.getElementById('ds').value;
  const limit = parseInt(document.getElementById('limit').value || '0', 10);
  const include_internal = document.getElementById('internal').checked;
  const status = document.getElementById('status');
  const out = document.getElementById('out');
  status.textContent = 'running…';
  out.textContent = '';
  const r = await fetch('/run', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ path, limit, include_internal })
  });
  const data = await r.json();
  out.textContent = JSON.stringify(data, null, 2);
  status.textContent = `done (${data.count} tickets)`;
}
async function runTicket() {
  const payload = {
    ticket_id: document.getElementById('tid').value,
    subject: document.getElementById('tsub').value,
    body: document.getElementById('tbody').value,
  };
  const out = document.getElementById('out');
  out.textContent = 'running…';
  const r = await fetch('/ticket', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const data = await r.json();
  out.textContent = JSON.stringify(data, null, 2);
}
loadDatasets();
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML

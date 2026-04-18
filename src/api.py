"""
FastAPI wrapper around the triage pipeline.

Endpoints:
  GET  /               modern HTML UI (dataset picker + streamed results)
  GET  /health         readiness probe
  GET  /datasets       list CSV/JSON files under data/
  POST /run            run pipeline on a dataset, return all results at once
  POST /run_stream     run pipeline on a dataset, stream NDJSON per ticket
  POST /ticket         run pipeline on one ad-hoc ticket

Run locally:  uvicorn src.api:app --reload
Run in Docker: docker compose up api
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import AsyncGenerator, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.pipeline import load_tickets, process_ticket  # noqa: E402

DATA_DIR = REPO_ROOT / "data"

app = FastAPI(title="Steadfast Triage API", version="0.2.0")


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


def _ticket_envelope(t: dict) -> dict:
    return {
        "ticket_id": t.get("ticket_id"),
        "customer_name": t.get("customer_name"),
        "plan": t.get("plan"),
        "subject": t.get("subject"),
        "body": t.get("body"),
    }


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
        t0 = time.perf_counter()
        try:
            processed = process_ticket(t)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            base = processed["internal"] if req.include_internal else processed["final"]
            results.append({**base, "elapsed_ms": elapsed_ms})
        except Exception as exc:
            results.append(
                {
                    "ticket_id": t.get("ticket_id"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                }
            )
    return {"source": str(path), "count": len(results), "results": results}


@app.post("/run_stream")
async def run_stream(req: RunRequest) -> StreamingResponse:
    path = _resolve_dataset_path(req.path)
    tickets = load_tickets(path)
    if req.limit is not None and req.limit >= 0:
        tickets = tickets[: req.limit]

    async def gen() -> AsyncGenerator[bytes, None]:
        start_evt = {
            "event": "start",
            "source": str(path),
            "count": len(tickets),
        }
        yield (json.dumps(start_evt) + "\n").encode("utf-8")
        for t in tickets:
            t0 = time.perf_counter()
            try:
                processed = await asyncio.to_thread(process_ticket, t)
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                payload = {
                    "event": "result",
                    "elapsed_ms": elapsed_ms,
                    "ticket": _ticket_envelope(t),
                    "result": processed["internal"],
                }
            except Exception as exc:
                payload = {
                    "event": "error",
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                    "ticket": _ticket_envelope(t),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            yield (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        yield (json.dumps({"event": "done"}) + "\n").encode("utf-8")

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        gen(), media_type="application/x-ndjson", headers=headers
    )


@app.post("/ticket")
def ticket(req: TicketRequest) -> dict:
    t0 = time.perf_counter()
    processed = process_ticket(req.model_dump())
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return {**processed, "elapsed_ms": elapsed_ms}


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>Steadfast Triage</title>
  <style>
    :root {
      color-scheme: light dark;

      --bg:        #f6f7fb;
      --panel:     #ffffff;
      --panel-2:   #fafbfd;
      --ink:       #161821;
      --ink-soft:  #4a5160;
      --muted:     #8b91a1;
      --line:      #e6e8f0;
      --line-2:    #eef0f6;
      --accent:    #4f46e5;
      --accent-soft: #eef0ff;

      --pri-low:      #5b6473;
      --pri-medium:   #2563eb;
      --pri-high:     #d97706;
      --pri-critical: #dc2626;

      --mode-answer:  #16a34a;
      --mode-check:   #d97706;
      --mode-none:    #6b7280;

      --shadow: 0 1px 2px rgba(20,23,40,0.04), 0 8px 24px rgba(20,23,40,0.06);
      --radius: 14px;
      --radius-sm: 8px;
    }
    @media (prefers-color-scheme: dark) {
      :root {
        --bg:        #0f1115;
        --panel:     #161922;
        --panel-2:   #1b1f2a;
        --ink:       #e7e9ef;
        --ink-soft:  #b9bdca;
        --muted:     #7a8092;
        --line:      #252a36;
        --line-2:    #2c3140;
        --accent:    #818cf8;
        --accent-soft: #232742;

        --pri-low:      #9aa1b1;
        --pri-medium:   #60a5fa;
        --pri-high:     #fbbf24;
        --pri-critical: #f87171;

        --mode-answer:  #34d399;
        --mode-check:   #fbbf24;
        --mode-none:    #9ca3af;

        --shadow: 0 1px 2px rgba(0,0,0,0.3), 0 12px 28px rgba(0,0,0,0.35);
      }
    }

    * { box-sizing: border-box; }
    html, body { margin: 0; padding: 0; }
    body {
      font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI",
            Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--ink);
      min-height: 100vh;
    }
    .wrap { max-width: 1080px; margin: 0 auto; padding: 32px 20px 80px; }

    header.app {
      display: flex; align-items: baseline; justify-content: space-between;
      gap: 16px; margin-bottom: 18px;
    }
    header.app h1 {
      font-size: 22px; font-weight: 700; letter-spacing: -0.01em; margin: 0;
    }
    header.app .sub { color: var(--muted); font-size: 13px; }

    .controls {
      background: var(--panel); border: 1px solid var(--line);
      border-radius: var(--radius); padding: 18px 20px; box-shadow: var(--shadow);
      display: grid; grid-template-columns: 1fr auto auto auto; gap: 12px;
      align-items: end;
    }
    .controls .field { display: flex; flex-direction: column; gap: 6px; }
    .controls label {
      font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em;
      color: var(--muted); font-weight: 600;
    }
    .controls select, .controls input[type=number] {
      font: inherit; padding: 8px 10px; border-radius: var(--radius-sm);
      border: 1px solid var(--line); background: var(--panel-2); color: var(--ink);
      min-width: 0;
    }
    .controls select { min-width: 280px; }
    .controls input[type=number] { width: 90px; text-align: right; }
    .controls button {
      font: inherit; font-weight: 600;
      background: var(--accent); color: white; border: 0;
      border-radius: var(--radius-sm); padding: 9px 18px; cursor: pointer;
      transition: filter 0.15s;
    }
    .controls button:hover { filter: brightness(1.06); }
    .controls button:disabled { opacity: 0.5; cursor: not-allowed; }

    .status-bar {
      display: flex; align-items: center; gap: 14px;
      margin: 16px 2px 4px; min-height: 22px; color: var(--muted); font-size: 13px;
    }
    .status-bar .dot {
      width: 8px; height: 8px; border-radius: 50%; background: var(--muted);
    }
    .status-bar.running .dot {
      background: var(--accent); animation: pulse 1.2s ease-in-out infinite;
    }
    .status-bar.done .dot { background: var(--mode-answer); }
    @keyframes pulse {
      0%, 100% { opacity: 0.4; transform: scale(0.9); }
      50%      { opacity: 1;   transform: scale(1.15); }
    }

    .results { display: flex; flex-direction: column; gap: 16px; margin-top: 12px; }

    .card {
      background: var(--panel); border: 1px solid var(--line);
      border-radius: var(--radius); box-shadow: var(--shadow);
      overflow: hidden;
      animation: cardin 0.25s ease-out;
    }
    @keyframes cardin {
      from { opacity: 0; transform: translateY(6px); }
      to   { opacity: 1; transform: translateY(0); }
    }

    .card-head {
      display: flex; align-items: center; justify-content: space-between;
      gap: 12px; padding: 14px 18px;
      border-bottom: 1px solid var(--line-2);
      background: var(--panel-2);
    }
    .card-id { display: flex; align-items: baseline; gap: 8px; min-width: 0; }
    .card-id .id { font-weight: 700; letter-spacing: -0.005em; }
    .card-id .who { color: var(--muted); font-size: 12.5px; }
    .card-meta { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
    .elapsed {
      display: inline-flex; align-items: center; gap: 4px;
      font-variant-numeric: tabular-nums; color: var(--ink-soft);
      font-weight: 600; font-size: 12.5px;
      padding: 4px 8px; border-radius: 999px;
      background: var(--accent-soft);
    }

    .pill {
      display: inline-block; padding: 3px 9px; border-radius: 999px;
      font-size: 11px; font-weight: 700;
      text-transform: uppercase; letter-spacing: 0.05em;
      background: var(--line-2); color: var(--ink-soft);
      border: 1px solid var(--line);
    }
    .pill[data-pri=low]      { color: var(--pri-low); }
    .pill[data-pri=medium]   { color: var(--pri-medium); }
    .pill[data-pri=high]     { color: var(--pri-high); }
    .pill[data-pri=critical] { color: var(--pri-critical); }
    .pill[data-mode=answer_found]      { color: var(--mode-answer); }
    .pill[data-mode=needs_human_check] { color: var(--mode-check); }
    .pill[data-mode=no_relevant_answer]{ color: var(--mode-none); }

    .email { padding: 4px 18px 0; }
    .email-block { padding: 14px 0; border-bottom: 1px dashed var(--line-2); }
    .email-block:last-child { border-bottom: 0; }
    .email-label {
      display: flex; align-items: center; gap: 8px;
      font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
      color: var(--muted); font-weight: 700; margin-bottom: 6px;
    }
    .email-label .conf {
      color: var(--ink-soft); font-weight: 600;
      text-transform: none; letter-spacing: 0; font-size: 11.5px;
      padding: 1px 7px; background: var(--line-2);
      border-radius: 999px;
    }
    .email-subject {
      font-weight: 600; font-size: 14.5px; color: var(--ink);
      margin-bottom: 6px;
    }
    .email-body {
      color: var(--ink); white-space: pre-wrap; word-wrap: break-word;
      font-size: 13.5px; line-height: 1.55;
    }
    .email-block.response { background: linear-gradient(180deg, transparent, var(--accent-soft) 200%); }

    .flags { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
    .flag {
      font-size: 11px; padding: 2px 7px; border-radius: 6px;
      background: var(--line-2); color: var(--ink-soft);
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    }

    details.internals {
      border-top: 1px solid var(--line-2);
      background: var(--panel-2);
    }
    details.internals > summary {
      list-style: none; cursor: pointer; user-select: none;
      padding: 10px 18px; font-size: 12px; color: var(--muted);
      font-weight: 600; letter-spacing: 0.04em; text-transform: uppercase;
      display: flex; align-items: center; gap: 6px;
    }
    details.internals > summary::-webkit-details-marker { display: none; }
    details.internals > summary::before {
      content: "▸"; transition: transform 0.15s; display: inline-block;
    }
    details.internals[open] > summary::before { transform: rotate(90deg); }

    .internals-body { padding: 6px 18px 18px; }
    .kv { display: grid; grid-template-columns: 200px 1fr; gap: 12px; padding: 4px 0; }
    .kv .k { color: var(--muted); font-size: 12px; }
    .kv .v { color: var(--ink-soft); font-size: 13px; word-break: break-word; }
    .kv .v.mono {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px;
    }

    .chunks { display: flex; flex-direction: column; gap: 6px; margin-top: 8px; }
    .chunk {
      border: 1px solid var(--line-2); border-radius: var(--radius-sm);
      background: var(--panel); overflow: hidden;
    }
    .chunk > summary {
      list-style: none; cursor: pointer; user-select: none;
      display: grid;
      grid-template-columns: minmax(70px, auto) minmax(80px, auto) minmax(60px, auto) 1fr auto;
      gap: 10px; align-items: center;
      padding: 8px 12px; font-size: 12.5px;
    }
    .chunk > summary::-webkit-details-marker { display: none; }
    .chunk > summary::before {
      content: "▸"; color: var(--muted);
      transition: transform 0.15s; display: inline-block; margin-right: -4px;
    }
    .chunk[open] > summary::before { transform: rotate(90deg); }
    .chunk .c-id  {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      color: var(--ink); font-weight: 600;
    }
    .chunk .c-cat, .chunk .c-pri {
      font-size: 11px; color: var(--ink-soft);
    }
    .chunk .c-subject {
      color: var(--ink); white-space: nowrap;
      overflow: hidden; text-overflow: ellipsis;
    }
    .chunk .c-score {
      font-variant-numeric: tabular-nums; color: var(--ink-soft);
      font-size: 12px; padding: 2px 7px; border-radius: 999px;
      background: var(--accent-soft);
    }
    .chunk-body {
      padding: 6px 12px 12px; border-top: 1px solid var(--line-2);
      background: var(--panel-2);
      display: flex; flex-direction: column; gap: 8px;
    }
    .chunk-body .c-label {
      font-size: 10.5px; text-transform: uppercase; letter-spacing: 0.06em;
      color: var(--muted); font-weight: 700; margin-top: 4px;
    }
    .chunk-body .c-subject-full {
      font-weight: 600; font-size: 13.5px; color: var(--ink);
    }
    .chunk-body .c-text {
      color: var(--ink); white-space: pre-wrap; word-wrap: break-word;
      font-size: 13px; line-height: 1.5;
    }

    .card.error { border-color: var(--pri-critical); }
    .card.error .email-block { color: var(--pri-critical); }

    .empty {
      text-align: center; color: var(--muted); padding: 60px 0;
      font-size: 14px;
    }

    @media (max-width: 720px) {
      .controls { grid-template-columns: 1fr; }
      .controls select, .controls input[type=number] { width: 100%; min-width: 0; }
      .kv { grid-template-columns: 1fr; gap: 2px; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <header class="app">
      <div>
        <h1>Steadfast Triage</h1>
        <div class="sub">Pick a dataset, run the pipeline, inspect each reply as it arrives.</div>
      </div>
    </header>

    <section class="controls">
      <div class="field">
        <label for="ds">Dataset</label>
        <select id="ds"></select>
      </div>
      <div class="field">
        <label for="limit">Limit</label>
        <input id="limit" type="number" min="0" value="3"/>
      </div>
      <div class="field">
        <label>&nbsp;</label>
        <button id="run-btn" onclick="runDataset()">Run</button>
      </div>
      <div class="field">
        <label>&nbsp;</label>
        <button id="cancel-btn" onclick="cancelRun()" disabled
                style="background: var(--line-2); color: var(--ink-soft);">Cancel</button>
      </div>
    </section>

    <div class="status-bar" id="status-bar">
      <span class="dot"></span>
      <span id="status-text">Ready.</span>
    </div>

    <div class="results" id="results">
      <div class="empty">No results yet. Pick a dataset and hit <strong>Run</strong>.</div>
    </div>
  </div>

<script>
let abortCtrl = null;

async function loadDatasets() {
  const r = await fetch('/datasets');
  const { datasets } = await r.json();
  const sel = document.getElementById('ds');
  sel.innerHTML = datasets.map(d =>
    `<option value="${d.path}">${d.name}  (${d.size.toLocaleString()} bytes)</option>`
  ).join('');
}

function setStatus(text, state /* "" | "running" | "done" | "error" */) {
  const bar = document.getElementById('status-bar');
  const txt = document.getElementById('status-text');
  bar.className = 'status-bar' + (state ? ' ' + state : '');
  txt.textContent = text;
}

function clearResults() {
  document.getElementById('results').innerHTML = '';
}

function escapeHtml(s) {
  return (s ?? '').toString()
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#039;');
}

function fmtElapsed(ms) {
  if (ms < 1000) return ms + ' ms';
  return (ms / 1000).toFixed(2) + ' s';
}

function flagsHtml(flags) {
  if (!flags || !flags.length) return '';
  return `<div class="flags">${flags.map(f => `<span class="flag">${escapeHtml(f)}</span>`).join('')}</div>`;
}

function retrievedChunks(retrieved) {
  if (!retrieved || !retrieved.length) return '<em>(none)</em>';
  return `<div class="chunks">${retrieved.map(r => `
    <details class="chunk">
      <summary>
        <span class="c-id">${escapeHtml(r.ticket_id)}</span>
        <span class="c-cat">${escapeHtml(r.category)}</span>
        <span class="c-pri">${escapeHtml(r.priority)}</span>
        <span class="c-subject">${escapeHtml(r.subject)}</span>
        <span class="c-score">${(r.score ?? 0).toFixed(3)}</span>
      </summary>
      <div class="chunk-body">
        <div class="c-subject-full">${escapeHtml(r.subject)}</div>
        <div class="c-label">Body</div>
        <div class="c-text">${escapeHtml(r.body || '(empty)')}</div>
        <div class="c-label">Resolution</div>
        <div class="c-text">${escapeHtml(r.resolution || '(empty)')}</div>
      </div>
    </details>`).join('')}</div>`;
}

function renderResultCard(evt) {
  const t = evt.ticket || {};
  const r = evt.result || {};
  const who = [t.customer_name, t.plan].filter(Boolean).join(' · ');
  const conf = (r.confidence ?? 0).toFixed(2);
  const cConf = (r.classification_confidence ?? 0).toFixed(2);

  const card = document.createElement('article');
  card.className = 'card';
  card.innerHTML = `
    <header class="card-head">
      <div class="card-id">
        <span class="id">${escapeHtml(t.ticket_id)}</span>
        ${who ? `<span class="who">· ${escapeHtml(who)}</span>` : ''}
      </div>
      <div class="card-meta">
        <span class="pill" data-cat="${escapeHtml(r.category)}">${escapeHtml(r.category)}</span>
        <span class="pill" data-pri="${escapeHtml(r.priority)}">${escapeHtml(r.priority)}</span>
        <span class="pill" data-mode="${escapeHtml(r.response_mode)}">${escapeHtml(r.response_mode || '—')}</span>
        <span class="elapsed">⏱ ${fmtElapsed(evt.elapsed_ms)}</span>
      </div>
    </header>

    <div class="email">
      <div class="email-block">
        <div class="email-label">Customer message</div>
        <div class="email-subject">${escapeHtml(t.subject)}</div>
        <div class="email-body">${escapeHtml(t.body)}</div>
      </div>
      <div class="email-block response">
        <div class="email-label">
          AI reply
          <span class="conf">confidence ${conf}</span>
        </div>
        <div class="email-body">${escapeHtml(r.response)}</div>
        ${flagsHtml(r.flags)}
      </div>
    </div>

    <details class="internals">
      <summary>Show internals</summary>
      <div class="internals-body">
        <div class="kv"><span class="k">response_mode</span>           <span class="v">${escapeHtml(r.response_mode)}</span></div>
        <div class="kv"><span class="k">response confidence</span>     <span class="v">${conf}</span></div>
        <div class="kv"><span class="k">classification_confidence</span><span class="v">${cConf}</span></div>
        <div class="kv"><span class="k">flags</span>                   <span class="v mono">${escapeHtml((r.flags||[]).join(', ') || '—')}</span></div>
        <div class="kv"><span class="k">retrieval_query</span>         <span class="v mono">${escapeHtml(r.retrieval_query || '—')}</span></div>
        <div class="kv"><span class="k">retrieved (top ${(r.retrieved||[]).length})</span><span class="v">${retrievedChunks(r.retrieved)}</span></div>
      </div>
    </details>
  `;
  document.getElementById('results').appendChild(card);
}

function renderErrorCard(evt) {
  const t = evt.ticket || {};
  const card = document.createElement('article');
  card.className = 'card error';
  card.innerHTML = `
    <header class="card-head">
      <div class="card-id">
        <span class="id">${escapeHtml(t.ticket_id || '?')}</span>
      </div>
      <div class="card-meta">
        <span class="pill" data-mode="no_relevant_answer">error</span>
        <span class="elapsed">⏱ ${fmtElapsed(evt.elapsed_ms || 0)}</span>
      </div>
    </header>
    <div class="email">
      <div class="email-block">
        <div class="email-label">Pipeline error</div>
        <div class="email-body">${escapeHtml(evt.error)}</div>
      </div>
      ${t.subject ? `<div class="email-block">
        <div class="email-label">Customer message</div>
        <div class="email-subject">${escapeHtml(t.subject)}</div>
        <div class="email-body">${escapeHtml(t.body)}</div>
      </div>` : ''}
    </div>
  `;
  document.getElementById('results').appendChild(card);
}

async function runDataset() {
  const path = document.getElementById('ds').value;
  const limit = parseInt(document.getElementById('limit').value || '0', 10);
  const runBtn = document.getElementById('run-btn');
  const cancelBtn = document.getElementById('cancel-btn');

  clearResults();
  runBtn.disabled = true;
  cancelBtn.disabled = false;
  setStatus('Connecting…', 'running');

  abortCtrl = new AbortController();
  const tStart = performance.now();
  let total = 0, done = 0;

  try {
    const resp = await fetch('/run_stream', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ path, limit }),
      signal: abortCtrl.signal,
    });
    if (!resp.ok) {
      const txt = await resp.text();
      throw new Error('HTTP ' + resp.status + ': ' + txt.slice(0, 400));
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    while (true) {
      const { value, done: streamDone } = await reader.read();
      if (streamDone) break;
      buf += decoder.decode(value, { stream: true });
      const lines = buf.split('\n');
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let evt;
        try { evt = JSON.parse(line); } catch { continue; }
        if (evt.event === 'start') {
          total = evt.count;
          setStatus(`Running on ${total} ticket${total===1?'':'s'}…`, 'running');
        } else if (evt.event === 'result') {
          done++;
          renderResultCard(evt);
          setStatus(`${done} / ${total} done · ${((performance.now()-tStart)/1000).toFixed(1)}s`, 'running');
        } else if (evt.event === 'error') {
          done++;
          renderErrorCard(evt);
          setStatus(`${done} / ${total} done (with errors)`, 'running');
        } else if (evt.event === 'done') {
          setStatus(`Finished ${done} / ${total} in ${((performance.now()-tStart)/1000).toFixed(1)}s`, 'done');
        }
      }
    }
  } catch (err) {
    if (err.name === 'AbortError') {
      setStatus(`Cancelled after ${done} / ${total}`, '');
    } else {
      setStatus('Error: ' + err.message, 'error');
    }
  } finally {
    runBtn.disabled = false;
    cancelBtn.disabled = true;
    abortCtrl = null;
  }
}

function cancelRun() {
  if (abortCtrl) abortCtrl.abort();
}

loadDatasets();
</script>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return INDEX_HTML

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
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.evaluate import dataset_has_expected_labels, error_analysis, summarize  # noqa: E402
from src.pipeline import load_tickets, process_ticket  # noqa: E402

DATA_DIR = REPO_ROOT / "data"
OUTPUT_DIR = REPO_ROOT / "output"
STATIC_DIR = Path(__file__).resolve().parent / "static"

app = FastAPI(title="Steadfast Triage API", version="0.3.0")


class RunRequest(BaseModel):
    path: str
    limit: Optional[int] = None
    include_internal: bool = False
    evaluate: bool = True


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


def _write_output_files(
    results: list[dict], source: Path, evaluated: bool
) -> dict:
    """Persist the run to output/eval_results.json (+ error_analysis.json
    when eval was on). Returns a dict with relative paths for the client."""
    OUTPUT_DIR.mkdir(exist_ok=True)
    payload: dict = {
        "source": str(source.relative_to(REPO_ROOT)),
        "count": len(results),
        "results": results,
    }
    if evaluated:
        payload["summary"] = summarize(results)

    eval_path = OUTPUT_DIR / "eval_results.json"
    with eval_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    written = {"eval_results": str(eval_path.relative_to(REPO_ROOT))}
    if evaluated:
        err_path = OUTPUT_DIR / "error_analysis.json"
        with err_path.open("w", encoding="utf-8") as f:
            json.dump(error_analysis(results), f, ensure_ascii=False, indent=2)
        written["error_analysis"] = str(err_path.relative_to(REPO_ROOT))
    return written


@app.post("/run")
def run(req: RunRequest) -> dict:
    path = _resolve_dataset_path(req.path)
    tickets = load_tickets(path)
    if req.limit is not None and req.limit >= 0:
        tickets = tickets[: req.limit]

    evaluate = bool(req.evaluate) and dataset_has_expected_labels(tickets)

    results = []
    persist_records = []
    for t in tickets:
        t0 = time.perf_counter()
        try:
            processed = process_ticket(t, evaluate=evaluate)
            elapsed_ms = int((time.perf_counter() - t0) * 1000)
            base = processed["internal"] if req.include_internal else processed["final"]
            results.append({**base, "elapsed_ms": elapsed_ms})
            persist_records.append(processed["internal"])
        except Exception as exc:
            err = {
                "ticket_id": t.get("ticket_id"),
                "error": f"{type(exc).__name__}: {exc}",
                "elapsed_ms": int((time.perf_counter() - t0) * 1000),
            }
            results.append(err)
            persist_records.append(err)

    written = _write_output_files(persist_records, path, evaluated=evaluate)
    return {
        "source": str(path),
        "count": len(results),
        "evaluated": evaluate,
        "results": results,
        "summary": summarize(persist_records) if evaluate else None,
        "output": written,
    }


@app.post("/run_stream")
async def run_stream(req: RunRequest) -> StreamingResponse:
    path = _resolve_dataset_path(req.path)
    tickets = load_tickets(path)
    if req.limit is not None and req.limit >= 0:
        tickets = tickets[: req.limit]

    # User can ask for evaluation, but if the dataset has no gold labels we
    # silently turn it off and keep running.
    evaluate_requested = bool(req.evaluate)
    evaluate = evaluate_requested and dataset_has_expected_labels(tickets)

    async def gen() -> AsyncGenerator[bytes, None]:
        start_evt = {
            "event": "start",
            "source": str(path),
            "count": len(tickets),
            "evaluate": evaluate,
            "evaluate_requested": evaluate_requested,
        }
        yield (json.dumps(start_evt) + "\n").encode("utf-8")

        collected: list[dict] = []
        for t in tickets:
            t0 = time.perf_counter()
            try:
                processed = await asyncio.to_thread(
                    process_ticket, t, evaluate=evaluate
                )
                elapsed_ms = int((time.perf_counter() - t0) * 1000)
                internal = processed["internal"]
                collected.append(internal)
                payload = {
                    "event": "result",
                    "elapsed_ms": elapsed_ms,
                    "ticket": _ticket_envelope(t),
                    "result": internal,
                }
                if evaluate:
                    payload["running"] = summarize(collected)
            except Exception as exc:
                err = {
                    "ticket_id": t.get("ticket_id"),
                    "error": f"{type(exc).__name__}: {exc}",
                    "elapsed_ms": int((time.perf_counter() - t0) * 1000),
                }
                collected.append(err)
                payload = {
                    "event": "error",
                    "elapsed_ms": err["elapsed_ms"],
                    "ticket": _ticket_envelope(t),
                    "error": err["error"],
                }
            yield (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")

        written = _write_output_files(collected, path, evaluated=evaluate)
        done_evt: dict = {
            "event": "done",
            "count": len(collected),
            "output": written,
        }
        if evaluate:
            done_evt["summary"] = summarize(collected)
        yield (json.dumps(done_evt, ensure_ascii=False) + "\n").encode("utf-8")

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(
        gen(), media_type="application/x-ndjson", headers=headers
    )


@app.get("/outputs/{name}")
def outputs(name: str):
    """Download a file from the output/ directory (eval_results.json etc.)."""
    if "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(status_code=400, detail="invalid name")
    p = (OUTPUT_DIR / name).resolve()
    if OUTPUT_DIR.resolve() not in p.parents:
        raise HTTPException(status_code=400, detail="outside output/")
    if not p.exists():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(p, media_type="application/json", filename=name)


@app.post("/ticket")
def ticket(req: TicketRequest) -> dict:
    t0 = time.perf_counter()
    processed = process_ticket(req.model_dump())
    elapsed_ms = int((time.perf_counter() - t0) * 1000)
    return {**processed, "elapsed_ms": elapsed_ms}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")

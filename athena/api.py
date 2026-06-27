"""
Project Athena — FastAPI REST server.

Endpoints
---------
POST /athena/runs            Kick off a new Athena production loop.
GET  /athena/runs/{run_id}   Retrieve a completed or cached run.
GET  /athena/health          Liveness probe.
"""

from __future__ import annotations

import threading
from typing import Dict, Optional

from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel

from athena.models import AthenaRun, RunStatus
from athena.orchestrator import AthenaOrchestrator

app = FastAPI(
    title="Project Athena Orchestrator",
    description="End-to-end hardware production loop: Demand → Sourcing → Simulation → Execution → Quality",
    version="1.0.0",
)

# In-memory run store (replace with Redis / DynamoDB in production).
_runs: Dict[str, AthenaRun] = {}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class TriggerRequest(BaseModel):
    deal_id: Optional[str] = None
    notes: Optional[str] = None


class TriggerResponse(BaseModel):
    run_id: str
    status: RunStatus
    message: str


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

def _execute_run(run_id: str, deal_id: Optional[str]) -> None:
    orchestrator = AthenaOrchestrator()
    result = orchestrator.run(deal_id=deal_id)
    result.run_id = run_id  # keep the pre-assigned ID
    with _lock:
        _runs[run_id] = result


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/athena/health")
def health() -> dict:
    return {"status": "ok", "service": "athena-orchestrator"}


@app.post("/athena/runs", response_model=TriggerResponse, status_code=202)
def trigger_run(body: TriggerRequest, background_tasks: BackgroundTasks) -> TriggerResponse:
    """
    Kick off a new Project Athena production loop.
    The run executes asynchronously; poll GET /athena/runs/{run_id} for results.
    """
    import uuid
    run_id = f"ATH-{uuid.uuid4().hex[:10].upper()}"

    # Seed a PENDING placeholder immediately so callers can track the run.
    placeholder = AthenaRun(run_id=run_id, status=RunStatus.PENDING)
    with _lock:
        _runs[run_id] = placeholder

    background_tasks.add_task(_execute_run, run_id, body.deal_id)

    return TriggerResponse(
        run_id=run_id,
        status=RunStatus.PENDING,
        message=f"Athena run {run_id} queued. Poll /athena/runs/{run_id} for results.",
    )


@app.get("/athena/runs/{run_id}", response_model=AthenaRun)
def get_run(run_id: str) -> AthenaRun:
    """Retrieve the full state of a production run."""
    with _lock:
        run = _runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found.")
    return run


@app.get("/athena/runs", response_model=Dict[str, RunStatus])
def list_runs() -> Dict[str, RunStatus]:
    """List all runs and their current statuses."""
    with _lock:
        return {rid: r.status for rid, r in _runs.items()}

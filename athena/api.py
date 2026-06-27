"""
Project Athena — FastAPI REST server.

Endpoints
---------
POST /athena/runs              Kick off a new Athena production loop.
GET  /athena/runs/{run_id}     Retrieve a completed or cached run.
GET  /athena/health            Liveness probe.
POST /athena/rca/{run_id}      Trigger RCA analysis on a completed run via RAG.
GET  /athena/rca/{run_id}      Retrieve cached RCA results.
GET  /athena/predict           Predict run risk & yield via RAG (pre-run).

Optional: pass rag_pipeline=RAGPipeline() to enable RAG integration.
"""

from __future__ import annotations

import threading
from typing import Dict, Optional, TYPE_CHECKING

from fastapi import FastAPI, BackgroundTasks, HTTPException
from pydantic import BaseModel

from athena.models import AthenaRun, RunStatus
from athena.orchestrator import AthenaOrchestrator

if TYPE_CHECKING:
    from rag.pipeline import RAGPipeline

app = FastAPI(
    title="Project Athena Orchestrator",
    description="End-to-end hardware production loop: Demand → Sourcing → Simulation → Execution → Quality",
    version="1.0.0",
)

# In-memory run store (replace with Redis / DynamoDB in production).
_runs: Dict[str, AthenaRun] = {}
_rca_results: Dict[str, dict] = {}
_lock = threading.Lock()

# Optional RAG pipeline integration (set by create_app)
_rag_pipeline: Optional[RAGPipeline] = None


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
    orchestrator = AthenaOrchestrator(rag_pipeline=_rag_pipeline)
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


# ---------------------------------------------------------------------------
# RAG Integration Endpoints
# ---------------------------------------------------------------------------

@app.post("/athena/rca/{run_id}")
def trigger_rca(run_id: str, background_tasks: BackgroundTasks) -> dict:
    """
    Trigger root-cause analysis on a completed Athena run.
    Correlates quality results with FMEA and queries RAG for defect context.
    """
    with _lock:
        run = _runs.get(run_id)
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found.")

    if not _rag_pipeline:
        raise HTTPException(
            status_code=501,
            detail="RAG pipeline not configured. Configure via create_app(rag_pipeline=...)",
        )

    def _run_rca(rid: str) -> None:
        from athena.rag_integration import AthenaRAGBridge

        with _lock:
            r = _runs.get(rid)
        if not r or not r.quality:
            return

        bridge = AthenaRAGBridge(rag_pipeline=_rag_pipeline)
        result = bridge.correlate_quality_with_fmea(r.quality)
        with _lock:
            _rca_results[rid] = result

    background_tasks.add_task(_run_rca, run_id)
    return {
        "run_id": run_id,
        "message": f"RCA analysis queued. Poll /athena/rca/{run_id} for results.",
    }


@app.get("/athena/rca/{run_id}")
def get_rca(run_id: str) -> dict:
    """Retrieve cached RCA results for a run."""
    with _lock:
        result = _rca_results.get(run_id)
    if not result:
        raise HTTPException(status_code=404, detail=f"RCA results for {run_id!r} not found.")
    return result


@app.post("/athena/predict")
def predict_run_risk(deal_id: str, build_qty: int = 100) -> dict:
    """
    Predict run risk and yield for a deal before Athena execution.
    Uses RAG to find similar historical runs.
    """
    if not _rag_pipeline:
        raise HTTPException(
            status_code=501,
            detail="RAG pipeline not configured.",
        )

    from athena.rag_integration import AthenaRAGBridge

    bridge = AthenaRAGBridge(rag_pipeline=_rag_pipeline)
    prediction = bridge.predict_run_risk(deal_id=deal_id, build_qty=build_qty)
    return {
        "deal_id": deal_id,
        "build_qty": build_qty,
        "prediction": prediction,
    }


# ---------------------------------------------------------------------------
# App factory function
# ---------------------------------------------------------------------------

def create_app(rag_pipeline: Optional[RAGPipeline] = None) -> FastAPI:
    """Create and configure the Athena FastAPI app with optional RAG integration."""
    global _rag_pipeline
    _rag_pipeline = rag_pipeline
    return app

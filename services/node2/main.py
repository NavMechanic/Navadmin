"""
Project Athena — Node 2: Micro-PLM & Stage-Gate (Port 8002)

Handles product lifecycle management with governance gates.
Consumes: demand.updated, quality.spc_alert, fleet.capa_triggered
Publishes: gate.status_changed events
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from typing import Optional, List
from enum import Enum

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

LOG_LEVEL = os.getenv("ATHENA_LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# ============================================================================
# Types
# ============================================================================

class GateStage(str, Enum):
    DESIGN = "design"
    VALIDATION = "validation"
    RELEASE = "release"
    PRODUCTION = "production"


class GateStatus(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    HOLD = "hold"


class GateEvaluation(BaseModel):
    gate_id: str
    design_id: str
    stage: GateStage
    status: GateStatus
    evaluator: str
    evidence: dict
    notes: Optional[str] = None


class DesignBOM(BaseModel):
    design_id: str
    revision: str
    components: List[dict]
    total_cost: float


class FMEAEntry(BaseModel):
    design_id: str
    row_id: str
    function: str
    failure_mode: str
    occurrence: int
    severity: int
    detection: int
    current_rpn: int


class CAPAProposal(BaseModel):
    capa_id: str
    design_id: str
    issue_description: str
    proposed_action: str
    proposer: str
    status: str  # OPEN, IN_PROGRESS, RESOLVED


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title="Node 2 — Micro-PLM & Stage-Gate",
    description="Product lifecycle management with governance gates",
    version="1.0.0",
)

# In-memory stores
gates_store: dict = {}
designs_store: dict = {}
fmea_store: dict = {}
capa_store: dict = {}


# ============================================================================
# Middleware
# ============================================================================

@app.middleware("http")
async def verify_auth(request: Request, call_next):
    if request.url.path == "/health":
        return await call_next(request)

    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing auth token")

    request.state.user = request.headers.get("x-athena-user", "unknown")
    return await call_next(request)


# ============================================================================
# Health
# ============================================================================

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "node2-plm-gates"}


# ============================================================================
# Gate Endpoints
# ============================================================================

@app.get("/api/v1/gates")
async def list_gates(stage: Optional[str] = None, status: Optional[str] = None) -> dict:
    """List all design gates with optional filtering."""
    gates = list(gates_store.values())

    if stage:
        gates = [g for g in gates if g.get("stage") == stage]
    if status:
        gates = [g for g in gates if g.get("status") == status]

    return {"gates": gates}


@app.post("/api/v1/gates/{gate_id}/evaluate")
async def evaluate_gate(gate_id: str, body: GateEvaluation) -> dict:
    """Evaluate a design gate (PASS/FAIL/HOLD)."""
    logger.info(f"Evaluating gate {gate_id} by {body.evaluator}")

    gates_store[gate_id] = {
        "gate_id": gate_id,
        "design_id": body.design_id,
        "stage": body.stage.value,
        "status": body.status.value,
        "evaluator": body.evaluator,
        "evidence": body.evidence,
        "notes": body.notes,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }

    # TODO: Publish gate.status_changed event to Kafka

    return {"gate_id": gate_id, "status": body.status.value, "message": "Gate evaluated"}


# ============================================================================
# Design & BOM
# ============================================================================

@app.get("/api/v1/designs/{design_id}/bom")
async def get_design_bom(design_id: str) -> DesignBOM:
    """Get bill of materials for design."""
    if design_id not in designs_store:
        # Generate mock BOM
        designs_store[design_id] = {
            "design_id": design_id,
            "revision": "1.0",
            "components": [
                {
                    "part_number": "nRF52840",
                    "manufacturer": "Nordic",
                    "quantity": 1,
                    "unit_cost": 12.50,
                },
                {
                    "part_number": "ICR18650",
                    "manufacturer": "Sanyo",
                    "quantity": 2,
                    "unit_cost": 5.00,
                },
            ],
            "total_cost": 22.50,
        }

    return DesignBOM(**designs_store[design_id])


# ============================================================================
# FMEA
# ============================================================================

@app.get("/api/v1/fmea/{design_id}")
async def get_fmea(design_id: str) -> dict:
    """Get FMEA worksheet for design."""
    if design_id not in fmea_store:
        # Generate mock FMEA
        fmea_store[design_id] = {
            "design_id": design_id,
            "entries": [
                {
                    "row_id": "F001",
                    "function": "Supply power",
                    "failure_mode": "Battery drain",
                    "occurrence": 3,
                    "severity": 7,
                    "detection": 5,
                    "current_rpn": 105,
                },
                {
                    "row_id": "F002",
                    "function": "Wireless comm",
                    "failure_mode": "Connection loss",
                    "occurrence": 4,
                    "severity": 8,
                    "detection": 6,
                    "current_rpn": 192,
                },
            ]
        }

    return fmea_store[design_id]


@app.post("/api/v1/fmea/{design_id}/{row_id}/re-rate")
async def re_rate_fmea_row(design_id: str, row_id: str, body: dict) -> dict:
    """Re-rate FMEA row (privilege: fmea_expert role only)."""
    logger.info(f"Re-rating FMEA {design_id}:{row_id}")

    # Privilege check (in real app, verify JWT role)
    # user_roles = get_user_roles(request.state.user)
    # if "fmea_expert" not in user_roles:
    #     raise HTTPException(status_code=403, detail="Insufficient permissions")

    return {
        "row_id": row_id,
        "new_rpn": body.get("new_rpn", 0),
        "message": "FMEA row re-rated",
    }


# ============================================================================
# CAPA
# ============================================================================

@app.get("/api/v1/capa")
async def list_capa() -> dict:
    """List all CAPA proposals."""
    return {"capa_proposals": list(capa_store.values())}


@app.post("/api/v1/capa")
async def create_capa(body: CAPAProposal) -> dict:
    """Create CAPA proposal."""
    capa_store[body.capa_id] = {
        "capa_id": body.capa_id,
        "design_id": body.design_id,
        "issue_description": body.issue_description,
        "proposed_action": body.proposed_action,
        "proposer": body.proposer,
        "status": body.status,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return {"capa_id": body.capa_id, "status": "created"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002, log_level=LOG_LEVEL.lower())

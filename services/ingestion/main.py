"""
Project Athena — Ingestion Service (Port 8007)

PDF, CAD, telemetry parsing and buffering with operator review queue.
Endpoints: /ingest/pdf, /ingest/cad, /ingest/fleet/telemetry
Outputs: To RAG vector store and to Node 6 telemetry sink
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from typing import Optional, List
import uuid

from fastapi import FastAPI, HTTPException, Request, UploadFile, File
from pydantic import BaseModel

LOG_LEVEL = os.getenv("ATHENA_LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# ============================================================================
# Types
# ============================================================================

class IngestionRequest(BaseModel):
    content_type: str  # pdf, cad, telemetry
    source: str  # file path or device
    metadata: Optional[dict] = None


class QueuedIngestion(BaseModel):
    ingestion_id: str
    content_type: str
    source: str
    status: str  # PENDING, APPROVED, REJECTED
    parsed_metadata: dict
    created_at: str
    approved_by: Optional[str] = None
    approved_at: Optional[str] = None


# ============================================================================
# Parsing Engines (Mock)
# ============================================================================

class PDFParser:
    """Extract metadata and chunk content from PDF."""

    @staticmethod
    def parse(content: bytes) -> dict:
        """Parse PDF and extract sections."""
        # Mock: return extracted metadata
        return {
            "title": "Datasheet WiFi Beacon v2.0",
            "pages": 12,
            "sections": ["Overview", "Specifications", "Thermal Limits", "Reliability"],
            "extracted_text": "Mock PDF content...",
        }


class CADParser:
    """Extract geometry, materials, thermal properties from CAD."""

    @staticmethod
    def parse(content: bytes) -> dict:
        """Parse CAD file and extract properties."""
        # Mock: return extracted geometry & properties
        return {
            "model_name": "WiFi Beacon v2.0",
            "format": "STEP",
            "board_size_mm": [85, 65],
            "layer_count": 4,
            "components_count": 6,
            "thermal_properties": {
                "copper_conductivity_w_mk": 385,
                "pcb_conductivity_w_mk": 0.3,
            },
            "extracted_geometry": "Mock CAD geometry...",
        }


class TelemetryParser:
    """Parse and validate incoming telemetry."""

    @staticmethod
    def parse(payload: dict) -> dict:
        """Validate telemetry schema."""
        required_fields = ["device_id", "timestamp"]
        for field in required_fields:
            if field not in payload:
                raise ValueError(f"Missing required field: {field}")

        return {
            "device_id": payload.get("device_id"),
            "battery_health_pct": payload.get("battery_health_pct", 100),
            "operating_temp_c": payload.get("operating_temp_c", 25),
            "error_codes": payload.get("error_codes", []),
            "firmware_version": payload.get("firmware_version", ""),
            "timestamp": payload.get("timestamp"),
        }


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title="Ingestion Service",
    description="PDF, CAD, telemetry parsing with operator review queue",
    version="1.0.0",
)

# In-memory queue
ingestion_queue: dict = {}


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
    return {
        "status": "ok",
        "service": "ingestion",
        "queue_size": len(ingestion_queue),
    }


# ============================================================================
# PDF Ingestion
# ============================================================================

@app.post("/ingest/pdf")
async def ingest_pdf(request: Request, file: UploadFile = File(...)) -> dict:
    """
    Ingest PDF (datasheet, design doc, etc).
    Parse and buffer for operator review.
    """
    ingestion_id = f"ing_{uuid.uuid4().hex[:8]}"
    user = request.state.user

    logger.info(f"Ingesting PDF: {file.filename} (from {user})")

    # Read file content
    content = await file.read()

    # Parse PDF
    try:
        parsed = PDFParser.parse(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"PDF parse error: {str(e)}")

    # Queue for operator review
    ingestion_queue[ingestion_id] = {
        "ingestion_id": ingestion_id,
        "content_type": "pdf",
        "source": file.filename,
        "status": "PENDING",
        "parsed_metadata": parsed,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": user,
        "approved_by": None,
        "approved_at": None,
    }

    return {
        "ingestion_id": ingestion_id,
        "status": "queued",
        "message": f"PDF queued for review. Title: {parsed['title']}",
    }


# ============================================================================
# CAD Ingestion
# ============================================================================

@app.post("/ingest/cad")
async def ingest_cad(request: Request, file: UploadFile = File(...)) -> dict:
    """
    Ingest CAD file (STEP, IGES, etc).
    Extract geometry, material properties, thermal data.
    Buffer for operator review.
    """
    ingestion_id = f"ing_{uuid.uuid4().hex[:8]}"
    user = request.state.user

    logger.info(f"Ingesting CAD: {file.filename} (from {user})")

    content = await file.read()

    try:
        parsed = CADParser.parse(content)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"CAD parse error: {str(e)}")

    ingestion_queue[ingestion_id] = {
        "ingestion_id": ingestion_id,
        "content_type": "cad",
        "source": file.filename,
        "status": "PENDING",
        "parsed_metadata": parsed,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "created_by": user,
        "approved_by": None,
        "approved_at": None,
    }

    return {
        "ingestion_id": ingestion_id,
        "status": "queued",
        "message": f"CAD queued for review. Model: {parsed['model_name']}",
    }


# ============================================================================
# Telemetry Ingestion
# ============================================================================

@app.post("/ingest/fleet/telemetry")
async def ingest_fleet_telemetry(body: List[dict]) -> dict:
    """
    Ingest field device telemetry.
    Direct to Node 6 (no operator review).
    """
    parsed_devices = []

    for telemetry in body:
        try:
            parsed = TelemetryParser.parse(telemetry)
            parsed_devices.append(parsed)
        except ValueError as e:
            logger.warning(f"Telemetry parse error: {str(e)}")
            continue

    logger.info(f"Ingested {len(parsed_devices)} telemetry records")

    # TODO: Forward to Node 6 /api/v1/fleet/telemetry
    # import httpx
    # async with httpx.AsyncClient() as client:
    #     await client.post("http://127.0.0.1:8006/api/v1/fleet/telemetry", json=parsed_devices)

    return {
        "status": "ingested",
        "device_count": len(parsed_devices),
        "message": f"{len(parsed_devices)} telemetry records processed",
    }


# ============================================================================
# Operator Review Queue
# ============================================================================

@app.get("/ingest/queue")
async def list_ingestion_queue(status: Optional[str] = None) -> dict:
    """List all items in ingestion queue."""
    items = list(ingestion_queue.values())

    if status:
        items = [i for i in items if i["status"] == status]

    return {
        "total": len(ingestion_queue),
        "pending": sum(1 for i in ingestion_queue.values() if i["status"] == "PENDING"),
        "approved": sum(1 for i in ingestion_queue.values() if i["status"] == "APPROVED"),
        "items": items,
    }


@app.get("/ingest/queue/{ingestion_id}")
async def get_ingestion_item(ingestion_id: str) -> dict:
    """Get specific ingestion item."""
    if ingestion_id not in ingestion_queue:
        raise HTTPException(status_code=404, detail="Ingestion not found")

    return ingestion_queue[ingestion_id]


@app.post("/ingest/queue/{ingestion_id}/approve")
async def approve_ingestion(request: Request, ingestion_id: str) -> dict:
    """Operator approves ingestion, release to RAG or downstream service."""
    if ingestion_id not in ingestion_queue:
        raise HTTPException(status_code=404, detail="Ingestion not found")

    item = ingestion_queue[ingestion_id]
    user = request.state.user

    logger.info(f"Approving ingestion {ingestion_id} by {user}")

    item["status"] = "APPROVED"
    item["approved_by"] = user
    item["approved_at"] = datetime.now(timezone.utc).isoformat()

    # TODO: Forward to RAG vector store for embedding
    # TODO: Forward to Node 4 if CAD (for thermal/stress simulation)

    return {
        "ingestion_id": ingestion_id,
        "status": "approved",
        "message": "Ingestion approved and sent to processing",
    }


@app.post("/ingest/queue/{ingestion_id}/reject")
async def reject_ingestion(request: Request, ingestion_id: str, reason: Optional[str] = None) -> dict:
    """Operator rejects ingestion."""
    if ingestion_id not in ingestion_queue:
        raise HTTPException(status_code=404, detail="Ingestion not found")

    item = ingestion_queue[ingestion_id]
    user = request.state.user

    logger.info(f"Rejecting ingestion {ingestion_id} by {user}: {reason}")

    item["status"] = "REJECTED"
    item["rejected_by"] = user
    item["rejected_at"] = datetime.now(timezone.utc).isoformat()
    item["rejection_reason"] = reason

    return {
        "ingestion_id": ingestion_id,
        "status": "rejected",
        "message": f"Ingestion rejected: {reason}",
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8007, log_level=LOG_LEVEL.lower())

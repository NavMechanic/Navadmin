"""
Project Athena — Node 5: Quality, Yield & Machine Vision (Port 8005)

Real-time defect detection, SPC, recipe management.
Consumes: kitting.released
Publishes: quality.spc_alert events
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
OPCUA_ENDPOINT = os.getenv("ATHENA_OPCUA_ENDPOINT", "")  # Empty = simulator mode

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# ============================================================================
# Types
# ============================================================================

class DefectType(str, Enum):
    SOLDER_BRIDGE = "solder_bridge"
    OPEN_CIRCUIT = "open_circuit"
    MISSING_COMPONENT = "missing_component"
    COLD_JOINT = "cold_joint"
    TOMBSTONE = "tombstone"
    CORROSION = "corrosion"
    CLEAN = "clean"


class BoardInspectionResult(BaseModel):
    board_id: str
    inspection_date: datetime
    defects_found: List[dict]
    overall_status: str  # PASS, FAIL
    aoi_confidence: float  # 0.0-1.0


class SPCAlert(BaseModel):
    characteristic: str
    value: float
    ucl: float
    lcl: float
    cpk: float
    alert_type: str  # OUT_OF_CONTROL, TREND, SIGMA


class RecipeParameter(BaseModel):
    name: str
    value: float
    unit: str
    tolerance: Optional[str] = None


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title="Node 5 — Quality, Yield & Machine Vision",
    description="Defect detection, SPC, recipe management",
    version="1.0.0",
)

# In-memory stores
boards_inspected: dict = {}
spc_data: dict = {}
recipes: dict = {
    "recipe_default": {
        "recipe_id": "recipe_default",
        "name": "WiFi Beacon v2.0 Standard",
        "parameters": [
            {"name": "reflow_peak_temp", "value": 245, "unit": "°C", "tolerance": "240-250"},
            {"name": "dwell_time", "value": 10, "unit": "s", "tolerance": "8-12"},
        ],
        "hold_status": "active",
    }
}

# OPC-UA mock state (simulator mode)
plc_state = {"acknowledged": False} if not OPCUA_ENDPOINT else {"acknowledged": True}


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
    return {"status": "ok", "service": "node5-quality-vision"}


# ============================================================================
# AOI Inspection
# ============================================================================

@app.post("/api/v1/quality/board")
async def inspect_board(body: dict) -> dict:
    """
    Inspect board via machine vision (AOI).
    Returns defects found, pass/fail, confidence score.
    """
    board_id = body.get("board_id")
    logger.info(f"Inspecting board {board_id}")

    # Mock AOI result
    import random
    has_defects = random.random() < 0.15  # 15% defect rate

    defects = []
    if has_defects:
        defect_types = [
            DefectType.SOLDER_BRIDGE,
            DefectType.MISSING_COMPONENT,
            DefectType.COLD_JOINT,
        ]
        for _ in range(random.randint(1, 3)):
            defects.append({
                "type": random.choice(defect_types).value,
                "location": f"Board position ({random.randint(0, 85)}, {random.randint(0, 65)})",
                "confidence": random.uniform(0.75, 0.99),
            })

    overall_status = "FAIL" if defects else "PASS"
    aoi_confidence = 0.97 if defects else 0.98

    boards_inspected[board_id] = {
        "board_id": board_id,
        "inspection_date": datetime.now(timezone.utc).isoformat(),
        "defects_found": defects,
        "overall_status": overall_status,
        "aoi_confidence": aoi_confidence,
    }

    return {
        "board_id": board_id,
        "overall_status": overall_status,
        "defects_count": len(defects),
        "defects": defects,
        "aoi_confidence": aoi_confidence,
    }


# ============================================================================
# SPC (Statistical Process Control)
# ============================================================================

@app.get("/api/v1/quality/spc")
async def get_spc_data(characteristic: Optional[str] = None) -> dict:
    """Get SPC charts and out-of-control alerts."""
    # Mock SPC data
    spc_records = {
        "reflow_peak_temp": {
            "characteristic": "Reflow Peak Temp",
            "unit": "°C",
            "lcl": 240,
            "ucl": 250,
            "target": 245,
            "recent_values": [244, 245, 246, 247, 248],  # Trending high
            "cpk": 1.45,
            "out_of_control": False,
            "trend": "increasing",
        },
        "dwell_time": {
            "characteristic": "Dwell Time",
            "unit": "s",
            "lcl": 8,
            "ucl": 12,
            "target": 10,
            "recent_values": [9.8, 10.1, 10.2, 10.0, 9.9],
            "cpk": 1.67,
            "out_of_control": False,
            "trend": "stable",
        },
    }

    if characteristic:
        if characteristic in spc_records:
            return {"characteristic": characteristic, "data": spc_records[characteristic]}
        raise HTTPException(status_code=404, detail="Characteristic not found")

    return {"spc_data": spc_records}


@app.post("/api/v1/quality/spc-alert")
async def trigger_spc_alert(body: SPCAlert) -> dict:
    """Trigger SPC alert (published to Kafka -> Node 2)."""
    logger.info(f"SPC Alert: {body.characteristic} cpk={body.cpk}, type={body.alert_type}")

    # TODO: Publish quality.spc_alert to Kafka

    return {"status": "alert_recorded", "alert_type": body.alert_type}


# ============================================================================
# Recipes
# ============================================================================

@app.get("/api/v1/quality/recipe")
async def list_recipes() -> dict:
    """List available process recipes."""
    return {"recipes": list(recipes.values())}


@app.post("/api/v1/quality/recipe")
async def create_recipe(body: dict) -> dict:
    """Create new process recipe."""
    recipe_id = body.get("recipe_id")
    logger.info(f"Creating recipe {recipe_id}")

    recipes[recipe_id] = {
        "recipe_id": recipe_id,
        "name": body.get("name"),
        "parameters": body.get("parameters", []),
        "hold_status": "active",
        "created_at": datetime.now(timezone.utc).isoformat(),
    }

    return {"recipe_id": recipe_id, "status": "created"}


@app.post("/api/v1/quality/recipe/{recipe_id}/hold")
async def hold_recipe(recipe_id: str, reason: Optional[str] = None) -> dict:
    """Place recipe on hold (blocks production until cleared)."""
    if recipe_id not in recipes:
        raise HTTPException(status_code=404, detail="Recipe not found")

    recipes[recipe_id]["hold_status"] = "held"
    logger.info(f"Recipe {recipe_id} placed on hold: {reason}")

    return {"recipe_id": recipe_id, "status": "held", "reason": reason}


# ============================================================================
# PLC Integration (OPC-UA)
# ============================================================================

@app.post("/api/v1/quality/plc-command")
async def send_plc_command(request: Request, body: dict) -> dict:
    """
    Send command to PLC (OPC-UA).
    Audited action: logged with user attribution.
    """
    user = request.state.user
    command = body.get("command")

    logger.info(f"PLC command from {user}: {command}")
    logger.info(f"  Action audited: user={user}, command={command}, timestamp={datetime.now(timezone.utc).isoformat()}")

    # Mock PLC response
    acknowledged = bool(OPCUA_ENDPOINT)  # True if connected to real PLC

    return {
        "command": command,
        "acknowledged": acknowledged,
        "status": "executed" if acknowledged else "simulated",
        "message": "Command sent to PLC" if acknowledged else "Simulator mode (no real PLC connected)",
    }


@app.post("/api/v1/quality/telemetry")
async def ingest_telemetry(body: dict) -> dict:
    """Ingest telemetry from running line."""
    station = body.get("station")
    metrics = body.get("metrics", {})

    logger.info(f"Telemetry from {station}: {metrics}")

    return {"status": "recorded", "station": station}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8005, log_level=LOG_LEVEL.lower())

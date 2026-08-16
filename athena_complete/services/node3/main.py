"""
Project Athena — Node 3: Supply Chain & Landed Cost (Port 8003)

Handles part availability, alternate mapping, supplier management.
Consumes: demand.updated, gate.status_changed
Publishes: kitting.released events
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

LOG_LEVEL = os.getenv("ATHENA_LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# ============================================================================
# Types
# ============================================================================

class PartAvailability(BaseModel):
    part_number: str
    manufacturer: str
    quantity_available: int
    lead_time_days: int
    unit_cost: float
    stock_status: str  # IN_STOCK, LEAD_TIME, OBSOLETE


class Alternate(BaseModel):
    original_part: str
    alternate_part: str
    compatibility_score: float  # 0.0-1.0
    cost_delta: float


class SupplierInfo(BaseModel):
    supplier_id: str
    name: str
    rating: float
    parts_supplied: int


class WorkOrder(BaseModel):
    work_order_id: str
    design_id: str
    status: str  # PENDING, RELEASED, KITTED, READY_FOR_MFG
    components: List[dict]
    released_at: Optional[str] = None


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title="Node 3 — Supply Chain & Landed Cost",
    description="Part availability and supply chain optimization",
    version="1.0.0",
)

# In-memory stores
parts_db: dict = {
    "nRF52840": {
        "part_number": "nRF52840",
        "manufacturer": "Nordic Semiconductor",
        "quantity_available": 500,
        "lead_time_days": 2,
        "unit_cost": 12.50,
        "stock_status": "IN_STOCK",
    },
    "ICR18650": {
        "part_number": "ICR18650",
        "manufacturer": "Sanyo",
        "quantity_available": 2000,
        "lead_time_days": 5,
        "unit_cost": 5.00,
        "stock_status": "IN_STOCK",
    },
}

alternates_db: dict = {
    "nRF52840": [
        {"original_part": "nRF52840", "alternate_part": "nRF52833", "compatibility_score": 0.92, "cost_delta": -1.50}
    ]
}

work_orders: dict = {}


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
    return {"status": "ok", "service": "node3-supply-chain"}


# ============================================================================
# Part Availability
# ============================================================================

@app.get("/api/v1/parts")
async def query_parts(pn: Optional[str] = None, manufacturer: Optional[str] = None) -> dict:
    """Query part availability by part number or manufacturer."""
    results = []

    for part_num, part_info in parts_db.items():
        if pn and part_num.lower() != pn.lower():
            continue
        if manufacturer and part_info["manufacturer"].lower() != manufacturer.lower():
            continue
        results.append(part_info)

    return {"parts": results, "count": len(results)}


@app.get("/api/v1/parts/{part_number}/alternates")
async def get_alternates(part_number: str) -> dict:
    """Get alternate parts for a part number."""
    alternates = alternates_db.get(part_number, [])
    return {"part_number": part_number, "alternates": alternates}


# ============================================================================
# Landed Cost
# ============================================================================

@app.get("/api/v1/landed-cost")
async def calculate_landed_cost(design_id: str) -> dict:
    """Calculate total landed cost for design."""
    # Mock calculation
    components_cost = 22.50
    shipping = 2.00
    duties = 1.80
    total = components_cost + shipping + duties

    return {
        "design_id": design_id,
        "components_cost": components_cost,
        "shipping": shipping,
        "duties": duties,
        "total_landed_cost": total,
        "currency": "USD",
    }


# ============================================================================
# Supplier Network
# ============================================================================

@app.get("/api/v1/supplier-network")
async def get_supplier_network() -> dict:
    """Get supplier network and ratings."""
    suppliers = [
        {"supplier_id": "SUP-001", "name": "Nordic Semiconductor", "rating": 4.8, "parts_supplied": 156},
        {"supplier_id": "SUP-002", "name": "Sanyo", "rating": 4.6, "parts_supplied": 89},
    ]
    return {"suppliers": suppliers}


# ============================================================================
# Work Orders
# ============================================================================

@app.post("/api/v1/work-orders")
async def create_work_order(body: WorkOrder) -> dict:
    """Create work order for design."""
    logger.info(f"Creating work order {body.work_order_id} for design {body.design_id}")

    work_orders[body.work_order_id] = {
        "work_order_id": body.work_order_id,
        "design_id": body.design_id,
        "status": body.status,
        "components": body.components,
        "released_at": datetime.now(timezone.utc).isoformat(),
    }

    # TODO: Publish kitting.released event to Kafka

    return {"work_order_id": body.work_order_id, "status": "created"}


@app.get("/api/v1/work-orders/{work_order_id}")
async def get_work_order(work_order_id: str) -> dict:
    """Get work order details."""
    if work_order_id not in work_orders:
        raise HTTPException(status_code=404, detail="Work order not found")

    return work_orders[work_order_id]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003, log_level=LOG_LEVEL.lower())

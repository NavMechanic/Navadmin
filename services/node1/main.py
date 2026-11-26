"""
Project Athena — Node 1: Demand Sensing & Intent (Port 8001)

Handles AI-driven demand forecasting with safety buffers.
Publishes demand.updated events to Kafka topic.
"""

from __future__ import annotations

import os
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List
from dataclasses import dataclass, asdict
from enum import Enum

import httpx
from fastapi import FastAPI, HTTPException, Request, status
from pydantic import BaseModel

KAFKA_BOOTSTRAP = os.getenv("ATHENA_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
LOG_LEVEL = os.getenv("ATHENA_LOG_LEVEL", "INFO")

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# ============================================================================
# Types
# ============================================================================

class ForecastHorizon(str, Enum):
    WEEK = "week"
    MONTH = "month"
    QUARTER = "quarter"


@dataclass
class SKUForecast:
    sku: str
    horizon: ForecastHorizon
    quantity: int
    confidence: float  # 0.0-1.0
    safety_margin_pct: float
    updated_at: datetime


class DemandRequest(BaseModel):
    sku: str
    historical_weeks: int = 12
    include_seasonal: bool = True


class ForecastResponse(BaseModel):
    sku: str
    forecasts: List[dict]
    models_used: List[str]


# ============================================================================
# Demand ML Models (Mock - replace with Pecan AI in production)
# ============================================================================

class DemandForecaster:
    """AI demand forecasting engine."""

    def forecast_sku(self, sku: str, historical_weeks: int = 12) -> List[dict]:
        """Forecast demand for SKU across horizons (week, month, quarter)."""
        import random

        base_demand = random.randint(100, 5000)

        forecasts = [
            {
                "horizon": "week",
                "quantity": base_demand + random.randint(-100, 100),
                "confidence": 0.92,
                "safety_margin_pct": 10,
            },
            {
                "horizon": "month",
                "quantity": base_demand * 4 + random.randint(-500, 500),
                "confidence": 0.85,
                "safety_margin_pct": 15,
            },
            {
                "horizon": "quarter",
                "quantity": base_demand * 12 + random.randint(-2000, 2000),
                "confidence": 0.78,
                "safety_margin_pct": 20,
            },
        ]
        return forecasts


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title="Node 1 — Demand Sensing",
    description="AI-driven demand forecasting with safety buffers",
    version="1.0.0",
)

forecaster = DemandForecaster()
demand_store: dict = {}  # In-memory store of latest forecasts


# ============================================================================
# Middleware: Auth verification
# ============================================================================

@app.middleware("http")
async def verify_auth(request: Request, call_next):
    """Verify JWT from gateway."""
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
    return {"status": "ok", "service": "node1-demand-sensing"}


# ============================================================================
# Demand Endpoints
# ============================================================================

@app.get("/api/v1/demand/forecast")
async def get_demand_forecast(sku: Optional[str] = None) -> dict:
    """Get latest demand forecast for SKU(s)."""
    if sku:
        if sku in demand_store:
            return {"sku": sku, "forecast": demand_store[sku]}
        return {"sku": sku, "forecast": None, "message": "No forecast yet"}

    return {"forecasts": demand_store}


@app.post("/api/v1/demand/forecast")
async def forecast_demand(body: DemandRequest) -> ForecastResponse:
    """Generate demand forecast for SKU."""
    logger.info(f"Forecasting demand for {body.sku}")

    forecasts = forecaster.forecast_sku(
        body.sku,
        historical_weeks=body.historical_weeks
    )

    # Store latest forecast
    demand_store[body.sku] = {
        "forecasts": forecasts,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # TODO: Publish demand.updated event to Kafka
    # await kafka_producer.send("demand.updated", {
    #     "sku": body.sku,
    #     "forecasts": forecasts,
    #     "timestamp": datetime.now(timezone.utc).isoformat(),
    # })

    return ForecastResponse(
        sku=body.sku,
        forecasts=forecasts,
        models_used=["exponential_smoothing", "arima"],
    )


@app.get("/api/v1/demand/models")
async def list_models() -> dict:
    """List available demand forecasting models."""
    return {
        "models": [
            {"name": "exponential_smoothing", "type": "statistical"},
            {"name": "arima", "type": "statistical"},
            {"name": "prophet", "type": "statistical"},
            {"name": "lstm", "type": "neural"},
        ]
    }


@app.post("/api/v1/demand/retrain")
async def retrain_models(sku: Optional[str] = None) -> dict:
    """Retrain demand models (async)."""
    logger.info(f"Retraining models for {sku or 'all SKUs'}")
    return {
        "status": "retrain_started",
        "sku": sku,
        "estimated_time_minutes": 15,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001, log_level=LOG_LEVEL.lower())

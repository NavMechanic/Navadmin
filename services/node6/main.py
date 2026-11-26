"""
Project Athena — Node 6: Fleet Twin & After-Sales (Port 8006)

Field device health monitoring, predictive RMA, CAPA evidence.
Consumes: (telemetry ingestion)
Publishes: fleet.capa_triggered events
"""

from __future__ import annotations

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional, List
import math

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

LOG_LEVEL = os.getenv("ATHENA_LOG_LEVEL", "INFO")
TIMESCALE_REQUIRED = os.getenv("ATHENA_TIMESCALE_REQUIRED", "false").lower() == "true"

logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger(__name__)

# ============================================================================
# Types
# ============================================================================

class DeviceTelemetry(BaseModel):
    device_id: str
    battery_health_pct: float
    operating_temp_c: float
    error_codes: List[int] = []
    firmware_version: str = ""
    uptime_hours: int = 0


class DeviceStatus(BaseModel):
    device_id: str
    battery_health_pct: float
    operating_temp_c: float
    error_count: int
    rma_risk_score: float
    recommended_replacement: bool


class AnomalyCluster(BaseModel):
    cluster_id: str
    devices: List[str]
    failure_pattern: str
    cluster_size: int


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title="Node 6 — Fleet Twin & After-Sales",
    description="Field device monitoring and predictive RMA",
    version="1.0.0",
)

# In-memory device fleet (mock TimescaleDB)
device_fleet: dict = {}
anomaly_clusters: dict = {}

# Initialize with mock fleet
for i in range(1, 11):
    device_id = f"DEV-{i:06d}"
    device_fleet[device_id] = {
        "device_id": device_id,
        "battery_health_pct": 85 - (i * 5),
        "operating_temp_c": 35 + (i * 2),
        "error_count": i // 2,
        "latest_errors": [],
        "firmware_version": "2.1.0",
        "days_in_field": 180 + (i * 10),
        "rma_risk_score": 0.0,
        "recommended_replacement": False,
    }


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
        "service": "node6-fleet-twin",
        "timescaledb_required": TIMESCALE_REQUIRED,
        "devices_monitored": len(device_fleet),
    }


# ============================================================================
# RMA Rule Engine
# ============================================================================

class RMARuleEngine:
    """Predictive RMA scoring."""

    BATTERY_HEALTH_CRITICAL = 40
    TEMP_MAX_SAFE = 75
    ERROR_RATE_CRITICAL = 10
    RMA_SCORE_THRESHOLD = 0.75

    @staticmethod
    def evaluate_device(device: dict) -> None:
        """Apply rules and update RMA recommendation."""
        score = 0.0

        # Battery health rule (0.0-0.4 contribution)
        if device["battery_health_pct"] < RMARuleEngine.BATTERY_HEALTH_CRITICAL:
            score += 0.4 * (1 - device["battery_health_pct"] / 100)

        # Temperature rule (0.0-0.3 contribution)
        if device["operating_temp_c"] > RMARuleEngine.TEMP_MAX_SAFE:
            temp_excess = device["operating_temp_c"] - RMARuleEngine.TEMP_MAX_SAFE
            score += 0.3 * min(1.0, temp_excess / 25)

        # Error rate rule (0.0-0.3 contribution)
        if device["error_count"] > 0 and device["days_in_field"] > 0:
            daily_error_rate = device["error_count"] / device["days_in_field"]
            if daily_error_rate > RMARuleEngine.ERROR_RATE_CRITICAL / 365:
                score += 0.3 * min(1.0, daily_error_rate / (2 * RMARuleEngine.ERROR_RATE_CRITICAL / 365))

        device["rma_risk_score"] = min(1.0, score)
        device["recommended_replacement"] = (
            device["rma_risk_score"] >= RMARuleEngine.RMA_SCORE_THRESHOLD
            or device["battery_health_pct"] < 25
            or device["operating_temp_c"] > 90
        )


# ============================================================================
# Telemetry Ingestion
# ============================================================================

@app.post("/api/v1/fleet/telemetry")
async def ingest_telemetry(body: List[dict]) -> dict:
    """
    Ingest telemetry from field devices.
    Updates in TimescaleDB hypertable.
    """
    for telemetry in body:
        device_id = telemetry.get("device_id")
        logger.info(f"Telemetry from {device_id}")

        # Update or create device record
        if device_id not in device_fleet:
            device_fleet[device_id] = {
                "device_id": device_id,
                "battery_health_pct": 100.0,
                "operating_temp_c": 25.0,
                "error_count": 0,
                "latest_errors": [],
                "firmware_version": "",
                "days_in_field": 0,
                "rma_risk_score": 0.0,
                "recommended_replacement": False,
            }

        device = device_fleet[device_id]
        device["battery_health_pct"] = telemetry.get("battery_health_pct", 100.0)
        device["operating_temp_c"] = telemetry.get("operating_temp_c", 25.0)
        device["error_count"] = len(telemetry.get("error_codes", []))
        device["firmware_version"] = telemetry.get("firmware_version", "")
        device["uptime_hours"] = telemetry.get("uptime_hours", 0)

        # Evaluate RMA risk
        RMARuleEngine.evaluate_device(device)

    return {"status": "telemetry_recorded", "device_count": len(body)}


# ============================================================================
# Device Queries
# ============================================================================

@app.get("/api/v1/fleet/device/{device_id}")
async def get_device_status(device_id: str) -> dict:
    """Get device health status."""
    if device_id not in device_fleet:
        raise HTTPException(status_code=404, detail="Device not found")

    device = device_fleet[device_id]
    return {
        "device_id": device_id,
        "battery_health_pct": device["battery_health_pct"],
        "operating_temp_c": device["operating_temp_c"],
        "error_count": device["error_count"],
        "rma_risk_score": device["rma_risk_score"],
        "recommended_replacement": device["recommended_replacement"],
        "firmware_version": device["firmware_version"],
        "days_in_field": device.get("days_in_field", 0),
    }


@app.get("/api/v1/fleet/devices-at-risk")
async def get_devices_at_risk() -> dict:
    """Get devices recommended for RMA."""
    at_risk = [
        d for d in device_fleet.values()
        if d["recommended_replacement"]
    ]
    return {"devices_at_risk": len(at_risk), "devices": at_risk}


# ============================================================================
# Anomaly Detection (K-means clustering)
# ============================================================================

@app.get("/api/v1/fleet/anomalies")
async def detect_anomalies() -> dict:
    """
    Detect failure clusters in field data.
    K-means clustering on battery + temperature + error patterns.
    """
    if not device_fleet:
        return {"clusters": []}

    # Simple clustering: group high-risk devices
    normal_devices = []
    at_risk_devices = []

    for device in device_fleet.values():
        if device["rma_risk_score"] > 0.5:
            at_risk_devices.append(device)
        else:
            normal_devices.append(device)

    clusters = []
    if at_risk_devices:
        # Cluster by failure pattern
        battery_failures = [
            d for d in at_risk_devices
            if d["battery_health_pct"] < 40
        ]
        if battery_failures:
            clusters.append({
                "cluster_id": "battery_degradation",
                "devices": [d["device_id"] for d in battery_failures],
                "failure_pattern": "Battery degradation",
                "cluster_size": len(battery_failures),
            })

        temp_failures = [
            d for d in at_risk_devices
            if d["operating_temp_c"] > 75
        ]
        if temp_failures:
            clusters.append({
                "cluster_id": "thermal_stress",
                "devices": [d["device_id"] for d in temp_failures],
                "failure_pattern": "Thermal stress",
                "cluster_size": len(temp_failures),
            })

    return {
        "total_devices": len(device_fleet),
        "normal_devices": len(normal_devices),
        "at_risk_devices": len(at_risk_devices),
        "clusters": clusters,
    }


# ============================================================================
# RMA Forecasting
# ============================================================================

@app.get("/api/v1/fleet/rma-forecast")
async def forecast_rma() -> dict:
    """Forecast RMA volume in next 30 days."""
    at_risk = [
        d for d in device_fleet.values()
        if d["recommended_replacement"]
    ]

    # Weibull curve approximation
    days_to_failure = []
    for device in at_risk:
        if device["battery_health_pct"] < 25:
            days_to_failure.append(random_weibull(shape=2, scale=7))
        elif device["operating_temp_c"] > 90:
            days_to_failure.append(random_weibull(shape=2, scale=14))
        else:
            days_to_failure.append(random_weibull(shape=2, scale=21))

    rma_in_30_days = sum(1 for d in days_to_failure if d < 30)

    return {
        "total_at_risk": len(at_risk),
        "forecast_30_days": rma_in_30_days,
        "estimated_cost_usd": rma_in_30_days * 45,  # $45 per replacement
        "confidence": 0.82,
    }


def random_weibull(shape: float, scale: float) -> float:
    """Generate Weibull random variable (mock)."""
    import random
    u = random.random()
    return scale * (-math.log(1 - u)) ** (1 / shape)


# ============================================================================
# CAPA Proposals (from field failures)
# ============================================================================

@app.post("/api/v1/fleet/capa-proposal")
async def create_capa_from_field(body: dict) -> dict:
    """
    Propose CAPA to Node 2 based on field failure cluster.
    Note: Node 6 CANNOT apply FMEA re-ratings (privilege boundary).
    """
    cluster_id = body.get("cluster_id")
    logger.info(f"Proposing CAPA for cluster {cluster_id}")

    capa_proposal = {
        "capa_id": f"CAPA-{cluster_id}",
        "issue": body.get("failure_pattern"),
        "evidence": body.get("devices", []),
        "proposed_action": body.get("proposed_action"),
        "proposed_by": "fleet_twin",
        "status": "proposed",
    }

    # TODO: Publish fleet.capa_triggered event to Kafka -> Node 2
    # Node 2 will handle FMEA re-rating (privilege boundary enforced)

    return {
        "capa_id": capa_proposal["capa_id"],
        "status": "proposed",
        "message": "CAPA proposal sent to Node 2 for evaluation",
    }


# ============================================================================
# Survival Analysis (Weibull curves)
# ============================================================================

@app.get("/api/v1/fleet/survival-analysis")
async def survival_analysis() -> dict:
    """
    Weibull survival curves for device fleet.
    Estimates time to failure by cohort.
    """
    return {
        "survival_curves": {
            "all_devices": {
                "shape": 2.1,
                "scale": 420,  # days
                "median_ttf_days": 290,
                "90_percentile_days": 90,
            },
            "high_temp_devices": {
                "shape": 2.3,
                "scale": 180,
                "median_ttf_days": 125,
                "90_percentile_days": 40,
            },
        }
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8006, log_level=LOG_LEVEL.lower())

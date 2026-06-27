"""Pydantic data models shared across all Athena orchestration layers."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    PARTIAL = "partial"


class PartStatus(str, Enum):
    AVAILABLE = "available"
    SHORT = "short"
    ALTERNATE_MAPPED = "alternate_mapped"
    CRITICAL = "critical"


class DefectSeverity(str, Enum):
    NONE = "none"
    MINOR = "minor"
    MAJOR = "major"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Layer 1 — Demand Forecasting (Pecan AI)
# ---------------------------------------------------------------------------

class DealSignal(BaseModel):
    deal_id: str
    account_name: str
    probability: float = Field(..., ge=0.0, le=1.0)
    estimated_units: int
    close_date: str
    segment: str = "enterprise"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class DemandForecast(BaseModel):
    signal: DealSignal
    forecast_units: int
    confidence_interval_low: int
    confidence_interval_high: int
    recommended_build_qty: int
    triggered_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Layer 2 — Design & Sourcing Lock (Duro PLM + Nexar)
# ---------------------------------------------------------------------------

class BOMLine(BaseModel):
    line_id: str
    mpn: str
    manufacturer: str
    description: str
    quantity: int
    reference_designators: List[str] = Field(default_factory=list)
    unit_price_usd: Optional[float] = None


class BOM(BaseModel):
    bom_id: str
    revision: str
    product_name: str
    lines: List[BOMLine]
    pulled_at: datetime = Field(default_factory=datetime.utcnow)


class PartAvailability(BaseModel):
    mpn: str
    status: PartStatus
    stock_qty: int
    lead_time_weeks: Optional[int] = None
    alternate_mpn: Optional[str] = None
    alternate_source: Optional[str] = None
    unit_price_usd: Optional[float] = None


class SourcingResult(BaseModel):
    bom: BOM
    availability: List[PartAvailability]
    shortage_count: int
    alternates_mapped: int
    sourcing_risk: str  # "low" | "medium" | "high"


# ---------------------------------------------------------------------------
# Layer 3 — Simulation & Optimization (Lokad + Ansys)
# ---------------------------------------------------------------------------

class SafetyStockResult(BaseModel):
    mpn: str
    safety_stock_qty: int
    reorder_point: int
    cycle_service_level: float
    demand_std_dev: float


class PhysicalValidation(BaseModel):
    passed: bool
    max_stress_mpa: float
    thermal_delta_c: float
    vibration_g_rms: float
    failure_modes: List[str] = Field(default_factory=list)
    notes: str = ""


class SimulationResult(BaseModel):
    safety_stocks: List[SafetyStockResult]
    physical_validation: PhysicalValidation
    simulation_timestamp: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Layer 4 — Execution (MacroFab + Fictiv)
# ---------------------------------------------------------------------------

class ProductionOrder(BaseModel):
    order_id: str
    supplier: str  # "macrofab" | "fictiv"
    order_type: str  # "pcba" | "mechanical"
    quantity: int
    status: RunStatus = RunStatus.PENDING
    estimated_ship_date: Optional[str] = None
    tracking_url: Optional[str] = None
    submitted_at: datetime = Field(default_factory=datetime.utcnow)


class ExecutionResult(BaseModel):
    pcba_order: ProductionOrder
    mechanical_order: ProductionOrder
    total_cost_usd: float
    lead_time_days: int


# ---------------------------------------------------------------------------
# Layer 5 — Quality & Revenue (Instrumental + TwinMaker + RaaS/YaSS)
# ---------------------------------------------------------------------------

class VisionInspectionResult(BaseModel):
    unit_serial: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)
    defect_severity: DefectSeverity
    defect_types: List[str] = Field(default_factory=list)
    confidence: float = Field(..., ge=0.0, le=1.0)
    image_key: Optional[str] = None


class TwinMakerUpdate(BaseModel):
    asset_id: str
    workspace_id: str
    properties_updated: Dict[str, Any]
    pushed_at: datetime = Field(default_factory=datetime.utcnow)


class RevenueShare(BaseModel):
    unit_serial: str
    raas_monthly_usd: float
    yass_annual_usd: float
    quality_score: float = Field(..., ge=0.0, le=1.0)
    monetization_tier: str  # "standard" | "premium" | "enterprise"


class QualityRevenueResult(BaseModel):
    inspections: List[VisionInspectionResult]
    twin_updates: List[TwinMakerUpdate]
    revenue_shares: List[RevenueShare]
    yield_rate: float
    total_raas_monthly_usd: float
    total_yass_annual_usd: float


# ---------------------------------------------------------------------------
# Top-level Athena Run
# ---------------------------------------------------------------------------

class AthenaRun(BaseModel):
    run_id: str
    status: RunStatus = RunStatus.PENDING
    started_at: datetime = Field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None

    demand: Optional[DemandForecast] = None
    sourcing: Optional[SourcingResult] = None
    simulation: Optional[SimulationResult] = None
    execution: Optional[ExecutionResult] = None
    quality: Optional[QualityRevenueResult] = None

    errors: List[str] = Field(default_factory=list)

    def layer_statuses(self) -> Dict[str, str]:
        return {
            "demand": "done" if self.demand else "pending",
            "sourcing": "done" if self.sourcing else "pending",
            "simulation": "done" if self.simulation else "pending",
            "execution": "done" if self.execution else "pending",
            "quality": "done" if self.quality else "pending",
        }

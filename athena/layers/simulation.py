"""Layer 3 — Simulation & Optimization via Lokad + Ansys Twin Builder."""

from __future__ import annotations

import math
import random
from typing import List

import httpx

from athena.config import cfg
from athena.models import (
    BOM,
    PhysicalValidation,
    SafetyStockResult,
    SimulationResult,
)


class SimulationLayer:
    """
    Computes probabilistic safety stocks via Lokad and validates physical
    design integrity via Ansys Twin Builder.
    """

    def __init__(self) -> None:
        self._lokad = httpx.Client(
            base_url=cfg.LOKAD_BASE_URL,
            headers={"Authorization": f"Token {cfg.LOKAD_TOKEN}"},
            timeout=cfg.HTTP_TIMEOUT_S,
        )
        self._ansys = httpx.Client(
            base_url=cfg.ANSYS_TWIN_URL,
            headers={"X-API-Key": cfg.ANSYS_API_KEY},
            timeout=cfg.HTTP_TIMEOUT_S,
        ) if cfg.ANSYS_TWIN_URL else None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self, bom: BOM, build_qty: int) -> SimulationResult:
        safety_stocks = self._compute_safety_stocks(bom, build_qty)
        physical = self._validate_physical(build_qty)
        return SimulationResult(safety_stocks=safety_stocks, physical_validation=physical)

    # ------------------------------------------------------------------
    # Lokad — probabilistic safety stock
    # ------------------------------------------------------------------

    def _compute_safety_stocks(self, bom: BOM, build_qty: int) -> List[SafetyStockResult]:
        if cfg.ATHENA_DRY_RUN or not cfg.LOKAD_TOKEN:
            return self._mock_safety_stocks(bom, build_qty)

        results = []
        for line in bom.lines:
            resp = self._lokad.post(
                f"/accounts/{cfg.LOKAD_ACCOUNT_ID}/runs",
                json={
                    "script": "safetystock",
                    "params": {
                        "mpn": line.mpn,
                        "demand_mean": line.quantity * build_qty,
                        "service_level": 0.95,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
            results.append(
                SafetyStockResult(
                    mpn=line.mpn,
                    safety_stock_qty=data["safety_stock"],
                    reorder_point=data["reorder_point"],
                    cycle_service_level=data["service_level"],
                    demand_std_dev=data["demand_std_dev"],
                )
            )
        return results

    # ------------------------------------------------------------------
    # Ansys Twin Builder — physical validation
    # ------------------------------------------------------------------

    def _validate_physical(self, build_qty: int) -> PhysicalValidation:
        if cfg.ATHENA_DRY_RUN or not self._ansys:
            return self._mock_physical()

        resp = self._ansys.post(
            "/simulations",
            json={
                "model": cfg.TWINMAKER_ASSET_MODEL_ID,
                "parameters": {
                    "ambient_temp_c": 25.0,
                    "max_current_a": 2.5,
                    "vibration_profile": "MIL-STD-810H",
                    "build_qty": build_qty,
                },
            },
        )
        resp.raise_for_status()
        data = resp.json()
        failure_modes = data.get("failure_modes", [])
        return PhysicalValidation(
            passed=data["passed"],
            max_stress_mpa=data["max_stress_mpa"],
            thermal_delta_c=data["thermal_delta_c"],
            vibration_g_rms=data["vibration_g_rms"],
            failure_modes=failure_modes,
            notes=data.get("notes", ""),
        )

    # ------------------------------------------------------------------
    # Mock helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mock_safety_stocks(bom: BOM, build_qty: int) -> List[SafetyStockResult]:
        results = []
        for line in bom.lines:
            demand_mean = line.quantity * build_qty
            std_dev = demand_mean * 0.12
            z = 1.645  # 95 % service level
            ss = math.ceil(z * std_dev)
            rop = math.ceil(demand_mean * 0.25 + ss)
            results.append(
                SafetyStockResult(
                    mpn=line.mpn,
                    safety_stock_qty=ss,
                    reorder_point=rop,
                    cycle_service_level=0.95,
                    demand_std_dev=round(std_dev, 2),
                )
            )
        return results

    @staticmethod
    def _mock_physical() -> PhysicalValidation:
        stress = round(random.uniform(45.0, 72.0), 2)
        thermal = round(random.uniform(12.0, 28.0), 2)
        vibration = round(random.uniform(0.8, 2.1), 3)
        passed = stress < 80.0 and thermal < 35.0 and vibration < 3.0
        failure_modes = []
        if not passed:
            if stress >= 80.0:
                failure_modes.append("solder_joint_fatigue")
            if thermal >= 35.0:
                failure_modes.append("thermal_runaway_risk")
        return PhysicalValidation(
            passed=passed,
            max_stress_mpa=stress,
            thermal_delta_c=thermal,
            vibration_g_rms=vibration,
            failure_modes=failure_modes,
            notes="Ansys Twin Builder dry-run simulation",
        )

"""
Project Athena — Core Orchestrator.

Runs the 5-layer production loop sequentially, collecting results into
an AthenaRun record. Each layer is isolated; a failure in one layer
records an error and continues to subsequent layers where possible.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from athena.layers.demand import PecanDemandLayer
from athena.layers.execution import ExecutionLayer
from athena.layers.quality import QualityRevenueLayer
from athena.layers.simulation import SimulationLayer
from athena.layers.sourcing import DuroSourcingLayer
from athena.models import AthenaRun, RunStatus


class AthenaOrchestrator:
    """Coordinates all five Athena layers into a single atomic production run."""

    def __init__(self) -> None:
        self._demand = PecanDemandLayer()
        self._sourcing = DuroSourcingLayer()
        self._simulation = SimulationLayer()
        self._execution = ExecutionLayer()
        self._quality = QualityRevenueLayer()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, deal_id: Optional[str] = None) -> AthenaRun:
        """
        Execute the full Athena loop for a given Pecan AI deal.
        Returns an AthenaRun with populated layer results and final status.
        """
        run = AthenaRun(
            run_id=f"ATH-{uuid.uuid4().hex[:10].upper()}",
            status=RunStatus.RUNNING,
        )

        # ---- Layer 1: Demand Forecasting --------------------------------
        try:
            run.demand = self._demand.get_forecast(deal_id)
            build_qty = run.demand.recommended_build_qty
        except Exception as exc:
            run.errors.append(f"[demand] {exc}")
            run.status = RunStatus.FAILED
            run.completed_at = datetime.utcnow()
            return run

        # ---- Layer 2: Design & Sourcing Lock ----------------------------
        try:
            run.sourcing = self._sourcing.lock_sourcing(
                build_qty=build_qty,
                bom_id=None,
            )
        except Exception as exc:
            run.errors.append(f"[sourcing] {exc}")
            # Sourcing failure is blocking — can't manufacture without parts.
            run.status = RunStatus.FAILED
            run.completed_at = datetime.utcnow()
            return run

        # ---- Layer 3: Simulation & Optimization -------------------------
        try:
            run.simulation = self._simulation.run(
                bom=run.sourcing.bom,
                build_qty=build_qty,
            )
            if not run.simulation.physical_validation.passed:
                run.errors.append(
                    "[simulation] Physical validation failed: "
                    + ", ".join(run.simulation.physical_validation.failure_modes)
                )
        except Exception as exc:
            run.errors.append(f"[simulation] {exc}")
            # Non-blocking — continue to execution.

        # ---- Layer 4: Execution -----------------------------------------
        try:
            run.execution = self._execution.dispatch(
                build_qty=build_qty,
                bom_id=run.sourcing.bom.bom_id,
            )
        except Exception as exc:
            run.errors.append(f"[execution] {exc}")
            run.status = RunStatus.PARTIAL
            run.completed_at = datetime.utcnow()
            return run

        # ---- Layer 5: Quality & Revenue Monetization --------------------
        try:
            run.quality = self._quality.run(
                build_qty=build_qty,
                serial_prefix=run.run_id[:6],
            )
        except Exception as exc:
            run.errors.append(f"[quality] {exc}")
            run.status = RunStatus.PARTIAL
            run.completed_at = datetime.utcnow()
            return run

        # ---- Finalise ---------------------------------------------------
        run.status = RunStatus.PARTIAL if run.errors else RunStatus.SUCCESS
        run.completed_at = datetime.utcnow()
        return run

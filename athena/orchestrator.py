"""
Project Athena — Core Orchestrator.

Runs the complete 8-layer hardware production loop sequentially:
  1. Demand Forecasting (Pecan AI)
  2. Generative Design (Duro PLM + Fusion 360)
  3. Sourcing Lock (Duro PLM + Nexar)
  4. Simulation & Optimization (Lokad + Ansys Twin)
  5. Automated Testing Framework (Pluma + Instrumental.ai)
  6. Execution (MacroFab + Fictiv)
  7. Quality & Revenue (Instrumental AOI + TwinMaker)
  8. Financial Engine (RaaS/YaSS monetization)

Each layer is isolated; failures record errors but continue to subsequent layers.
Optionally integrates with RAG for RCA context and knowledge capture.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from athena.layers.demand import PecanDemandLayer
from athena.layers.generative_design import GenerativeDesignLayer
from athena.layers.sourcing import DuroSourcingLayer
from athena.layers.simulation import SimulationLayer
from athena.layers.automated_testing import AutomatedTestingLayer
from athena.layers.execution import ExecutionLayer
from athena.layers.quality import QualityRevenueLayer
from athena.layers.financial_engine import FinancialEngine
from athena.models import AthenaRun, RunStatus

if TYPE_CHECKING:
    from rag.pipeline import RAGPipeline


class AthenaOrchestrator:
    """Orchestrates all 8 layers of the Hardware as AI Service platform."""

    def __init__(self, rag_pipeline: Optional[RAGPipeline] = None) -> None:
        self._demand = PecanDemandLayer()
        self._design = GenerativeDesignLayer()
        self._sourcing = DuroSourcingLayer()
        self._simulation = SimulationLayer()
        self._testing = AutomatedTestingLayer()
        self._execution = ExecutionLayer()
        self._quality = QualityRevenueLayer(rag_pipeline=rag_pipeline)
        self._financial = FinancialEngine()
        self._rag = rag_pipeline

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(self, deal_id: Optional[str] = None) -> AthenaRun:
        """
        Execute the full Athena 8-layer loop for a given deal.
        Returns an AthenaRun with all layer results.
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
            run.errors.append(f"[1-demand] {exc}")
            run.status = RunStatus.FAILED
            run.completed_at = datetime.utcnow()
            return run

        # ---- Layer 2: Generative Design ---------------------------------
        try:
            design_artifact = self._design.generate_design(
                product_name="Aviator-Next",
                target_price=run.demand.signal.estimated_units * 50,  # rough CAC
                environmental_spec="IP68",
                volume_forecast=build_qty,
            )
            if not design_artifact.is_production_ready():
                run.errors.append("[2-design] Design failed DRC/DFM/thermal checks")
        except Exception as exc:
            run.errors.append(f"[2-design] {exc}")
            # Non-blocking — continue with default BOM

        # ---- Layer 3: Sourcing Lock ------------------------------------
        try:
            run.sourcing = self._sourcing.lock_sourcing(
                build_qty=build_qty,
                bom_id=None,
            )
        except Exception as exc:
            run.errors.append(f"[3-sourcing] {exc}")
            run.status = RunStatus.FAILED
            run.completed_at = datetime.utcnow()
            return run

        # ---- Layer 4: Simulation & Optimization -------------------------
        try:
            run.simulation = self._simulation.run(
                bom=run.sourcing.bom,
                build_qty=build_qty,
            )
            if not run.simulation.physical_validation.passed:
                run.errors.append(
                    "[4-simulation] Physical validation failed: "
                    + ", ".join(run.simulation.physical_validation.failure_modes)
                )
        except Exception as exc:
            run.errors.append(f"[4-simulation] {exc}")

        # ---- Layer 5: Automated Testing Framework -----------------------
        try:
            test_suite = self._testing.generate_test_suite(
                product_name="Aviator-Next",
                requirements={
                    "gpio_count": 16,
                    "adc_channels": 8,
                    "spi_speed_mhz": 50,
                },
                bom=run.sourcing.bom,
            )
            aoi_defects = self._testing.stream_smt_inspection(
                production_run_id=run.run_id,
                batch_size=build_qty,
            )
        except Exception as exc:
            run.errors.append(f"[5-testing] {exc}")

        # ---- Layer 6: Execution -----------------------------------------
        try:
            run.execution = self._execution.dispatch(
                build_qty=build_qty,
                bom_id=run.sourcing.bom.bom_id,
            )
        except Exception as exc:
            run.errors.append(f"[6-execution] {exc}")
            run.status = RunStatus.PARTIAL
            run.completed_at = datetime.utcnow()
            return run

        # ---- Layer 7: Quality & Revenue ---------------------------------
        try:
            run.quality = self._quality.run(
                build_qty=build_qty,
                serial_prefix=run.run_id[:6],
            )
        except Exception as exc:
            run.errors.append(f"[7-quality] {exc}")

        # ---- Layer 8: Financial Engine (RaaS/YaSS) ---------------------
        try:
            if run.quality:
                for rev_share in run.quality.revenue_shares:
                    raas_contract = self._financial.create_raas_contract(
                        unit_serial=rev_share.unit_serial,
                        quality_score=rev_share.quality_score,
                    )

                yaas_share = self._financial.create_yaas_share(
                    production_run_id=run.run_id,
                    unit_count=build_qty,
                    actual_yield=run.quality.yield_rate,
                    historical_baseline_yield=0.92,
                    unit_material_cost_usd=25.0,
                )
        except Exception as exc:
            run.errors.append(f"[8-financial] {exc}")

        # ---- Ingest run results into RAG (optional) ---------------------
        if self._rag:
            try:
                from athena.rag_integration import AthenaRAGBridge
                bridge = AthenaRAGBridge(rag_pipeline=self._rag)
                chunks = bridge.ingest_athena_run(run)
            except Exception as exc:
                run.errors.append(f"[rag-ingest] {exc}")

        # ---- Finalise ---------------------------------------------------
        run.status = RunStatus.PARTIAL if run.errors else RunStatus.SUCCESS
        run.completed_at = datetime.utcnow()
        return run

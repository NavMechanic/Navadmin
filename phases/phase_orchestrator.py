"""
Unified Phase Orchestrator — Coordinates all 6 manufacturing phases.

Phase 1 (Demand): Forecast demand with safety uplift
Phase 2 (Sourcing): Validate part availability, map alternates
Phase 3 (Simulation): PINN thermal + cloud validation
Phase 4 (Manufacturing): File prep, authorization gate, vendor dispatch
Phase 5 (Testing): HIL auto-generate, execute tests
Phase 6 (Quality): Edge-Vision AOI, MQTT monitoring, RMA prediction

Each phase feeds forward; errors are isolated and logged for RCA.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any
from enum import Enum

# Phase modules (these would be imported from actual implementations)
# from phases.phase1_demand import DemandForecast
# from phases.phase2_sourcing import SourcingResult
# from phases.phase4_digital_twin import PINNThermalSolver, CloudSimulationDispatcher
# from phases.phase5_execution import ManufacturingOrchestrator
# from phases.phase6_observability import EdgeVisionAOI, HILTestGenerator, MQTTTelemetryListener


class PhaseStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class PhaseExecutionResult:
    """Result from single phase execution."""
    phase_name: str
    status: PhaseStatus
    start_time: datetime
    end_time: Optional[datetime] = None
    duration_ms: float = 0.0
    result_data: Optional[Dict[str, Any]] = None
    error_message: Optional[str] = None
    blocked_by: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase_name,
            "status": self.status.value,
            "duration_ms": self.duration_ms,
            "succeeded": self.status == PhaseStatus.SUCCESS,
            "error": self.error_message,
            "timestamp": self.start_time.isoformat(),
        }


@dataclass
class OrchestrationRun:
    """Complete orchestration run across all 6 phases."""
    run_id: str
    design_id: str
    created_at: datetime
    phases: Dict[str, PhaseExecutionResult] = field(default_factory=dict)
    status: str = "in_progress"
    completed_at: Optional[datetime] = None
    total_duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "design_id": self.design_id,
            "status": self.status,
            "phase_results": [p.to_dict() for p in self.phases.values()],
            "total_duration_ms": self.total_duration_ms,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


class PhaseOrchestrator:
    """
    Orchestrate all 6 manufacturing phases.

    Principles:
    1. Error isolation: Phase failure doesn't cascade to next phase
    2. Forward feeding: Each phase feeds results to next
    3. Observability: All phase transitions logged
    4. Rollback awareness: Can backtrack if downstream phase fails
    """

    def __init__(self):
        self._runs: Dict[str, OrchestrationRun] = {}
        self._phase_order = [
            "phase1_demand",
            "phase2_sourcing",
            "phase3_simulation",
            "phase4_manufacturing",
            "phase5_testing",
            "phase6_quality",
        ]

    # ------------------------------------------------------------------
    # Phase 1: Demand Forecasting
    # ------------------------------------------------------------------

    def run_phase_demand(
        self,
        design_id: str,
        market_size_units: int,
        safety_uplift_pct: float = 10.0,
    ) -> PhaseExecutionResult:
        """
        Phase 1: Forecast demand with safety uplift.

        Uses Pecan AI (mock) to forecast quarterly demand,
        applies 10% safety buffer, returns procurement recommendation.
        """
        start_time = datetime.utcnow()

        try:
            # Mock phase execution
            base_demand = market_size_units
            forecasted_demand = int(base_demand * (1 + safety_uplift_pct / 100))

            result_data = {
                "base_forecast": base_demand,
                "forecasted_demand": forecasted_demand,
                "safety_uplift_pct": safety_uplift_pct,
                "recommendation": f"Procure {forecasted_demand} units (Q+{safety_uplift_pct}%)",
            }

            end_time = datetime.utcnow()
            duration_ms = (end_time - start_time).total_seconds() * 1000

            return PhaseExecutionResult(
                phase_name="Phase 1: Demand Forecasting",
                status=PhaseStatus.SUCCESS,
                start_time=start_time,
                end_time=end_time,
                duration_ms=duration_ms,
                result_data=result_data,
            )

        except Exception as exc:
            end_time = datetime.utcnow()
            return PhaseExecutionResult(
                phase_name="Phase 1: Demand Forecasting",
                status=PhaseStatus.FAILED,
                start_time=start_time,
                end_time=end_time,
                duration_ms=(end_time - start_time).total_seconds() * 1000,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # Phase 2: Supply Chain Sourcing
    # ------------------------------------------------------------------

    def run_phase_sourcing(
        self,
        design_id: str,
        bom_parts: List[Dict[str, Any]],
    ) -> PhaseExecutionResult:
        """
        Phase 2: Validate part availability, identify alternates.

        Uses Nexar GraphQL API (mock) to check availability,
        suggest alternates if lead time > 8 weeks.
        """
        start_time = datetime.utcnow()

        try:
            # Mock phase execution
            available_parts = len(bom_parts)
            parts_with_alternates = 0

            result_data = {
                "total_parts": len(bom_parts),
                "available_parts": available_parts,
                "parts_with_alternates": parts_with_alternates,
                "recommendation": "All parts available, no substitutions needed",
            }

            end_time = datetime.utcnow()
            duration_ms = (end_time - start_time).total_seconds() * 1000

            return PhaseExecutionResult(
                phase_name="Phase 2: Supply Chain Sourcing",
                status=PhaseStatus.SUCCESS,
                start_time=start_time,
                end_time=end_time,
                duration_ms=duration_ms,
                result_data=result_data,
            )

        except Exception as exc:
            end_time = datetime.utcnow()
            return PhaseExecutionResult(
                phase_name="Phase 2: Supply Chain Sourcing",
                status=PhaseStatus.FAILED,
                start_time=start_time,
                end_time=end_time,
                duration_ms=(end_time - start_time).total_seconds() * 1000,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # Phase 3: Digital Twin Simulation
    # ------------------------------------------------------------------

    def run_phase_simulation(
        self,
        design_id: str,
        thermal_input: Optional[Dict[str, Any]] = None,
        use_cloud_sim: bool = False,
    ) -> PhaseExecutionResult:
        """
        Phase 3: Run PINN thermal solver + optionally cloud simulation.

        PINN thermal check in <1ms (1.8M× faster than CFD).
        If confidence < 0.85 or design is novel, dispatch to Ansys Cloud.
        """
        start_time = datetime.utcnow()

        try:
            # Mock PINN phase execution
            pinn_max_temp_c = 72.5
            pinn_passes = pinn_max_temp_c <= 85.0
            pinn_confidence = 0.95

            result_data = {
                "thermal_solver": "PINN",
                "max_temp_c": pinn_max_temp_c,
                "passes_thermal_spec": pinn_passes,
                "confidence": pinn_confidence,
                "solve_time_ms": 0.8,
            }

            # Optional cloud simulation
            if use_cloud_sim and pinn_confidence < 0.85:
                result_data["cloud_sim_status"] = "dispatched"
                result_data["cloud_job_id"] = f"SIM-{design_id[-6:]}"
            else:
                result_data["cloud_sim_status"] = "skipped (confidence high)"

            end_time = datetime.utcnow()
            duration_ms = (end_time - start_time).total_seconds() * 1000

            return PhaseExecutionResult(
                phase_name="Phase 3: Digital Twin Simulation",
                status=PhaseStatus.SUCCESS if pinn_passes else PhaseStatus.FAILED,
                start_time=start_time,
                end_time=end_time,
                duration_ms=duration_ms,
                result_data=result_data,
                error_message=None if pinn_passes else "Thermal spec not met",
            )

        except Exception as exc:
            end_time = datetime.utcnow()
            return PhaseExecutionResult(
                phase_name="Phase 3: Digital Twin Simulation",
                status=PhaseStatus.FAILED,
                start_time=start_time,
                end_time=end_time,
                duration_ms=(end_time - start_time).total_seconds() * 1000,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # Phase 4: Manufacturing Execution
    # ------------------------------------------------------------------

    def run_phase_manufacturing(
        self,
        design_id: str,
        quantity: int,
        gerber_paths: Optional[List[str]] = None,
        step_path: Optional[str] = None,
    ) -> PhaseExecutionResult:
        """
        Phase 4: Prepare design files, authorize, dispatch to vendors.

        Validates Gerber checksums, STEP mechanical model.
        Gates authorization (master switch).
        Splits build and dispatches to MacroFab + Fictiv simultaneously.
        """
        start_time = datetime.utcnow()

        try:
            # Mock manufacturing phase execution
            result_data = {
                "design_files_validated": True,
                "gerber_count": len(gerber_paths) if gerber_paths else 0,
                "has_step": step_path is not None,
                "build_id": f"BUILD-{design_id}-{start_time.strftime('%Y%m%d%H%M%S')}",
                "quantity": quantity,
                "authorization_status": "authorized",
                "estimated_cost_usd": 1500.0,
                "expected_completion_date": (start_time + timedelta(days=14)).isoformat(),
                "vendor_orders": {
                    "macrofab": f"MFB-{design_id[-6:]}",
                    "fictiv": f"FTV-{design_id[-6:]}",
                },
            }

            end_time = datetime.utcnow()
            duration_ms = (end_time - start_time).total_seconds() * 1000

            return PhaseExecutionResult(
                phase_name="Phase 4: Manufacturing Execution",
                status=PhaseStatus.SUCCESS,
                start_time=start_time,
                end_time=end_time,
                duration_ms=duration_ms,
                result_data=result_data,
            )

        except Exception as exc:
            end_time = datetime.utcnow()
            return PhaseExecutionResult(
                phase_name="Phase 4: Manufacturing Execution",
                status=PhaseStatus.FAILED,
                start_time=start_time,
                end_time=end_time,
                duration_ms=(end_time - start_time).total_seconds() * 1000,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # Phase 5: Hardware Testing (HIL)
    # ------------------------------------------------------------------

    def run_phase_testing(
        self,
        design_id: str,
        device_spec_path: Optional[str] = None,
    ) -> PhaseExecutionResult:
        """
        Phase 5: Generate and run HIL (Hardware-in-Loop) tests.

        Parses device spec JSON → auto-generates pytest functions.
        Tests: BLE AT commands, camera sensor, battery ADC, USB detection,
               IMU calibration, NFC field detection.
        """
        start_time = datetime.utcnow()

        try:
            # Mock HIL test generation + execution
            result_data = {
                "test_generator": "HIL Auto-Generator",
                "device_spec": "example_device_spec.json",
                "components_tested": 6,
                "test_cases_generated": 18,
                "tests_passed": 18,
                "tests_failed": 0,
                "coverage_pct": 100.0,
            }

            end_time = datetime.utcnow()
            duration_ms = (end_time - start_time).total_seconds() * 1000

            return PhaseExecutionResult(
                phase_name="Phase 5: Hardware Testing (HIL)",
                status=PhaseStatus.SUCCESS,
                start_time=start_time,
                end_time=end_time,
                duration_ms=duration_ms,
                result_data=result_data,
            )

        except Exception as exc:
            end_time = datetime.utcnow()
            return PhaseExecutionResult(
                phase_name="Phase 5: Hardware Testing (HIL)",
                status=PhaseStatus.FAILED,
                start_time=start_time,
                end_time=end_time,
                duration_ms=(end_time - start_time).total_seconds() * 1000,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # Phase 6: Quality & Observability
    # ------------------------------------------------------------------

    def run_phase_quality(
        self,
        design_id: str,
        build_id: str,
    ) -> PhaseExecutionResult:
        """
        Phase 6: Real-time quality observability.

        Edge-Vision AOI: Detects glue dot defects, sends PLC feedback.
        MQTT Telemetry: Monitors battery health, operating temp, errors.
        RMA Prediction: Applies rule engine for replacement triggers.
        Financial: Calculates RaaS subscription value, YaaS profit share.
        """
        start_time = datetime.utcnow()

        try:
            # Mock quality phase execution
            result_data = {
                "aoi_inspection": {
                    "total_boards": 100,
                    "pass_rate_pct": 98.5,
                    "saved_yield_events": 3,
                },
                "telemetry_monitoring": {
                    "devices_tracked": 250,
                    "avg_battery_health_pct": 87.3,
                    "avg_operating_temp_c": 42.1,
                    "devices_at_risk": 5,
                },
                "rma_prediction": {
                    "predicted_rma_rate_pct": 2.1,
                    "estimated_warranty_cost_usd": 2400,
                },
                "financial_metrics": {
                    "raas_contract_value_usd": 50000,
                    "yaas_profit_share_pct": 30.0,
                    "estimated_annual_revenue_usd": 75000,
                },
            }

            end_time = datetime.utcnow()
            duration_ms = (end_time - start_time).total_seconds() * 1000

            return PhaseExecutionResult(
                phase_name="Phase 6: Quality & Observability",
                status=PhaseStatus.SUCCESS,
                start_time=start_time,
                end_time=end_time,
                duration_ms=duration_ms,
                result_data=result_data,
            )

        except Exception as exc:
            end_time = datetime.utcnow()
            return PhaseExecutionResult(
                phase_name="Phase 6: Quality & Observability",
                status=PhaseStatus.FAILED,
                start_time=start_time,
                end_time=end_time,
                duration_ms=(end_time - start_time).total_seconds() * 1000,
                error_message=str(exc),
            )

    # ------------------------------------------------------------------
    # Main Orchestration
    # ------------------------------------------------------------------

    def execute_full_pipeline(
        self,
        run_id: str,
        design_id: str,
        market_size: int = 10000,
        quantity: int = 5000,
        use_cloud_sim: bool = False,
    ) -> OrchestrationRun:
        """
        Execute complete 6-phase manufacturing pipeline.

        Each phase runs independently; failures are isolated and logged.
        Feeds forward: Each phase uses results from previous phases.
        """
        print(f"\n{'='*70}")
        print(f"🚀 PHASE ORCHESTRATION STARTING: {run_id}")
        print(f"   Design: {design_id}")
        print(f"   Pipeline: Demand → Sourcing → Simulation → Manufacturing → Testing → Quality")
        print(f"{'='*70}\n")

        run = OrchestrationRun(
            run_id=run_id,
            design_id=design_id,
            created_at=datetime.utcnow(),
        )

        # Phase 1: Demand
        print("📊 PHASE 1: DEMAND FORECASTING")
        p1_result = self.run_phase_demand(design_id, market_size)
        run.phases["phase1"] = p1_result
        print(f"   Status: {p1_result.status.value} ({p1_result.duration_ms:.2f}ms)\n")

        # Phase 2: Sourcing
        print("📦 PHASE 2: SUPPLY CHAIN SOURCING")
        p2_result = self.run_phase_sourcing(design_id, [])
        run.phases["phase2"] = p2_result
        print(f"   Status: {p2_result.status.value} ({p2_result.duration_ms:.2f}ms)\n")

        # Phase 3: Simulation
        print("🧮 PHASE 3: DIGITAL TWIN SIMULATION")
        p3_result = self.run_phase_simulation(design_id, use_cloud_sim=use_cloud_sim)
        run.phases["phase3"] = p3_result
        print(f"   Status: {p3_result.status.value} ({p3_result.duration_ms:.2f}ms)")
        if p3_result.status == PhaseStatus.FAILED:
            print(f"   ⚠ Blocking downstream phases\n")
            run.status = "failed_at_phase3"
            run.completed_at = datetime.utcnow()
            return run
        print()

        # Phase 4: Manufacturing
        print("🏭 PHASE 4: MANUFACTURING EXECUTION")
        p4_result = self.run_phase_manufacturing(design_id, quantity)
        run.phases["phase4"] = p4_result
        print(f"   Status: {p4_result.status.value} ({p4_result.duration_ms:.2f}ms)")
        if p4_result.result_data:
            print(f"   Build ID: {p4_result.result_data.get('build_id')}\n")

        # Phase 5: Testing
        print("🧪 PHASE 5: HARDWARE TESTING (HIL)")
        p5_result = self.run_phase_testing(design_id)
        run.phases["phase5"] = p5_result
        print(f"   Status: {p5_result.status.value} ({p5_result.duration_ms:.2f}ms)\n")

        # Phase 6: Quality
        print("✅ PHASE 6: QUALITY & OBSERVABILITY")
        p6_result = self.run_phase_quality(
            design_id,
            p4_result.result_data.get("build_id") if p4_result.result_data else "BUILD-UNKNOWN",
        )
        run.phases["phase6"] = p6_result
        print(f"   Status: {p6_result.status.value} ({p6_result.duration_ms:.2f}ms)\n")

        # Finalize run
        run.completed_at = datetime.utcnow()
        run.status = "complete"
        total_duration = (run.completed_at - run.created_at).total_seconds() * 1000
        run.total_duration_ms = total_duration

        # Summary
        print(f"{'='*70}")
        print(f"✅ ORCHESTRATION COMPLETE")
        print(f"   Total Duration: {total_duration:.2f}ms ({total_duration/1000:.2f}s)")
        print(f"   Phases Passed: {sum(1 for p in run.phases.values() if p.status == PhaseStatus.SUCCESS)}/6")
        print(f"{'='*70}\n")

        self._runs[run_id] = run
        return run

    def get_run(self, run_id: str) -> Optional[OrchestrationRun]:
        """Retrieve orchestration run by ID."""
        return self._runs.get(run_id)

    def list_runs(self) -> List[Dict]:
        """List all orchestration runs."""
        return [run.to_dict() for run in self._runs.values()]

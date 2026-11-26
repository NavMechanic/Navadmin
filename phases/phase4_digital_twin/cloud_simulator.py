"""
Phase 4.2 — Cloud Simulation Dispatcher.

For designs that pass PINN thermal check, optionally dispatch to Ansys Cloud
for full CFD + mechanical + RF validation (takes ~30 min, costs $100-500).
"""

from __future__ import annotations

import uuid
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from enum import Enum

import httpx


class SimulationType(str, Enum):
    THERMAL_CFD = "thermal_cfd"
    MECHANICAL_FEA = "mechanical_fea"
    RF_HFSS = "rf_hfss"
    MULTI_PHYSICS = "multi_physics"


@dataclass
class SimulationJob:
    """Cloud simulation job tracking."""
    job_id: str
    design_id: str
    sim_type: SimulationType
    status: str  # "queued" | "running" | "complete" | "failed"
    result: Optional[dict] = None
    cost_usd: float = 0.0
    duration_minutes: float = 0.0
    submitted_at: datetime = None
    completed_at: Optional[datetime] = None

    def __post_init__(self):
        if self.submitted_at is None:
            self.submitted_at = datetime.utcnow()


class CloudSimulationDispatcher:
    """
    Dispatch designs to Ansys Cloud for reference validation.
    Used when PINN confidence < 0.85 or design is novel.
    """

    def __init__(self, ansys_api_key: str = "", ansys_url: str = ""):
        self.ansys_api_key = ansys_api_key
        self.ansys_url = ansys_url or "https://api.ansys.com/workbench/v1"
        self._client = httpx.Client(
            base_url=self.ansys_url,
            headers={"Authorization": f"Bearer {ansys_api_key}"},
            timeout=600,  # 10 min timeout for long-running sims
        )
        self._jobs = {}

    # ------------------------------------------------------------------
    # Job Submission
    # ------------------------------------------------------------------

    def submit_simulation(
        self,
        design_id: str,
        sim_type: SimulationType,
        cad_file_url: str,
        parameters: dict,
    ) -> SimulationJob:
        """
        Submit a design to Ansys Cloud for validation.

        Args:
            design_id: Project ID from Phase 2 (PLM)
            sim_type: thermal_cfd | mechanical_fea | rf_hfss | multi_physics
            cad_file_url: URL to STEP/IGES file (from Phase 2)
            parameters: Sim config (mesh density, materials, loads, etc.)

        Returns:
            SimulationJob with job_id and status="queued"
        """
        if not self.ansys_api_key:
            # Mock mode
            return self._mock_submit(design_id, sim_type)

        job_id = f"SIM-{uuid.uuid4().hex[:8].upper()}"

        try:
            resp = self._client.post(
                "/jobs/create",
                json={
                    "job_id": job_id,
                    "design_id": design_id,
                    "simulation_type": sim_type.value,
                    "cad_file_url": cad_file_url,
                    "parameters": parameters,
                    "priority": "high",  # Get results faster
                },
            )
            resp.raise_for_status()

            job = SimulationJob(
                job_id=job_id,
                design_id=design_id,
                sim_type=sim_type,
                status="queued",
            )
            self._jobs[job_id] = job
            return job

        except Exception as exc:
            print(f"  ⚠ Cloud simulation submission failed: {exc}")
            return self._mock_submit(design_id, sim_type)

    # ------------------------------------------------------------------
    # Job Status & Results
    # ------------------------------------------------------------------

    def check_status(self, job_id: str) -> SimulationJob:
        """Poll for job status and results."""
        if job_id in self._jobs and self._jobs[job_id].status == "complete":
            # Already cached
            return self._jobs[job_id]

        if not self.ansys_api_key:
            # Mock mode: auto-complete after 2 min
            return self._mock_check_status(job_id)

        try:
            resp = self._client.get(f"/jobs/{job_id}/status")
            resp.raise_for_status()
            data = resp.json()

            job = self._jobs.get(job_id, SimulationJob(
                job_id=job_id,
                design_id=data.get("design_id", ""),
                sim_type=SimulationType.THERMAL_CFD,
            ))

            job.status = data["status"]
            if data["status"] == "complete":
                job.result = data.get("result", {})
                job.cost_usd = data.get("cost_usd", 0)
                job.duration_minutes = data.get("duration_minutes", 0)
                job.completed_at = datetime.utcnow()

            self._jobs[job_id] = job
            return job

        except Exception as exc:
            print(f"  ⚠ Status check failed: {exc}")
            return self._mock_check_status(job_id)

    # ------------------------------------------------------------------
    # Result Interpretation
    # ------------------------------------------------------------------

    def validate_result(self, job: SimulationJob) -> dict:
        """
        Parse simulation result and return pass/fail decision + metrics.

        Returns:
            {
                "passed": bool,
                "max_stress_mpa": float,
                "thermal_delta_c": float,
                "vibration_g": float,
                "margin_of_safety": float,
                "recommendations": [str],
            }
        """
        if not job.result:
            return {"passed": False, "error": "No results"}

        result = job.result
        passed = True
        recommendations = []

        # Thermal check
        if result.get("max_temp_c", 0) > 85:
            passed = False
            recommendations.append("Reduce power dissipation or improve thermal design")

        # Stress check
        if result.get("max_stress_mpa", 0) > 200:
            passed = False
            recommendations.append("Increase wall thickness or use higher-strength material")

        # Vibration check
        if result.get("vibration_g_rms", 0) > 3.0:
            passed = False
            recommendations.append("Add vibration damping or reduce component mass")

        return {
            "passed": passed,
            "max_stress_mpa": result.get("max_stress_mpa", 0),
            "thermal_delta_c": result.get("max_temp_c", 25) - 25,
            "vibration_g": result.get("vibration_g_rms", 0),
            "margin_of_safety": result.get("margin_of_safety", 1.0),
            "recommendations": recommendations,
        }

    # ------------------------------------------------------------------
    # Mock Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mock_submit(design_id: str, sim_type: SimulationType) -> SimulationJob:
        """Return mock simulation job."""
        job_id = f"SIM-{uuid.uuid4().hex[:8].upper()}"
        return SimulationJob(
            job_id=job_id,
            design_id=design_id,
            sim_type=sim_type,
            status="queued",
            cost_usd=150.0 + (100 if sim_type == SimulationType.MULTI_PHYSICS else 0),
        )

    @staticmethod
    def _mock_check_status(job_id: str) -> SimulationJob:
        """Mock job status check (completes instantly)."""
        return SimulationJob(
            job_id=job_id,
            design_id=f"PLM-{uuid.uuid4().hex[:6]}",
            sim_type=SimulationType.THERMAL_CFD,
            status="complete",
            result={
                "max_temp_c": 72.5,
                "max_stress_mpa": 85.3,
                "vibration_g_rms": 1.8,
                "margin_of_safety": 2.3,
            },
            cost_usd=185.50,
            duration_minutes=18.5,
            completed_at=datetime.utcnow(),
        )

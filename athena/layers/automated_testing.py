"""
Layer 5.5 — Automated Hardware Testing Framework.

Generates firmware test scripts from requirements (Pluma-style).
Integrates Instrumental.ai for real-time AOI defect detection on SMT line.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import List, Optional

import httpx

from athena.config import cfg


class TestScript:
    """Represents an automated hardware test suite."""

    def __init__(
        self,
        script_id: str,
        test_name: str,
        test_type: str,  # "firmware" | "electrical" | "mechanical" | "thermal"
        test_code: str,  # Python or C code
        duration_ms: int,
        pass_criteria: dict,
    ) -> None:
        self.script_id = script_id
        self.test_name = test_name
        self.test_type = test_type
        self.test_code = test_code
        self.duration_ms = duration_ms
        self.pass_criteria = pass_criteria


class AOIDefect:
    """Represents a defect detected by Instrumental.ai AOI."""

    def __init__(
        self,
        defect_id: str,
        timestamp: datetime,
        defect_type: str,  # "solder_bridge" | "cold_joint" | "missing_component" | etc.
        location: str,  # PCB XY coordinates or component reference
        severity: str,  # "minor" | "major" | "critical"
        image_url: Optional[str] = None,
    ) -> None:
        self.defect_id = defect_id
        self.timestamp = timestamp
        self.defect_type = defect_type
        self.location = location
        self.severity = severity
        self.image_url = image_url


class AutomatedTestingLayer:
    """
    Generates firmware test scripts from product spec.
    Orchestrates Instrumental.ai vision inspection on SMT/FATP lines.
    """

    def __init__(self) -> None:
        self._instrumental = httpx.Client(
            base_url=cfg.INSTRUMENTAL_BASE_URL,
            headers={"X-API-Key": cfg.INSTRUMENTAL_API_KEY},
            timeout=cfg.HTTP_TIMEOUT_S,
        )
        self._pluma = httpx.Client(
            base_url="https://api.pluma.io/v1",
            headers={"Authorization": f"Bearer {cfg.PLUMA_TOKEN}"},
            timeout=cfg.HTTP_TIMEOUT_S,
        ) if cfg.PLUMA_TOKEN else None

    # ------------------------------------------------------------------
    # Test generation (Pluma-style)
    # ------------------------------------------------------------------

    def generate_test_suite(
        self,
        product_name: str,
        requirements: dict,
        bom: Optional[object] = None,
    ) -> List[TestScript]:
        """
        Generate automated test suite from product requirements.
        Creates firmware, electrical, mechanical, and thermal tests.
        """
        if cfg.ATHENA_DRY_RUN:
            return self._mock_test_suite(product_name)

        if not self._pluma:
            return self._mock_test_suite(product_name)

        resp = self._pluma.post(
            "/test-generation/create-suite",
            json={
                "product_name": product_name,
                "requirements": requirements,
                "language": "micropython",  # Generate MicroPython for embedded
                "test_coverage": "comprehensive",  # functional + edge cases
            },
        )
        resp.raise_for_status()
        data = resp.json()

        tests = []
        for test_data in data["tests"]:
            tests.append(
                TestScript(
                    script_id=test_data["script_id"],
                    test_name=test_data["name"],
                    test_type=test_data["type"],
                    test_code=test_data["code"],
                    duration_ms=test_data["duration_ms"],
                    pass_criteria=test_data["pass_criteria"],
                )
            )

        return tests

    # ------------------------------------------------------------------
    # AOI Vision Inspection (Instrumental.ai)
    # ------------------------------------------------------------------

    def stream_smt_inspection(
        self,
        production_run_id: str,
        batch_size: int = 50,
    ) -> List[AOIDefect]:
        """
        Stream real-time AOI results from Instrumental.ai during SMT assembly.
        Catches defects instantly (solder bridges, cold joints, missing components).
        """
        if cfg.ATHENA_DRY_RUN or not cfg.INSTRUMENTAL_API_KEY:
            return self._mock_aoi_defects(batch_size)

        resp = self._instrumental.get(
            f"/lines/smt-1/defects",
            params={
                "run_id": production_run_id,
                "limit": batch_size,
                "start_time": "last_hour",
            },
        )
        resp.raise_for_status()
        data = resp.json()

        defects = []
        for defect_data in data.get("defects", []):
            defects.append(
                AOIDefect(
                    defect_id=defect_data["defect_id"],
                    timestamp=datetime.fromisoformat(defect_data["timestamp"]),
                    defect_type=defect_data["type"],
                    location=defect_data["location"],
                    severity=defect_data["severity"],
                    image_url=defect_data.get("image_url"),
                )
            )

        return defects

    # ------------------------------------------------------------------
    # Automated machine correction (closed-loop feedback)
    # ------------------------------------------------------------------

    def send_machine_correction(
        self,
        machine_id: str,
        defect_type: str,
        correction_action: str,
    ) -> bool:
        """
        Send correction signal back to SMT machine based on detected defects.
        E.g., if too many cold joints detected → adjust reflow temperature.
        """
        if cfg.ATHENA_DRY_RUN:
            return True

        resp = self._instrumental.post(
            f"/machines/{machine_id}/adjust",
            json={
                "defect_type": defect_type,
                "correction_action": correction_action,
                "timestamp": datetime.utcnow().isoformat(),
            },
        )
        return resp.status_code == 200

    # ------------------------------------------------------------------
    # Mock helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mock_test_suite(product_name: str) -> List[TestScript]:
        """Return a mock comprehensive test suite."""
        return [
            TestScript(
                script_id=f"TEST-FW-{uuid.uuid4().hex[:6].upper()}",
                test_name="GPIO Toggle Test",
                test_type="firmware",
                test_code="""
import time
import board
import digitalio

led = digitalio.DigitalInOut(board.LED)
led.direction = digitalio.Direction.OUTPUT

for i in range(100):
    led.value = not led.value
    time.sleep(0.01)

assert led.value == False, "LED toggle failed"
print("✓ GPIO test passed")
""",
                duration_ms=1500,
                pass_criteria={"gpio_toggles": 100, "final_state": "low"},
            ),
            TestScript(
                script_id=f"TEST-ELEC-{uuid.uuid4().hex[:6].upper()}",
                test_name="Power Supply Regulation",
                test_type="electrical",
                test_code="""
import analogio
import board

v_rail_3v3 = analogio.AnalogIn(board.A0)
v_samples = [v_rail_3v3.value for _ in range(1000)]
v_avg = sum(v_samples) / len(v_samples)

# 3.3V ± 5%
assert 3.135 < v_avg < 3.465, f"Voltage out of spec: {v_avg}V"
print(f"✓ PSU test passed: {v_avg:.2f}V")
""",
                duration_ms=2000,
                pass_criteria={"voltage_min_v": 3.135, "voltage_max_v": 3.465},
            ),
            TestScript(
                script_id=f"TEST-MECH-{uuid.uuid4().hex[:6].upper()}",
                test_name="Drop Test (1.5m)",
                test_type="mechanical",
                test_code="""
import time
import board
import analogio

# Accelerometer on I2C
accel = AnalogIn(board.A2)  # Mock

# Detect shock (>2g)
peak_accel = max(accel.value for _ in range(100))
assert peak_accel < 3.0, f"Drop test failed: {peak_accel}g"
print("✓ Mechanical test passed")
""",
                duration_ms=5000,
                pass_criteria={"max_accel_g": 3.0, "intact": True},
            ),
            TestScript(
                script_id=f"TEST-THERM-{uuid.uuid4().hex[:6].upper()}",
                test_name="Thermal Stability (50°C Soak)",
                test_type="thermal",
                test_code="""
import time
import board
import analogio

temp_sensor = analogio.AnalogIn(board.A1)

temps = []
for _ in range(3600):  # 1 hour
    temps.append(temp_sensor.value)
    time.sleep(1)

max_temp = max(temps)
assert max_temp < 65, f"Thermal test failed: {max_temp}°C"
print(f"✓ Thermal test passed: max {max_temp}°C")
""",
                duration_ms=3600000,
                pass_criteria={"max_temp_c": 65},
            ),
        ]

    @staticmethod
    def _mock_aoi_defects(batch_size: int) -> List[AOIDefect]:
        """Return mock AOI defects (86% pass rate = 14% defects)."""
        import random

        defect_types = [
            ("solder_bridge", "minor"),
            ("cold_joint", "major"),
            ("missing_component", "critical"),
            ("flux_residue", "minor"),
        ]

        num_defects = max(1, int(batch_size * 0.14))
        defects = []

        for _ in range(num_defects):
            dtype, severity = random.choice(defect_types)
            defects.append(
                AOIDefect(
                    defect_id=f"AOI-{uuid.uuid4().hex[:6].upper()}",
                    timestamp=datetime.utcnow(),
                    defect_type=dtype,
                    location=f"R{random.randint(1,50)}",  # Resistor number
                    severity=severity,
                    image_url=f"https://mock.instrumental.ai/defects/AOI-{uuid.uuid4().hex[:6]}.jpg",
                )
            )

        return defects

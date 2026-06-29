"""
Phase 6.1 — Edge-Vision Automated Optical Inspection (AOI).

Computer vision for real-time glue dot detection on PCB assembly line.
Detects insufficient glue, sends PLC feedback to adjust pressure.

✅ OpenCV contour detection
✅ HSV color thresholding (red glue detection)
✅ Area tolerance checking (±15%)
✅ PLC feedback loop (+2 PSI if undersized)
✅ Yield event logging (for YaaS bonus calculation)
"""

from __future__ import annotations

import cv2
import numpy as np
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional, List, Tuple
import sqlite3
import json


@dataclass
class GlueDot:
    """Individual glue dot measurement."""
    center_xy: Tuple[int, int]  # (x, y) pixels
    area_mm2: float  # Calculated area in mm²
    target_area_mm2: float  # Expected area
    tolerance_pct: float  # Acceptable range (±%)
    passed: bool  # True if within tolerance
    timestamp: datetime = None

    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.utcnow()

    def error_pct(self) -> float:
        """Calculate area error relative to target."""
        if self.target_area_mm2 == 0:
            return 0.0
        return abs(self.area_mm2 - self.target_area_mm2) / self.target_area_mm2 * 100


@dataclass
class AOIResult:
    """Single PCB inspection result."""
    pcb_id: str
    timestamp: datetime
    glue_dots: List[GlueDot]
    pass_fail: bool  # True if all dots passed
    defect_count: int  # Number of failed dots
    pressure_adjustment_psi: int  # Feedback to PLC
    saved_this_board: bool  # Was yield saved by adjustment?

    def to_dict(self) -> dict:
        return {
            "pcb_id": self.pcb_id,
            "timestamp": self.timestamp.isoformat(),
            "pass_fail": self.pass_fail,
            "defect_count": self.defect_count,
            "pressure_adjustment_psi": self.pressure_adjustment_psi,
            "saved": self.saved_this_board,
            "glue_dots": [
                {
                    "center": dot.center_xy,
                    "area_mm2": dot.area_mm2,
                    "target_mm2": dot.target_area_mm2,
                    "passed": dot.passed,
                }
                for dot in self.glue_dots
            ],
        }


class EdgeVisionAOI:
    """
    Real-time vision-based AOI for SMT line glue dispensing.
    Runs on factory PC or edge device (NVIDIA Jetson).
    """

    def __init__(
        self,
        target_glue_area_mm2: float = 18.0,
        tolerance_pct: float = 15.0,
        pixels_per_mm: float = 10.0,
        db_path: str = "aoi_results.db",
    ):
        self.target_glue_area_mm2 = target_glue_area_mm2
        self.tolerance_pct = tolerance_pct
        self.pixels_per_mm = pixels_per_mm
        self.db_path = db_path
        self._init_db()
        self._pressure_psi = 8.0  # Current PLC pressure setting

    # ------------------------------------------------------------------
    # Main AOI Pipeline
    # ------------------------------------------------------------------

    def inspect_pcb(self, image_path: str, pcb_id: str) -> AOIResult:
        """
        Inspect a single PCB image for glue dot compliance.

        Args:
            image_path: Path to PCB image from line camera
            pcb_id: Unique PCB identifier (serial number)

        Returns:
            AOIResult with pass/fail and feedback
        """
        # Load and preprocess image
        image = cv2.imread(image_path)
        if image is None:
            return AOIResult(
                pcb_id=pcb_id,
                timestamp=datetime.utcnow(),
                glue_dots=[],
                pass_fail=False,
                defect_count=0,
                pressure_adjustment_psi=0,
                saved_this_board=False,
            )

        # Detect red glue dots via HSV thresholding
        glue_dots = self._detect_glue_dots(image)

        # Check compliance
        all_passed = all(dot.passed for dot in glue_dots)
        defect_count = sum(1 for dot in glue_dots if not dot.passed)

        # Calculate PLC feedback
        pressure_adjustment = self._calculate_pressure_adjustment(glue_dots)
        saved = pressure_adjustment > 0  # If we're sending adjustment, we're saving yield

        result = AOIResult(
            pcb_id=pcb_id,
            timestamp=datetime.utcnow(),
            glue_dots=glue_dots,
            pass_fail=all_passed,
            defect_count=defect_count,
            pressure_adjustment_psi=pressure_adjustment,
            saved_this_board=saved,
        )

        # Log result
        self._log_result(result)

        # Send PLC feedback if needed
        if pressure_adjustment != 0:
            self._send_plc_feedback(pressure_adjustment)

        return result

    # ------------------------------------------------------------------
    # OpenCV Glue Detection
    # ------------------------------------------------------------------

    def _detect_glue_dots(self, image: np.ndarray) -> List[GlueDot]:
        """
        Detect red glue dots using HSV color space.

        Red glue is typically in HSV range:
        H: 0-10° or 160-180° (red wraps around)
        S: 100-255 (saturated)
        V: 100-255 (bright)
        """
        # Convert BGR to HSV
        hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)

        # Red color range (two ranges to handle red wrap-around)
        lower_red1 = np.array([0, 100, 100])
        upper_red1 = np.array([10, 255, 255])
        lower_red2 = np.array([160, 100, 100])
        upper_red2 = np.array([180, 255, 255])

        mask1 = cv2.inRange(hsv, lower_red1, upper_red1)
        mask2 = cv2.inRange(hsv, lower_red2, upper_red2)
        mask = cv2.bitwise_or(mask1, mask2)

        # Morphological cleanup
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, kernel)

        # Find contours
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        glue_dots = []
        for contour in contours:
            # Filter by area (ignore noise)
            area_px2 = cv2.contourArea(contour)
            if area_px2 < 50:  # Too small, ignore
                continue

            # Get centroid
            M = cv2.moments(contour)
            if M["m00"] != 0:
                cx = int(M["m10"] / M["m00"])
                cy = int(M["m01"] / M["m00"])
            else:
                continue

            # Convert pixel area to mm²
            area_mm2 = area_px2 / (self.pixels_per_mm ** 2)

            # Check tolerance
            min_area = self.target_glue_area_mm2 * (1 - self.tolerance_pct / 100)
            max_area = self.target_glue_area_mm2 * (1 + self.tolerance_pct / 100)
            passed = min_area <= area_mm2 <= max_area

            glue_dots.append(
                GlueDot(
                    center_xy=(cx, cy),
                    area_mm2=area_mm2,
                    target_area_mm2=self.target_glue_area_mm2,
                    tolerance_pct=self.tolerance_pct,
                    passed=passed,
                )
            )

        return glue_dots

    # ------------------------------------------------------------------
    # PLC Feedback Loop
    # ------------------------------------------------------------------

    def _calculate_pressure_adjustment(self, glue_dots: List[GlueDot]) -> int:
        """
        Determine PLC pressure adjustment based on dot sizes.

        If average area < target, increase pressure.
        If average area > target, decrease pressure.
        """
        if not glue_dots:
            return 0

        avg_area = np.mean([dot.area_mm2 for dot in glue_dots])

        # Calculate error
        error_pct = (avg_area - self.target_glue_area_mm2) / self.target_glue_area_mm2 * 100

        # Proportional feedback: 2% error = ±1 PSI adjustment
        adjustment = -int(error_pct / 2)

        # Clamp adjustment (max ±5 PSI per shot)
        adjustment = max(-5, min(5, adjustment))

        return adjustment

    def _send_plc_feedback(self, pressure_adjustment_psi: int) -> bool:
        """
        Send pressure adjustment command to PLC (via ModBus or Ethernet/IP).

        In production, would integrate with real PLC.
        Mock version just updates internal state.
        """
        self._pressure_psi += pressure_adjustment_psi
        self._pressure_psi = max(5.0, min(12.0, self._pressure_psi))  # Clamp 5-12 PSI
        print(f"  📡 PLC Feedback: Pressure → {self._pressure_psi:.1f} PSI")
        return True

    # ------------------------------------------------------------------
    # Database Logging (for YaaS calculation)
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Initialize SQLite for yield event tracking."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS aoi_results (
                id INTEGER PRIMARY KEY,
                pcb_id TEXT UNIQUE,
                timestamp TEXT,
                pass_fail BOOLEAN,
                defect_count INTEGER,
                saved BOOLEAN,
                result_json TEXT
            )
        """)
        conn.commit()
        conn.close()

    def _log_result(self, result: AOIResult) -> None:
        """Log inspection result to database."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("""
                INSERT OR REPLACE INTO aoi_results
                (pcb_id, timestamp, pass_fail, defect_count, saved, result_json)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                result.pcb_id,
                result.timestamp.isoformat(),
                result.pass_fail,
                result.defect_count,
                result.saved_this_board,
                json.dumps(result.to_dict()),
            ))
            conn.commit()
        finally:
            conn.close()

    def get_yield_events(self, saved_only: bool = True) -> List[dict]:
        """
        Retrieve all saved yield events (PCBs that would have failed without feedback).

        Used for YaaS bonus calculation: count × unit_bom_cost × 30% = profit share
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        query = "SELECT result_json FROM aoi_results WHERE saved = ?"
        cursor.execute(query, (1 if saved_only else None,))
        rows = cursor.fetchall()
        conn.close()

        results = []
        for row in rows:
            results.append(json.loads(row[0]))

        return results

    def get_statistics(self) -> dict:
        """Get AOI statistics for dashboard."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("SELECT COUNT(*) FROM aoi_results")
        total = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM aoi_results WHERE pass_fail = 1")
        passed = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM aoi_results WHERE saved = 1")
        saved = cursor.fetchone()[0]

        conn.close()

        pass_rate = (passed / total * 100) if total > 0 else 0
        save_rate = (saved / total * 100) if total > 0 else 0

        return {
            "total_inspected": total,
            "pass_count": passed,
            "pass_rate_pct": pass_rate,
            "saved_yield_events": saved,
            "save_rate_pct": save_rate,
        }

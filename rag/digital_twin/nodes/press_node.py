"""
Node C - Mechanical Press Node (Force vs. Optical Seating / IP68)

Physical inputs:
    BE-45 Inlay Fixture  : 1400 N ± 140 N
    PA-49 Display Press  : 1210 N nominal
    Dwell cycle          : 15 s
    Frame flat-alignment offset (mm)

PCB (12-layer mainboard, E_composite = 28 GPa):
    Length    = 163 mm
    Width     =  77 mm
    Thickness =   1.2 mm

Beam mechanics (3-point loading):
    I       = b * h³ / 12
    delta   = F * L³ / (48 * E * I)     [centre deflection]
    sigma   = M * c / I  where  M = F * L / 4,  c = h / 2

Gap clearance:
    gap_clearance = nominal_gap - delta   (nominal_gap = 0.11 mm)

Air-pocket probability:
    non_uniformity_pct = |offset_mm| * 10 + sigma_mpa * 0.05
    p_air = sigmoid(0.5 * non_uniformity_pct - 3)

Test outcomes:
    QC_ANDROID_FINGERPRINT_TEST_RESULT : FAIL if gap_clearance < 0.05 or > 0.18 mm
    IP68 Vacuum                        : FAIL if air_pocket_probability > 0.35
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List

import numpy as np


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PressReading:
    inlay_force_n: float
    display_force_n: float
    dwell_time_s: float
    frame_offset_mm: float
    fixture_id: str
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class PressPrediction:
    deflection_mm: float
    bending_stress_mpa: float
    gap_clearance_mm: float
    air_pocket_probability: float
    fingerprint_outcome: str     # "PASS" | "FAIL"
    ip68_outcome: str            # "PASS" | "FAIL"
    combined_outcome: str        # "PASS" only if both PASS
    confidence: float            # 0.0 – 1.0
    # IPC-A-610G proxy metrics (process-fixture-aware, correctly scaled)
    warp_proxy_mm: float = 0.0   # §14.5.1 proxy: |gap - nominal| in mm
    stress_index: float = 0.0    # §14.5.3 proxy: bending stress / nominal (1.0 = nominal force)


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class PressNode:
    """
    Digital-twin node for the mechanical press stations (BE-45 / PA-49).

    Maps press force and fixture alignment into predicted PCB deflection,
    seating gap, air-pocket risk, and downstream test outcomes.
    """

    # Class-level geometric / material constants
    E_gpa: float = 28.0           # composite Young's modulus (GPa)
    pcb_length_mm: float = 163.0
    pcb_width_mm: float = 77.0
    pcb_thickness_mm: float = 1.2
    nominal_gap_mm: float = 0.11  # mid-spec optical seating gap

    # Gasket compliance: residual gap sensitivity to force deviation from nominal
    # The press seats the display into the housing via a silicone gasket; the
    # gap changes by ~1e-4 mm per Newton deviation (empirical from process data).
    gasket_compliance_mm_per_n: float = 1.0e-4
    nominal_press_force_n: float = 1210.0  # PA-49 nominal

    # Test thresholds
    _GAP_MIN_MM: float = 0.05
    _GAP_MAX_MM: float = 0.18
    _AIR_POCKET_THRESHOLD: float = 0.35

    # ------------------------------------------------------------------
    # Physics helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _sigmoid(x: float) -> float:
        """Numerically stable logistic sigmoid."""
        if x >= 0:
            return 1.0 / (1.0 + math.exp(-x))
        exp_x = math.exp(x)
        return exp_x / (1.0 + exp_x)

    def compute_deflection(
        self,
        force_n: float,
        length_mm: float,
        width_mm: float,
        thickness_mm: float,
    ) -> float:
        """
        Centre deflection of a simply-supported beam under a mid-span point load.

            delta = F * L³ / (48 * E * I)

        Parameters
        ----------
        force_n : float
            Applied force in Newtons.
        length_mm : float
            Beam span in millimetres.
        width_mm : float
            Cross-section width in millimetres (b).
        thickness_mm : float
            Cross-section height / thickness in millimetres (h).

        Returns
        -------
        float
            Centre deflection in millimetres.
        """
        # Convert to SI (metres / Pascals)
        L: float = length_mm * 1e-3
        b: float = width_mm * 1e-3
        h: float = thickness_mm * 1e-3
        E: float = self.E_gpa * 1e9  # Pa

        I: float = (b * h ** 3) / 12.0          # m⁴
        delta_m: float = (force_n * L ** 3) / (48.0 * E * I)
        return delta_m * 1e3  # convert back to mm

    def compute_bending_stress(
        self,
        force_n: float,
        length_mm: float,
        thickness_mm: float,
    ) -> float:
        """
        Maximum bending stress at the mid-span of a simply-supported beam.

            M = F * L / 4
            sigma = M * c / I   where c = h / 2

        Parameters
        ----------
        force_n : float
            Applied force in Newtons.
        length_mm : float
            Beam span in millimetres.
        thickness_mm : float
            Cross-section thickness in millimetres (h).

        Returns
        -------
        float
            Maximum bending stress in MPa.
        """
        L: float = length_mm * 1e-3
        h: float = thickness_mm * 1e-3
        b: float = self.pcb_width_mm * 1e-3

        I: float = (b * h ** 3) / 12.0
        M: float = (force_n * L) / 4.0
        c: float = h / 2.0

        sigma_pa: float = (M * c) / I
        return sigma_pa * 1e-6  # Pa -> MPa

    def compute_air_pocket_probability(
        self,
        offset_mm: float,
        inlay_force_n: float,
    ) -> float:
        """
        Estimate probability of a sealing air pocket forming under the display.

        Driven by two factors:
          - Frame mis-alignment (offset_mm): large offset creates a pressure
            gradient across the gasket, trapping air on the recessed side.
          - Inlay force excess: force > 10% above nominal over-deflects the
            chassis, opening micro-gaps opposite the load point.

            offset_term = |offset_mm| * 20          (mm→% sensitivity)
            force_excess = max(0, (F - 1400) / 140) (fractional over-spec)
            non_uniformity = offset_term + force_excess * 15
            p_air = sigmoid(0.8 * non_uniformity - 3)

        Returns
        -------
        float
            Air-pocket probability in [0, 1].
        """
        offset_term: float = abs(offset_mm) * 20.0
        force_excess: float = max(0.0, (inlay_force_n - 1400.0) / 140.0)
        non_uniformity: float = offset_term + force_excess * 5.0
        return self._sigmoid(0.8 * non_uniformity - 3.0)

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, reading: PressReading) -> PressPrediction:
        """
        Run the full prediction pipeline for a single press cycle.

        Parameters
        ----------
        reading : PressReading
            Process measurements from the press station.

        Returns
        -------
        PressPrediction
            Predicted mechanical quantities and QC test outcomes.
        """
        # Bending stress from inlay force (used for air-pocket non-uniformity)
        bending_stress_mpa: float = self.compute_bending_stress(
            force_n=reading.inlay_force_n,
            length_mm=self.pcb_length_mm,
            thickness_mm=self.pcb_thickness_mm,
        )

        # Structural deflection of the PCB under inlay load (for reporting)
        deflection_mm: float = self.compute_deflection(
            force_n=reading.inlay_force_n,
            length_mm=self.pcb_length_mm,
            width_mm=self.pcb_width_mm,
            thickness_mm=self.pcb_thickness_mm,
        )

        # Gap clearance is governed by display press force via gasket compliance.
        # Higher-than-nominal force compresses the gasket more → smaller residual gap.
        force_delta_n: float = reading.display_force_n - self.nominal_press_force_n
        gap_clearance_mm: float = self.nominal_gap_mm - force_delta_n * self.gasket_compliance_mm_per_n

        air_pocket_prob: float = self.compute_air_pocket_probability(
            offset_mm=reading.frame_offset_mm,
            inlay_force_n=reading.inlay_force_n,
        )

        # --- Fingerprint test -------------------------------------------
        fingerprint_outcome: str = (
            "FAIL"
            if gap_clearance_mm < self._GAP_MIN_MM
            or gap_clearance_mm > self._GAP_MAX_MM
            else "PASS"
        )

        # --- IP68 vacuum test -------------------------------------------
        ip68_outcome: str = (
            "FAIL" if air_pocket_prob > self._AIR_POCKET_THRESHOLD else "PASS"
        )

        # --- Combined outcome -------------------------------------------
        combined_outcome: str = (
            "PASS"
            if fingerprint_outcome == "PASS" and ip68_outcome == "PASS"
            else "FAIL"
        )

        # --- Confidence -------------------------------------------------
        # Higher confidence when force is within ±10 % of nominal and
        # dwell time is sufficient.
        inlay_nominal: float = 1400.0
        force_within_spec: bool = (
            abs(reading.inlay_force_n - inlay_nominal) / inlay_nominal <= 0.10
        )
        dwell_sufficient: bool = reading.dwell_time_s >= 15.0
        confidence: float = (
            0.90
            if force_within_spec and dwell_sufficient
            else 0.70
        )

        # IPC-A-610G proxy metrics
        # §14.5.1 warp proxy: gap clearance deviation from nominal (fixture-aware)
        warp_proxy = abs(gap_clearance_mm - self.nominal_gap_mm)
        # §14.5.3 stress index: normalised relative to nominal press force bending
        # Normalise against inlay nominal (1400 N), not display press nominal
        inlay_nominal_stress = self.compute_bending_stress(
            1400.0, self.pcb_length_mm, self.pcb_thickness_mm
        )
        stress_index = bending_stress_mpa / inlay_nominal_stress if inlay_nominal_stress > 0 else 1.0

        return PressPrediction(
            deflection_mm=round(deflection_mm, 6),
            bending_stress_mpa=round(bending_stress_mpa, 4),
            gap_clearance_mm=round(gap_clearance_mm, 6),
            air_pocket_probability=round(air_pocket_prob, 6),
            fingerprint_outcome=fingerprint_outcome,
            ip68_outcome=ip68_outcome,
            combined_outcome=combined_outcome,
            confidence=confidence,
            warp_proxy_mm=round(warp_proxy, 6),
            stress_index=round(stress_index, 4),
        )

    # ------------------------------------------------------------------
    # Batch simulation
    # ------------------------------------------------------------------

    def simulate_line_batch(
        self, n: int = 50, seed: int = 42
    ) -> List[PressReading]:
        """
        Generate a batch of realistic press-line readings around nominal values.

        Distributions:
            inlay_force_n   ~ N(1400, 46.67)  [±140 N ≈ 3σ]
            display_force_n ~ N(1210, 30.0)
            dwell_time_s    ~ N(15.0, 0.5)
            frame_offset_mm ~ N(0.0,  0.05)

        Parameters
        ----------
        n : int
            Number of readings to generate.
        seed : int
            NumPy random seed for reproducibility.

        Returns
        -------
        list[PressReading]
        """
        rng = np.random.default_rng(seed)

        inlay_forces: np.ndarray = rng.normal(loc=1400.0, scale=46.67, size=n)
        display_forces: np.ndarray = rng.normal(loc=1210.0, scale=30.0, size=n)
        dwell_times: np.ndarray = rng.normal(loc=15.0, scale=0.5, size=n)
        offsets: np.ndarray = rng.normal(loc=0.0, scale=0.05, size=n)

        readings: List[PressReading] = [
            PressReading(
                inlay_force_n=float(inlay_forces[i]),
                display_force_n=float(display_forces[i]),
                dwell_time_s=float(dwell_times[i]),
                frame_offset_mm=float(offsets[i]),
                fixture_id="BE-45" if i % 2 == 0 else "PA-49",
            )
            for i in range(n)
        ]
        return readings


# ---------------------------------------------------------------------------
# Quick smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    node = PressNode()
    batch = node.simulate_line_batch(n=10, seed=0)
    for r in batch:
        p = node.predict(r)
        print(
            f"F={r.inlay_force_n:.1f} N | "
            f"delta={p.deflection_mm:.5f} mm | "
            f"gap={p.gap_clearance_mm:.5f} mm | "
            f"p_air={p.air_pocket_probability:.4f} | "
            f"FP={p.fingerprint_outcome} | "
            f"IP68={p.ip68_outcome} | "
            f"{p.combined_outcome}"
        )

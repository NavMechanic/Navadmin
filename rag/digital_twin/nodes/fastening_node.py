"""
Node A - Fastening Node (Screwing Depth vs. Torque vs. RF Performance)

Station: Station 22
Component: SA38F14673 top bracket
Predicted test: QC_LTE_BC01_RSSI_MCH_18300_ANT_CW

Physics:
    F_axial = T / (K * d)
        K = 0.20  (nut factor, dry steel)
        d = 0.0016 m  (M1.6 nominal diameter)

Contact resistance model (torque-proportional inverse power law):
    R_c = R_nominal * (T_nominal / T)^exponent
        R_nominal = 0.30 milliohms  (at nominal torque 0.45 Nm)
        T_nominal = 0.45 Nm
        exponent  = 2.5

    Rationale: F_axial from the nut-factor formula is reported for diagnostic
    context, but the dominant driver of contact resistance at the gold-plated
    antenna pin interface is torque directly (spring-back compliance means
    residual clamping force is torque-proportional, not linearly F = T/Kd).

Thresholds:
    R_c > 0.8 milliohm  -> RISK   (~0.30 Nm)
    R_c > 1.2 milliohm  -> FAIL   (~0.24 Nm)
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
class FasteningReading:
    torque_nm: float
    depth_mm: float
    cycle_time_s: float
    station_id: str = "S22"
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class FasteningPrediction:
    f_axial_n: float
    contact_resistance_mohm: float
    rf_mismatch_risk: bool
    test_outcome: str                          # "PASS" | "RISK" | "FAIL"
    confidence: float                          # 0.0 – 1.0
    predicted_rssi_delta_db: float
    test_code: str = "QC_LTE_BC01_RSSI_MCH_18300_ANT_CW"


# ---------------------------------------------------------------------------
# Node
# ---------------------------------------------------------------------------

class FasteningNode:
    """
    Digital-twin node for screw-fastening Station 22.

    Translates torque/depth process parameters into predicted RF contact
    resistance and LTE RSSI test outcome for component SA38F14673.
    """

    def __init__(self) -> None:
        self.nut_factor: float = 0.20            # K  (dimensionless, dry steel)
        self.screw_diameter_m: float = 0.0016    # d  (M1.6, metres)
        self.R_nominal: float = 0.30             # contact resistance at nominal torque (milliohms)
        self.T_nominal: float = 0.45             # nominal torque (Nm)
        self.exponent: float = 2.5               # power-law exponent for torque→resistance curve

        self.thresholds: dict[str, float] = {
            "risk": 0.8,   # milliohms
            "fail": 1.2,   # milliohms
        }

    # ------------------------------------------------------------------
    # Physics helpers
    # ------------------------------------------------------------------

    def compute_axial_force(self, torque_nm: float, depth_mm: float) -> float:
        """
        Compute bolt axial clamping force from applied torque.

        Parameters
        ----------
        torque_nm : float
            Applied torque in Newton-metres.
        depth_mm : float
            Screw engagement depth in millimetres (unused in basic model but
            retained for future depth-dependent K corrections).

        Returns
        -------
        float
            Axial force in Newtons, clamped to [0, 5000].
        """
        if self.nut_factor <= 0 or self.screw_diameter_m <= 0:
            raise ValueError("nut_factor and screw_diameter_m must be positive.")

        f_axial: float = torque_nm / (self.nut_factor * self.screw_diameter_m)
        return float(np.clip(f_axial, 0.0, 5000.0))

    def predict_contact_resistance(self, torque_nm: float) -> float:
        """
        Estimate contact resistance from applied torque (inverse power law).

        R_c = R_nominal * (T_nominal / torque)^exponent

        Parameters
        ----------
        torque_nm : float
            Applied torque in Newton-metres.

        Returns
        -------
        float
            Contact resistance in milliohms.
        """
        torque_nm = max(torque_nm, 1e-6)  # guard against zero/negative torque
        return self.R_nominal * (self.T_nominal / torque_nm) ** self.exponent

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, reading: FasteningReading) -> FasteningPrediction:
        """
        Run the full prediction pipeline for a single fastening cycle.

        Parameters
        ----------
        reading : FasteningReading
            Process measurements from Station 22.

        Returns
        -------
        FasteningPrediction
            Predicted physics quantities and RF test outcome.
        """
        f_axial: float = self.compute_axial_force(reading.torque_nm, reading.depth_mm)
        r_c: float = self.predict_contact_resistance(reading.torque_nm)

        # RSSI degradation (dB) — linear penalty above the risk threshold
        if r_c > self.thresholds["risk"]:
            rssi_delta: float = -0.5 * (r_c / self.thresholds["risk"])
        else:
            rssi_delta = 0.0

        # Confidence: higher when torque is within the nominal process window
        normal_torque_min: float = 0.3
        normal_torque_max: float = 0.7
        confidence: float = (
            0.85
            if normal_torque_min <= reading.torque_nm <= normal_torque_max
            else 0.65
        )

        # Outcome classification
        if r_c <= self.thresholds["risk"]:
            outcome = "PASS"
        elif r_c <= self.thresholds["fail"]:
            outcome = "RISK"
        else:
            outcome = "FAIL"

        rf_mismatch_risk: bool = r_c > self.thresholds["risk"]

        return FasteningPrediction(
            f_axial_n=round(f_axial, 4),
            contact_resistance_mohm=round(r_c, 6),
            rf_mismatch_risk=rf_mismatch_risk,
            test_outcome=outcome,
            confidence=confidence,
            predicted_rssi_delta_db=round(rssi_delta, 4),
        )

    # ------------------------------------------------------------------
    # Batch simulation
    # ------------------------------------------------------------------

    def simulate_station22_batch(
        self, n: int = 50, seed: int = 42
    ) -> List[FasteningReading]:
        """
        Generate a batch of realistic Station 22 fastening readings.

        Torque   ~ N(0.45, 0.06) Nm
        Depth    ~ N(3.2,  0.15) mm
        Cycle    ~ N(2.1,  0.3)  s

        Parameters
        ----------
        n : int
            Number of readings to generate.
        seed : int
            NumPy random seed for reproducibility.

        Returns
        -------
        list[FasteningReading]
        """
        rng = np.random.default_rng(seed)

        torques: np.ndarray = rng.normal(loc=0.45, scale=0.06, size=n)
        depths: np.ndarray = rng.normal(loc=3.2, scale=0.15, size=n)
        cycles: np.ndarray = rng.normal(loc=2.1, scale=0.3, size=n)

        readings: List[FasteningReading] = [
            FasteningReading(
                torque_nm=float(torques[i]),
                depth_mm=float(depths[i]),
                cycle_time_s=float(cycles[i]),
            )
            for i in range(n)
        ]
        return readings


# ---------------------------------------------------------------------------
# Quick smoke-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    node = FasteningNode()
    batch = node.simulate_station22_batch(n=10, seed=0)
    for r in batch:
        p = node.predict(r)
        print(
            f"torque={r.torque_nm:.3f} Nm | "
            f"F={p.f_axial_n:.1f} N | "
            f"Rc={p.contact_resistance_mohm:.4f} mΩ | "
            f"{p.test_outcome} | "
            f"ΔRSSI={p.predicted_rssi_delta_db:.3f} dB"
        )

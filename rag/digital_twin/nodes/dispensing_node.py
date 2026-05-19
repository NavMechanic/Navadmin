"""
Node B - Dispensing Node: Thermal Viscosity vs. Volume → Thermal Performance.

Uses a Physics-Informed Neural Network (PINN) implemented in pure numpy.
Models TMQ-100 thermal grease and liquid metal dispensing onto the SM8845 SoC.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List

import numpy as np


# ---------------------------------------------------------------------------
# Physics-Informed Neural Network
# ---------------------------------------------------------------------------

class ThermalPINN:
    """Numpy PINN that learns θ_JA correction from process deviations.

    Uses physics residual: residual = theta_predicted - theta_physics_base
    Initialized with seed=42 for reproducibility.
    """

    def __init__(self) -> None:
        rng = np.random.default_rng(42)
        self.W1: np.ndarray = rng.normal(0, 0.1, (4, 16))
        self.b1: np.ndarray = np.zeros(16)
        self.W2: np.ndarray = rng.normal(0, 0.1, (16, 8))
        self.b2: np.ndarray = np.zeros(8)
        self.W3: np.ndarray = rng.normal(0, 0.05, (8, 1))
        self.b3: np.ndarray = np.zeros(1)

    def forward(self, x: np.ndarray) -> float:
        h1 = np.tanh(x @ self.W1 + self.b1)
        h2 = np.tanh(h1 @ self.W2 + self.b2)
        out = h2 @ self.W3 + self.b3
        return float(np.clip(out.squeeze(), -0.3, 0.3))

    def correction(self, mass_tmq: float, mass_lm: float, humidity: float, pressure: float) -> float:
        """Returns fractional correction to θ_JA. Normalize by nominal values."""
        x = np.array([
            (mass_tmq - 352.0) / 20.0,
            (mass_lm - 45.0) / 15.0,
            (humidity - 45.0) / 15.0,
            (pressure - 3.0) / 0.5,
        ])
        return self.forward(x)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class DispensingReading:
    """Raw sensor / process inputs for a single dispense event."""
    mass_tmq_mg: float
    mass_lm_mg: float
    nozzle_pressure_bar: float
    humidity_pct: float
    line_temperature_c: float = 35.0
    timestamp: datetime = field(default_factory=datetime.utcnow)


@dataclass
class DispensingPrediction:
    """Thermal-path prediction produced by DispensingNode."""
    theta_JA_base_kw: float
    theta_JA_corrected_kw: float
    pinn_correction: float
    t_junction_c: float
    thermal_throttle_risk: bool
    thermal_margin_c: float
    test_outcome: str
    confidence: float
    test_code: str = "PAT2_THERMAL_THROTTLING"


# ---------------------------------------------------------------------------
# Dispensing Node
# ---------------------------------------------------------------------------

class DispensingNode:
    """Digital-twin node for the thermal-material dispensing station.

    Physical constants
    ------------------
    SM8845 SoC
        TDP        = 10 W
        Die area   = 8.5 cm² = 8.5e-4 m²

    TMQ-100 thermal grease
        k_tmq      = 3.5 W/mK
        density    = 2500 kg/m³

    Liquid metal
        k_lm       = 13.0 W/mK
        density    = 6440 kg/m³

    Throttle threshold: T_j > 85 °C
    """

    # SM8845 SoC constants
    TDP_W: float = 10.0

    # Compound stack thermal resistance [K/W] — measured at nominal process:
    #   junction-to-case (package): 0.50
    #   TIM1 bond line (nominal):   1.20   ← variable: TIM mass/quality drives this
    #   heatspreader:               0.30
    #   chassis-to-ambient:         1.50
    # Total nominal θ_JA = 3.50 K/W → T_j(nominal) = 35 + 10*3.5 = 70°C
    THETA_JA_NOMINAL_KW: float = 3.50
    THETA_JA_TMQ_NOMINAL: float = 1.20    # TIM1 portion at nominal 352 mg
    THETA_JA_LM_NOMINAL: float = 0.10     # LM layer portion at nominal 45 mg

    # TIM degradation sensitivities (per mg below nominal)
    THETA_SENSITIVITY_TMQ: float = 0.008  # K/W per mg deficit below 352 mg
    THETA_SENSITIVITY_LM: float = 0.020   # K/W per mg deficit below 45 mg

    # Humidity degradation (K/W per %RH above 60%)
    THETA_HUMIDITY_SLOPE: float = 0.015

    # Nominal dispense masses [mg]
    NOMINAL_MASS_TMQ_MG: float = 352.0
    NOMINAL_MASS_LM_MG: float = 45.0

    # Thermal throttle threshold [°C]
    THROTTLE_THRESHOLD_C: float = 85.0

    def __init__(self) -> None:
        self.pinn: ThermalPINN = ThermalPINN()

    # ------------------------------------------------------------------
    # Physics helpers
    # ------------------------------------------------------------------

    def compute_theta_base(self, mass_tmq_mg: float, mass_lm_mg: float) -> float:
        """Compute base θ_JA [K/W] from dispensed TIM masses.

        Uses an empirical compound stack model (nominal = 3.50 K/W).
        The TIM1 portion increases when dispensed mass is below nominal,
        modelling voids and incomplete coverage that raise thermal resistance.
        """
        # TIM1 (TMQ-100): deficit below nominal adds theta
        tmq_deficit: float = max(0.0, self.NOMINAL_MASS_TMQ_MG - mass_tmq_mg)
        theta_tmq: float = self.THETA_JA_TMQ_NOMINAL + tmq_deficit * self.THETA_SENSITIVITY_TMQ

        # Liquid metal layer: deficit raises theta faster (higher k impact)
        lm_deficit: float = max(0.0, self.NOMINAL_MASS_LM_MG - mass_lm_mg)
        theta_lm: float = self.THETA_JA_LM_NOMINAL + lm_deficit * self.THETA_SENSITIVITY_LM

        # Fixed stack (package + heatspreader + chassis-to-ambient)
        theta_fixed: float = self.THETA_JA_NOMINAL_KW - self.THETA_JA_TMQ_NOMINAL - self.THETA_JA_LM_NOMINAL

        return theta_fixed + theta_tmq + theta_lm

    # ------------------------------------------------------------------
    # Prediction
    # ------------------------------------------------------------------

    def predict(self, reading: DispensingReading) -> DispensingPrediction:
        """Run the physics + PINN model for a single DispensingReading.

        Steps
        -----
        1. Compute base θ_JA from dispensed masses.
        2. Get PINN fractional correction from process deviations.
        3. Apply humidity degradation to effective conductivity.
        4. Compute T_junction.
        5. Classify outcome.
        """
        # 1. Base thermal resistance (physics only)
        theta_base: float = self.compute_theta_base(
            reading.mass_tmq_mg, reading.mass_lm_mg
        )

        # 2. PINN fractional correction
        pinn_corr: float = self.pinn.correction(
            mass_tmq=reading.mass_tmq_mg,
            mass_lm=reading.mass_lm_mg,
            humidity=reading.humidity_pct,
            pressure=reading.nozzle_pressure_bar,
        )

        # 3. Apply PINN correction to θ_JA
        theta_corrected: float = theta_base * (1.0 + pinn_corr)

        # 4. Humidity correction: high humidity degrades TIM conductivity above 60%RH
        humidity_delta_theta: float = (
            self.THETA_HUMIDITY_SLOPE * max(0.0, reading.humidity_pct - 60.0)
        )

        # 5. Junction temperature [°C]
        t_junction: float = (
            reading.line_temperature_c
            + self.TDP_W * (theta_corrected + humidity_delta_theta)
        )

        # 6. Derived outputs
        thermal_margin: float = self.THROTTLE_THRESHOLD_C - t_junction
        throttle_risk: bool = t_junction > self.THROTTLE_THRESHOLD_C

        if t_junction < 80.0:
            outcome = "PASS"
        elif t_junction < 85.0:
            outcome = "RISK"
        else:
            outcome = "FAIL"

        return DispensingPrediction(
            theta_JA_base_kw=theta_base,
            theta_JA_corrected_kw=theta_corrected,
            pinn_correction=pinn_corr,
            t_junction_c=t_junction,
            thermal_throttle_risk=throttle_risk,
            thermal_margin_c=thermal_margin,
            test_outcome=outcome,
            confidence=0.80,  # PINN uncertainty acknowledged
        )

    # ------------------------------------------------------------------
    # Batch simulation
    # ------------------------------------------------------------------

    def simulate_dispenser_batch(
        self, n: int = 50, seed: int = 42
    ) -> List[DispensingReading]:
        """Simulate *n* dispense shots with Gaussian process variation.

        Noise model (clipped to physical limits):
            mass_tmq  ~ N(352, 20)   mg     clipped [280, 430]
            mass_lm   ~ N(45,  15)   mg     clipped [0,   90]
            pressure  ~ N(3.0, 0.5)  bar    clipped [1.5, 4.5]
            humidity  ~ N(45,  15)   %RH    clipped [5,   95]
        """
        rng = np.random.default_rng(seed)

        mass_tmq_samples: np.ndarray = rng.normal(352.0, 20.0, n).clip(280.0, 430.0)
        mass_lm_samples: np.ndarray = rng.normal(45.0, 15.0, n).clip(0.0, 90.0)
        pressure_samples: np.ndarray = rng.normal(3.0, 0.5, n).clip(1.5, 4.5)
        humidity_samples: np.ndarray = rng.normal(45.0, 15.0, n).clip(5.0, 95.0)
        temp_samples: np.ndarray = rng.normal(35.0, 2.0, n).clip(28.0, 45.0)

        results: List[DispensingReading] = []
        for i in range(n):
            results.append(DispensingReading(
                mass_tmq_mg=float(mass_tmq_samples[i]),
                mass_lm_mg=float(mass_lm_samples[i]),
                nozzle_pressure_bar=float(pressure_samples[i]),
                humidity_pct=float(humidity_samples[i]),
                line_temperature_c=float(temp_samples[i]),
            ))

        return results


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    node = DispensingNode()

    nominal = DispensingReading(
        mass_tmq_mg=352.0,
        mass_lm_mg=45.0,
        nozzle_pressure_bar=3.0,
        humidity_pct=45.0,
    )
    pred = node.predict(nominal)
    print("=== Nominal dispense prediction ===")
    print(f"  theta_JA base      : {pred.theta_JA_base_kw:.4f} K/W")
    print(f"  theta_JA corrected : {pred.theta_JA_corrected_kw:.4f} K/W")
    print(f"  PINN correction    : {pred.pinn_correction:+.4f}")
    print(f"  T_junction         : {pred.t_junction_c:.2f} C")
    print(f"  Thermal margin     : {pred.thermal_margin_c:.2f} C")
    print(f"  Outcome            : {pred.test_outcome}")
    print(f"  Test code          : {pred.test_code}")
    print(f"  Throttle risk      : {pred.thermal_throttle_risk}")

    batch = node.simulate_dispenser_batch(n=50, seed=42)
    outcomes = [p.test_outcome for p in batch]
    print("\n=== Batch (n=50) ===")
    for label in ("PASS", "RISK", "FAIL"):
        print(f"  {label}: {outcomes.count(label)}")

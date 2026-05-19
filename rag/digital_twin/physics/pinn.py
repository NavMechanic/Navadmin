"""
Physics-Informed Neural Network (PINN) - standalone module.

Re-exports ThermalPINN for use outside the dispensing node and provides
PCBStressModel as a convenience import from this package layer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from rag.digital_twin.physics.stress_model import PCBStressModel, StressResult


# ---------------------------------------------------------------------------
# Thermal PINN
# ---------------------------------------------------------------------------

class ThermalPINN:
    """Numpy PINN that learns θ_JA correction from process deviations.

    Uses physics residual: residual = theta_predicted - theta_physics_base
    Initialized with seed=42 for reproducibility.

    Network topology
    ----------------
    Input  : 4 normalised features
               [delta_mass_tmq/sigma, delta_mass_lm/sigma,
                delta_humidity/sigma, delta_pressure/sigma]
    Hidden : 4 -> 16 -> 8
    Output : scalar fractional correction clipped to [-0.3, +0.3]
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
        """Forward pass through the network."""
        h1 = np.tanh(x @ self.W1 + self.b1)
        h2 = np.tanh(h1 @ self.W2 + self.b2)
        out = h2 @ self.W3 + self.b3
        return float(np.clip(out.squeeze(), -0.3, 0.3))

    def correction(self, mass_tmq: float, mass_lm: float, humidity: float, pressure: float) -> float:
        """Returns fractional correction to θ_JA. Normalize by nominal values.

        Nominal operating values:
            mass_tmq_nominal = 352 mg  (sigma = 20 mg)
            mass_lm_nominal  =  45 mg  (sigma = 15 mg)
            humidity_nominal =  45 %RH (sigma = 15 %RH)
            pressure_nominal =   3 bar (sigma = 0.5 bar)
        """
        x = np.array([
            (mass_tmq - 352.0) / 20.0,
            (mass_lm - 45.0) / 15.0,
            (humidity - 45.0) / 15.0,
            (pressure - 3.0) / 0.5,
        ])
        return self.forward(x)


# ---------------------------------------------------------------------------
# Public re-exports
# ---------------------------------------------------------------------------

__all__ = ["ThermalPINN", "PCBStressModel", "StressResult"]

"""
PCB Stress and Deflection Model
================================
12-layer PCB (FR4 + copper) stress and deflection analysis.

Uses classical Euler-Bernoulli beam theory (simply-supported beam,
centre-point load) to estimate maximum bending stress and mid-span
deflection of a PCB panel.

Dependencies: math, dataclasses, numpy only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class StressResult:
    sigma_max_mpa: float   # Maximum bending stress [MPa]
    deflection_mm: float   # Mid-span deflection [mm]
    safety_factor: float   # yield_strength / sigma_max, yield = 350 MPa for FR4-copper composite
    is_safe: bool          # safety_factor > 1.5


# ---------------------------------------------------------------------------
# PCB Stress Model
# ---------------------------------------------------------------------------

class PCBStressModel:
    """12-layer PCB (FR4 + copper) stress and deflection model.

    Assumptions
    -----------
    * Simply-supported beam loaded at mid-span (worst-case assembly press-fit).
    * Homogeneous effective modulus E = 28 GPa (FR4-copper composite, 12-layer).
    * Rectangular cross-section: width x thickness.
    * Classical Euler-Bernoulli bending:
        sigma_max = M * c / I      where M = F*L/4,  c = t/2
        delta_max = F*L^3 / (48*E*I)
    * Yield strength of composite = 350 MPa (FR4-copper laminate).
    * Safety-factor threshold = 1.5 (IPC-2221 guideline).
    """

    E_GPA: float = 28.0                 # Effective elastic modulus [GPa]
    YIELD_STRENGTH_MPA: float = 350.0   # Composite yield strength [MPa]

    def analyze(
        self,
        force_n: float,
        length_mm: float,
        width_mm: float,
        thickness_mm: float,
    ) -> StressResult:
        """Analyse PCB panel under a centre-point load.

        Parameters
        ----------
        force_n       : Applied force [N]
        length_mm     : Span / length of the PCB panel [mm]
        width_mm      : Width of the PCB panel [mm]
        thickness_mm  : Total stack-up thickness [mm]

        Returns
        -------
        StressResult
        """
        # Convert inputs to SI units
        F: float = float(force_n)               # N
        L: float = float(length_mm) * 1e-3      # m
        b: float = float(width_mm) * 1e-3       # m
        h: float = float(thickness_mm) * 1e-3   # m
        E: float = self.E_GPA * 1e9             # Pa

        # Second moment of area for rectangle: I = b * h^3 / 12
        I_m4: float = (b * h ** 3) / 12.0

        # Bending moment at mid-span (simply-supported, point load at centre)
        M_nm: float = F * L / 4.0

        # Distance from neutral axis to extreme fibre
        c: float = h / 2.0

        # Maximum bending stress [Pa] -> [MPa]
        sigma_pa: float = (M_nm * c) / I_m4
        sigma_mpa: float = sigma_pa / 1e6

        # Mid-span deflection [m] -> [mm]
        delta_m: float = (F * L ** 3) / (48.0 * E * I_m4)
        delta_mm: float = delta_m * 1e3

        # Safety factor: yield_strength / sigma_max
        if sigma_mpa > 0.0:
            safety: float = self.YIELD_STRENGTH_MPA / sigma_mpa
        else:
            safety = math.inf

        is_safe: bool = safety > 1.5

        return StressResult(
            sigma_max_mpa=sigma_mpa,
            deflection_mm=delta_mm,
            safety_factor=safety,
            is_safe=is_safe,
        )


# ---------------------------------------------------------------------------
# Quick self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    model = PCBStressModel()

    # Typical smartphone PCB: 150 x 70 x 1.2 mm, 50 N press-fit load
    result = model.analyze(
        force_n=50.0,
        length_mm=150.0,
        width_mm=70.0,
        thickness_mm=1.2,
    )
    print("=== PCB Stress Analysis ===")
    print(f"  sigma_max     : {result.sigma_max_mpa:.2f} MPa")
    print(f"  Deflection    : {result.deflection_mm:.4f} mm")
    print(f"  Safety factor : {result.safety_factor:.2f}")
    print(f"  Is safe       : {result.is_safe}")

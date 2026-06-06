"""Digital twin physics models."""
from rag.digital_twin.physics.pinn import ThermalPINN
from rag.digital_twin.physics.stress_model import PCBStressModel
__all__ = ["ThermalPINN", "PCBStressModel"]

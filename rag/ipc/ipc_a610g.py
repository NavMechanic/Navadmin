"""IPC-A-610G Acceptability of Electronic Assemblies.

Maps Aviator assembly process stages to IPC-A-610G classification:
  Target (T) – ideal condition, meets all requirements
  Acceptable (A) – meets class 2 requirements, may not be ideal
  Process Indicator (PI) – minor concern, still within acceptance, watch closely
  Defect (D) – does not meet requirements, disposition required
"""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class IPCClass(Enum):
    CLASS_1 = 1  # General electronics
    CLASS_2 = 2  # Dedicated service electronics (smartphones)
    CLASS_3 = 3  # High reliability (medical, aerospace)


class IPCCondition(Enum):
    TARGET = "Target"
    ACCEPTABLE = "Acceptable"
    PROCESS_INDICATOR = "Process Indicator"
    DEFECT = "Defect"


@dataclass
class IPCCriteria:
    section: str          # e.g. "5.2.1" — IPC-A-610G section reference
    title: str            # Brief title
    description: str      # What is being measured
    ipc_class: IPCClass   # Class 2 for Aviator
    assembly_stage: str   # "S22_FASTENING", "S31_DISPENSING", "S42_PRESS"
    linked_node: str      # "A_FASTENING", "B_DISPENSING", "C_PRESS"
    metric: str           # Python-accessible attribute path from prediction
    target_max: float     # Upper bound for TARGET condition
    target_min: float     # Lower bound for TARGET condition
    acceptable_max: float
    acceptable_min: float
    pi_max: float         # Process Indicator upper bound
    pi_min: float
    defect_condition: str # Human-readable defect description
    units: str


@dataclass
class IPCInspectionResult:
    criteria_section: str
    criteria_title: str
    assembly_stage: str
    linked_node: str
    measured_value: float
    units: str
    condition: IPCCondition
    ipc_class: IPCClass
    note: str             # explanation of condition


class IPCInspector:
    """Inspects digital twin predictions against IPC-A-610G acceptance criteria.

    Usage:
        inspector = IPCInspector(ipc_class=IPCClass.CLASS_2)
        results = inspector.inspect(fastening_pred, dispensing_pred, press_pred)
        report = inspector.format_report(results)
    """

    def __init__(self, ipc_class: IPCClass = IPCClass.CLASS_2):
        self.ipc_class = ipc_class
        self.criteria = self._build_criteria()

    def _build_criteria(self) -> list[IPCCriteria]:
        """Build IPC-A-610G criteria table for all three Aviator assembly nodes."""
        return [
            # Node A — Fastening (IPC-A-610G Section 9 Terminal Connections)
            IPCCriteria(
                section="9.3.1",
                title="Antenna Pin Contact Resistance",
                description="Measured contact resistance at antenna pin terminal connection",
                ipc_class=IPCClass.CLASS_2,
                assembly_stage="S22_FASTENING",
                linked_node="A_FASTENING",
                metric="contact_resistance_mohm",
                target_max=0.3,
                target_min=0.0,
                acceptable_max=0.8,
                acceptable_min=0.0,
                pi_max=1.2,
                pi_min=0.0,
                defect_condition="R_c > 1.2 mΩ — RF path impedance mismatch",
                units="mΩ",
            ),
            IPCCriteria(
                section="9.3.2",
                title="Axial Clamping Force (M1.6)",
                description="Axial clamping force applied by M1.6 fastener during assembly",
                ipc_class=IPCClass.CLASS_2,
                assembly_stage="S22_FASTENING",
                linked_node="A_FASTENING",
                metric="f_axial_n",
                target_max=1500.0,
                target_min=1200.0,
                acceptable_max=1800.0,
                acceptable_min=900.0,
                pi_max=2200.0,
                pi_min=600.0,
                defect_condition="F < 600 N or F > 2200 N — fastener yield/stripping risk",
                units="N",
            ),
            IPCCriteria(
                section="9.3.3",
                title="RSSI Degradation",
                description="Received signal strength indicator delta due to fastening assembly",
                ipc_class=IPCClass.CLASS_2,
                assembly_stage="S22_FASTENING",
                linked_node="A_FASTENING",
                metric="predicted_rssi_delta_db",
                target_max=0.0,
                target_min=-0.5,
                acceptable_max=0.0,
                acceptable_min=-1.0,
                pi_max=0.0,
                pi_min=-1.5,
                defect_condition="RSSI delta < -1.5 dB — RF path performance degraded",
                units="dB",
            ),
            # Node B — Thermal Dispensing (IPC-A-610G Section 5.2 adjacent + IPC-7530)
            IPCCriteria(
                section="5.2.3",
                title="Junction Temperature under TDP",
                description="Component junction temperature under thermal design power load",
                ipc_class=IPCClass.CLASS_2,
                assembly_stage="S31_DISPENSING",
                linked_node="B_DISPENSING",
                metric="t_junction_c",
                target_max=70.0,
                target_min=0.0,
                acceptable_max=80.0,
                acceptable_min=0.0,
                pi_max=85.0,
                pi_min=0.0,
                defect_condition="T_j > 85°C — component thermal spec exceeded",
                units="°C",
            ),
            IPCCriteria(
                section="5.2.4",
                title="Thermal Resistance θ_JA",
                description="Junction-to-ambient thermal resistance after TIM dispensing",
                ipc_class=IPCClass.CLASS_2,
                assembly_stage="S31_DISPENSING",
                linked_node="B_DISPENSING",
                metric="theta_JA_corrected_kw",
                target_max=3.5,
                target_min=0.0,
                acceptable_max=4.0,
                acceptable_min=0.0,
                pi_max=4.5,
                pi_min=0.0,
                defect_condition="θ_JA > 4.5 K/W — TIM inadequate for heat dissipation",
                units="K/W",
            ),
            IPCCriteria(
                section="5.2.5",
                title="PINN Correction Magnitude",
                description="Physics-informed neural network correction factor magnitude",
                ipc_class=IPCClass.CLASS_2,
                assembly_stage="S31_DISPENSING",
                linked_node="B_DISPENSING",
                metric="pinn_correction",
                target_max=0.05,
                target_min=-0.05,
                acceptable_max=0.15,
                acceptable_min=-0.15,
                pi_max=0.25,
                pi_min=-0.25,
                defect_condition="|correction| > 0.25 — dispensing process severely out of spec",
                units="fraction",
            ),
            # Node C — Press/Seating (IPC-A-610G Section 8, 14 PCB Physical)
            IPCCriteria(
                section="8.3.1",
                title="Display Optical Stack Gap Clearance",
                description="Clearance gap in display optical stack assembly after pressing",
                ipc_class=IPCClass.CLASS_2,
                assembly_stage="S42_PRESS",
                linked_node="C_PRESS",
                metric="gap_clearance_mm",
                target_max=0.14,
                target_min=0.08,
                acceptable_max=0.18,
                acceptable_min=0.05,
                pi_max=0.20,
                pi_min=0.04,
                defect_condition="Gap outside 0.05–0.18 mm — fingerprint calibration failure",
                units="mm",
            ),
            IPCCriteria(
                section="14.5.1",
                title="PCB Bow/Warp Index",
                description="Gap clearance deviation from nominal — IPC-A-610G proxy for bow/twist in fixture-constrained press assembly",
                ipc_class=IPCClass.CLASS_2,
                assembly_stage="S42_PRESS",
                linked_node="C_PRESS",
                metric="warp_proxy_mm",
                target_max=0.03,
                target_min=0.0,
                acceptable_max=0.06,
                acceptable_min=0.0,
                pi_max=0.07,
                pi_min=0.0,
                defect_condition="|gap - nominal| > 0.07 mm — bow/twist beyond Class 2 seating tolerance",
                units="mm",
            ),
            IPCCriteria(
                section="14.5.2",
                title="Air Pocket Probability",
                description="Predicted probability of air pocket formation in gasket sealing zone",
                ipc_class=IPCClass.CLASS_2,
                assembly_stage="S42_PRESS",
                linked_node="C_PRESS",
                metric="air_pocket_probability",
                target_max=0.10,
                target_min=0.0,
                acceptable_max=0.25,
                acceptable_min=0.0,
                pi_max=0.35,
                pi_min=0.0,
                defect_condition="P_air > 0.35 — IP68 gasket sealing failure predicted",
                units="probability",
            ),
            IPCCriteria(
                section="14.5.3",
                title="Bending Stress Safety Index",
                description="Normalised bending stress relative to nominal press force (1.0 = nominal). Values > 1.15 indicate over-press risk.",
                ipc_class=IPCClass.CLASS_2,
                assembly_stage="S42_PRESS",
                linked_node="C_PRESS",
                metric="stress_index",
                target_max=1.05,
                target_min=0.90,
                acceptable_max=1.10,
                acceptable_min=0.85,
                pi_max=1.15,
                pi_min=0.80,
                defect_condition="Stress index > 1.15 or < 0.80 — press force deviation risks PCB delamination or under-seating",
                units="ratio",
            ),
        ]

    def inspect_value(self, value: float, criteria: IPCCriteria) -> IPCInspectionResult:
        """Classify a single measured value against IPC criteria."""
        section = criteria.section

        # RSSI delta (negative scale — lower is worse)
        if section == "9.3.3":
            if value >= criteria.target_min:
                condition = IPCCondition.TARGET
                note = (
                    f"RSSI delta {value} {criteria.units} >= target_min "
                    f"{criteria.target_min} {criteria.units}: Target condition"
                )
            elif value >= criteria.acceptable_min:
                condition = IPCCondition.ACCEPTABLE
                note = (
                    f"RSSI delta {value} {criteria.units} >= acceptable_min "
                    f"{criteria.acceptable_min} {criteria.units}: Acceptable condition"
                )
            elif value >= criteria.pi_min:
                condition = IPCCondition.PROCESS_INDICATOR
                note = (
                    f"RSSI delta {value} {criteria.units} >= pi_min "
                    f"{criteria.pi_min} {criteria.units}: Process Indicator — monitor closely"
                )
            else:
                condition = IPCCondition.DEFECT
                note = f"DEFECT: {criteria.defect_condition}"

        # Gap clearance (range check — must be within band)
        elif section == "8.3.1":
            if criteria.target_min <= value <= criteria.target_max:
                condition = IPCCondition.TARGET
                note = (
                    f"Gap {value} {criteria.units} within target range "
                    f"[{criteria.target_min}, {criteria.target_max}]: Target condition"
                )
            elif criteria.acceptable_min <= value <= criteria.acceptable_max:
                condition = IPCCondition.ACCEPTABLE
                note = (
                    f"Gap {value} {criteria.units} within acceptable range "
                    f"[{criteria.acceptable_min}, {criteria.acceptable_max}]: Acceptable condition"
                )
            elif criteria.pi_min <= value <= criteria.pi_max:
                condition = IPCCondition.PROCESS_INDICATOR
                note = (
                    f"Gap {value} {criteria.units} within PI range "
                    f"[{criteria.pi_min}, {criteria.pi_max}]: Process Indicator — monitor closely"
                )
            else:
                condition = IPCCondition.DEFECT
                note = f"DEFECT: {criteria.defect_condition}"

        # Axial clamping force (range check — must be within band)
        elif section == "9.3.2":
            if criteria.target_min <= value <= criteria.target_max:
                condition = IPCCondition.TARGET
                note = (
                    f"Force {value} {criteria.units} within target range "
                    f"[{criteria.target_min}, {criteria.target_max}]: Target condition"
                )
            elif criteria.acceptable_min <= value <= criteria.acceptable_max:
                condition = IPCCondition.ACCEPTABLE
                note = (
                    f"Force {value} {criteria.units} within acceptable range "
                    f"[{criteria.acceptable_min}, {criteria.acceptable_max}]: Acceptable condition"
                )
            elif criteria.pi_min <= value <= criteria.pi_max:
                condition = IPCCondition.PROCESS_INDICATOR
                note = (
                    f"Force {value} {criteria.units} within PI range "
                    f"[{criteria.pi_min}, {criteria.pi_max}]: Process Indicator — monitor closely"
                )
            else:
                condition = IPCCondition.DEFECT
                note = f"DEFECT: {criteria.defect_condition}"

        # PINN correction (symmetric range — absolute magnitude check)
        elif section == "5.2.5":
            if criteria.target_min <= value <= criteria.target_max:
                condition = IPCCondition.TARGET
                note = (
                    f"PINN correction {value} {criteria.units} within target range "
                    f"[{criteria.target_min}, {criteria.target_max}]: Target condition"
                )
            elif criteria.acceptable_min <= value <= criteria.acceptable_max:
                condition = IPCCondition.ACCEPTABLE
                note = (
                    f"PINN correction {value} {criteria.units} within acceptable range "
                    f"[{criteria.acceptable_min}, {criteria.acceptable_max}]: Acceptable condition"
                )
            elif criteria.pi_min <= value <= criteria.pi_max:
                condition = IPCCondition.PROCESS_INDICATOR
                note = (
                    f"PINN correction {value} {criteria.units} within PI range "
                    f"[{criteria.pi_min}, {criteria.pi_max}]: Process Indicator — monitor closely"
                )
            else:
                condition = IPCCondition.DEFECT
                note = f"DEFECT: {criteria.defect_condition}"

        # Normal metrics (value should be LOW — upper bound checks)
        else:
            if value <= criteria.target_max:
                condition = IPCCondition.TARGET
                note = (
                    f"{value} {criteria.units} <= target_max "
                    f"{criteria.target_max} {criteria.units}: Target condition"
                )
            elif value <= criteria.acceptable_max:
                condition = IPCCondition.ACCEPTABLE
                note = (
                    f"{value} {criteria.units} <= acceptable_max "
                    f"{criteria.acceptable_max} {criteria.units}: Acceptable condition"
                )
            elif value <= criteria.pi_max:
                condition = IPCCondition.PROCESS_INDICATOR
                note = (
                    f"{value} {criteria.units} <= pi_max "
                    f"{criteria.pi_max} {criteria.units}: Process Indicator — monitor closely"
                )
            else:
                condition = IPCCondition.DEFECT
                note = f"DEFECT: {criteria.defect_condition}"

        return IPCInspectionResult(
            criteria_section=criteria.section,
            criteria_title=criteria.title,
            assembly_stage=criteria.assembly_stage,
            linked_node=criteria.linked_node,
            measured_value=value,
            units=criteria.units,
            condition=condition,
            ipc_class=criteria.ipc_class,
            note=note,
        )

    def inspect(self, fastening_pred: Any, dispensing_pred: Any, press_pred: Any) -> list[IPCInspectionResult]:
        """Run all IPC-A-610G checks across the three assembly nodes and return results."""
        node_map = {
            "A_FASTENING": fastening_pred,
            "B_DISPENSING": dispensing_pred,
            "C_PRESS": press_pred,
        }

        results: list[IPCInspectionResult] = []
        for criteria in self.criteria:
            pred_obj = node_map[criteria.linked_node]
            value = getattr(pred_obj, criteria.metric)
            result = self.inspect_value(value, criteria)
            results.append(result)

        return results

    def format_report(self, results: list[IPCInspectionResult]) -> str:
        """Format a text report of all inspection results."""
        _severity_order = {
            IPCCondition.DEFECT: 0,
            IPCCondition.PROCESS_INDICATOR: 1,
            IPCCondition.ACCEPTABLE: 2,
            IPCCondition.TARGET: 3,
        }
        sorted_results = sorted(results, key=lambda r: _severity_order[r.condition])

        col_section = 9
        col_stage = 16
        col_title = 38
        col_value = 14
        col_condition = 17

        header = (
            f"{'Section':<{col_section}} "
            f"{'Stage':<{col_stage}} "
            f"{'Title':<{col_title}} "
            f"{'Value':<{col_value}} "
            f"{'Condition':<{col_condition}}"
        )
        separator = "-" * len(header)

        lines = [
            "IPC-A-610G Inspection Report",
            "=" * len(header),
            header,
            separator,
        ]

        for r in sorted_results:
            value_str = f"{r.measured_value:.4g} {r.units}"
            lines.append(
                f"{r.criteria_section:<{col_section}} "
                f"{r.assembly_stage:<{col_stage}} "
                f"{r.criteria_title:<{col_title}} "
                f"{value_str:<{col_value}} "
                f"{r.condition.value:<{col_condition}}"
            )

        lines.append(separator)

        defects = self.defect_count(results)
        pis = self.process_indicator_count(results)
        acceptables = sum(1 for r in results if r.condition == IPCCondition.ACCEPTABLE)
        targets = sum(1 for r in results if r.condition == IPCCondition.TARGET)

        lines.append(
            f"Summary: {len(results)} checks — "
            f"Target: {targets}  Acceptable: {acceptables}  "
            f"Process Indicator: {pis}  Defect: {defects}"
        )

        return "\n".join(lines)

    def defect_count(self, results: list[IPCInspectionResult]) -> int:
        """Count Defect-condition items."""
        return sum(1 for r in results if r.condition == IPCCondition.DEFECT)

    def process_indicator_count(self, results: list[IPCInspectionResult]) -> int:
        """Count Process Indicator-condition items."""
        return sum(1 for r in results if r.condition == IPCCondition.PROCESS_INDICATOR)

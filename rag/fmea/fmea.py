"""FMEA (Failure Mode and Effects Analysis) module for manufacturing quality.

Integrates PFMEA (Process) and DFMEA (Design) with the digital twin,
linking failure modes to nodes, predicted tests, and remedial actions.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import csv
import json


@dataclass
class FMEAItem:
    """Generic FMEA item (base for PFMEA and DFMEA)."""
    item_id: str
    failure_mode: str
    effects: str
    causes: str  # semicolon-separated list
    severity: int       # 1-10
    occurrence: int     # 1-10
    detection: int      # 1-10
    current_controls: str
    recommended_actions: str
    responsibility: str
    linked_node: str    # "A_FASTENING" | "B_DISPENSING" | "C_PRESS"
    linked_tests: str   # semicolon-separated test codes
    created_date: str = field(default_factory=lambda: datetime.utcnow().isoformat())
    status: str = "Open"  # Open | In Progress | Mitigated | Closed

    @property
    def rpn(self) -> int:
        """Risk Priority Number = S × O × D."""
        return self.severity * self.occurrence * self.detection

    @property
    def test_list(self) -> list[str]:
        """Parse semicolon-separated test codes."""
        return [t.strip() for t in self.linked_tests.split(";") if t.strip()]

    @property
    def cause_list(self) -> list[str]:
        """Parse semicolon-separated causes."""
        return [c.strip() for c in self.causes.split(";") if c.strip()]


@dataclass
class PFMEAItem(FMEAItem):
    """Process FMEA — focused on manufacturing process steps."""
    process_step: str = ""
    station_id: str = ""


@dataclass
class DFMEAItem(FMEAItem):
    """Design FMEA — focused on component design and integration."""
    component_name: str = ""
    design_function: str = ""


class FMEAAssessment:
    """Assessment engine that matches digital twin predictions to FMEA items."""

    def __init__(self, pfmea_items: list[FMEAItem] = None, dfmea_items: list[FMEAItem] = None):
        self.pfmea = pfmea_items or []
        self.dfmea = dfmea_items or []
        self.all_items = self.pfmea + self.dfmea

    def triggered_items(self, flagged_tests: list[str], risky_nodes: list[str] = None) -> list[FMEAItem]:
        """Return FMEA items triggered by predicted test failures and risky nodes.

        Parameters
        ----------
        flagged_tests : list[str]
            Predicted failing test codes from digital twin.
        risky_nodes : list[str], optional
            Nodes with RISK or FAIL outcomes ("A_FASTENING", etc.)

        Returns
        -------
        list[FMEAItem]
            FMEA items matched to the failures, sorted by RPN descending.
        """
        risky_nodes = risky_nodes or []
        triggered_dict = {}

        for item in self.all_items:
            # Match by test code
            if any(t in flagged_tests for t in item.test_list):
                triggered_dict[item.item_id] = item
                continue
            # Match by node
            if item.linked_node in risky_nodes:
                triggered_dict[item.item_id] = item

        # Remove duplicates and sort by RPN (risk)
        triggered = list(triggered_dict.values())
        return sorted(triggered, key=lambda x: -x.rpn)

    def high_rpn_items(self, threshold: int = 100) -> list[FMEAItem]:
        """Return all FMEA items with RPN >= threshold, sorted descending.

        Parameters
        ----------
        threshold : int
            RPN cutoff (default 100, typical manufacturing threshold).

        Returns
        -------
        list[FMEAItem]
        """
        high = [item for item in self.all_items if item.rpn >= threshold]
        return sorted(high, key=lambda x: -x.rpn)

    def to_report(self, title: str = "FMEA Assessment Report") -> str:
        """Generate a text report of all FMEA items and their status."""
        lines = [
            "=" * 80,
            title,
            "=" * 80,
            f"Total PFMEA items: {len(self.pfmea)}",
            f"Total DFMEA items: {len(self.dfmea)}",
            "",
        ]

        high = self.high_rpn_items()
        if high:
            lines.append(f"HIGH-RISK ITEMS (RPN >= 100): {len(high)}")
            lines.append("-" * 80)
            for item in high[:10]:  # Top 10
                lines.append(
                    f"  [{item.item_id}] {item.failure_mode}\n"
                    f"    RPN = {item.severity}×{item.occurrence}×{item.detection} = {item.rpn}\n"
                    f"    Effects: {item.effects}\n"
                    f"    Action: {item.recommended_actions}\n"
                )
        else:
            lines.append("No high-risk FMEA items detected.")

        lines.append("")
        return "\n".join(lines)


def load_pfmea_csv(csv_path: str) -> list[PFMEAItem]:
    """Load Process FMEA from CSV.

    Expected columns: item_id, process_step, station_id, failure_mode, effects,
    causes, severity, occurrence, detection, current_controls, recommended_actions,
    responsibility, linked_node, linked_tests, status
    """
    items = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            item = PFMEAItem(
                item_id=row["item_id"],
                process_step=row.get("process_step", ""),
                station_id=row.get("station_id", ""),
                failure_mode=row["failure_mode"],
                effects=row["effects"],
                causes=row["causes"],
                severity=int(row["severity"]),
                occurrence=int(row["occurrence"]),
                detection=int(row["detection"]),
                current_controls=row["current_controls"],
                recommended_actions=row["recommended_actions"],
                responsibility=row["responsibility"],
                linked_node=row["linked_node"],
                linked_tests=row["linked_tests"],
                status=row.get("status", "Open"),
            )
            items.append(item)
    return items


def load_dfmea_csv(csv_path: str) -> list[DFMEAItem]:
    """Load Design FMEA from CSV.

    Expected columns: item_id, component_name, design_function, failure_mode,
    effects, causes, severity, occurrence, detection, current_controls,
    recommended_actions, responsibility, linked_node, linked_tests, status
    """
    items = []
    with open(csv_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            item = DFMEAItem(
                item_id=row["item_id"],
                component_name=row.get("component_name", ""),
                design_function=row.get("design_function", ""),
                failure_mode=row["failure_mode"],
                effects=row["effects"],
                causes=row["causes"],
                severity=int(row["severity"]),
                occurrence=int(row["occurrence"]),
                detection=int(row["detection"]),
                current_controls=row["current_controls"],
                recommended_actions=row["recommended_actions"],
                responsibility=row["responsibility"],
                linked_node=row["linked_node"],
                linked_tests=row["linked_tests"],
                status=row.get("status", "Open"),
            )
            items.append(item)
    return items

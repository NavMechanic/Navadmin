"""Digital Twin orchestrator — fuses Node A/B/C predictions with RAG context."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Iterable, Optional, TYPE_CHECKING

import numpy as np

from rag.digital_twin.nodes.fastening_node import FasteningNode, FasteningReading, FasteningPrediction
from rag.digital_twin.nodes.dispensing_node import DispensingNode, DispensingReading, DispensingPrediction
from rag.digital_twin.nodes.press_node import PressNode, PressReading, PressPrediction

if TYPE_CHECKING:
    from rag.pipeline import RAGPipeline
    from rag.fmea.fmea import FMEAAssessment
    from rag.ipc.ipc_a610g import IPCInspector, IPCInspectionResult
    from rag.dq_gateway.telemetry import TelemetryGate, TelemetryVerdict


@dataclass
class TwinSnapshot:
    """A complete point-in-time snapshot of all three assembly nodes."""
    snapshot_id: str
    timestamp: datetime
    fastening: FasteningPrediction
    dispensing: DispensingPrediction
    press: PressPrediction
    overall_risk: str          # "PASS" / "RISK" / "FAIL"
    risk_score: float          # 0.0–1.0
    flagged_tests: list[str]   # predicted failing test codes
    rag_context: Optional[str] = None
    triggered_fmea_ids: list[str] = field(default_factory=list)
    triggered_fmea_rpn: list[int] = field(default_factory=list)
    ipc_defects: int = 0                            # IPC-A-610G Defect count
    ipc_process_indicators: int = 0                 # IPC-A-610G PI count
    ipc_results: list = field(default_factory=list) # list[IPCInspectionResult]
    # DQ10: provenance of the readings behind this snapshot. Only REAL snapshots are
    # field evidence; SYNTHETIC ones (simulator / batch) never are.
    origin_type: str = "SYNTHETIC"
    dq_event_ids: list[str] = field(default_factory=list)  # telemetry events that admitted the inputs

    @property
    def is_field_evidence(self) -> bool:
        return self.origin_type == "REAL"

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
        # IPCInspectionResult objects are not auto-serialisable — convert to strings
        d["ipc_results"] = [
            {"section": r.criteria_section, "title": r.criteria_title,
             "stage": r.assembly_stage, "value": r.measured_value,
             "units": r.units, "condition": r.condition.value}
            for r in self.ipc_results
        ] if self.ipc_results else []
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)


@dataclass
class TwinConfig:
    """Weights and thresholds for overall risk scoring."""
    fastening_weight: float = 0.35
    dispensing_weight: float = 0.35
    press_weight: float = 0.30
    risk_threshold: float = 0.40   # above → RISK
    fail_threshold: float = 0.70   # above → FAIL


_OUTCOME_SCORE = {"PASS": 0.0, "RISK": 0.5, "FAIL": 1.0}
_CYCLE_NODES = ("A_FASTENING", "B_DISPENSING", "C_PRESS")


def assemble_cycles(messages: Iterable, pending: dict, max_pending: int = 50) -> list[list]:
    """Group streamed PLC messages into A/B/C cycles keyed by ``unit_id``.

    Complete cycles are returned; partial ones wait in ``pending`` (caller-owned, persists
    across polls). A node repeating for the same unit, or the pending buffer overflowing,
    flushes the partial group as its own cycle so the gate rejects it as incomplete instead
    of merging readings from different units.
    """
    cycles: list[list] = []
    for m in messages:
        key, node = getattr(m, "unit_id", None), getattr(m, "node", "?")
        group = pending.setdefault(key, {})
        if node in group:
            cycles.append(list(group.values()))
            group = pending[key] = {}
        group[node] = m
        if all(n in group for n in _CYCLE_NODES):
            cycles.append(list(pending.pop(key).values()))
    while len(pending) > max_pending:
        cycles.append(list(pending.pop(next(iter(pending))).values()))
    return cycles


class DigitalTwin:
    """
    Combines all three manufacturing nodes into a unified transfer function:

        f(Process Inputs) → Predicted Functional Outputs + RAG context

    Optionally links to the RAG pipeline to pull relevant design/process docs
    alongside each prediction, enabling contextual RCA.

    Can also assess PFMEA and DFMEA checklists against predicted failures.
    """

    def __init__(
        self,
        config: TwinConfig = None,
        rag_pipeline: Optional["RAGPipeline"] = None,
        fmea_assessment: Optional["FMEAAssessment"] = None,
        ipc_inspector: Optional["IPCInspector"] = None,
    ) -> None:
        self.config = config or TwinConfig()
        self.node_a = FasteningNode()
        self.node_b = DispensingNode()
        self.node_c = PressNode()
        self._rag = rag_pipeline
        self._fmea = fmea_assessment
        self._ipc = ipc_inspector
        self._history: list[TwinSnapshot] = []
        self._ipc_history: list[list] = []  # parallel list of IPC results per snapshot
        self.telemetry_rejections: list[dict] = []  # cycles refused by the DQ telemetry gate

    # ------------------------------------------------------------------
    # Core prediction
    # ------------------------------------------------------------------

    def predict(
        self,
        fastening: FasteningReading,
        dispensing: DispensingReading,
        press: PressReading,
        query_rag: bool = True,
        origin_type: str = "SYNTHETIC",
        dq_event_ids: Optional[list[str]] = None,
    ) -> TwinSnapshot:
        """Run all three nodes and produce a fused snapshot.

        Direct calls are treated as SYNTHETIC (what-if / batch). Line telemetry must
        enter through :meth:`ingest_plc_cycle`, which applies the DQ gate first and is
        the only path that can mark a snapshot REAL.
        """
        if origin_type not in ("REAL", "SYNTHETIC", "DERIVED"):
            raise ValueError(f"origin_type must be REAL|SYNTHETIC|DERIVED, got {origin_type!r}")
        pred_a = self.node_a.predict(fastening)
        pred_b = self.node_b.predict(dispensing)
        pred_c = self.node_c.predict(press)

        risk_score = self._compute_risk_score(pred_a, pred_b, pred_c)
        overall = self._classify_overall(risk_score)
        flagged = self._collect_flagged_tests(pred_a, pred_b, pred_c)

        rag_context = None
        if query_rag and self._rag is not None:
            rag_context = self._fetch_rag_context(pred_a, pred_b, pred_c, flagged)

        # Assess FMEA items if available
        triggered_fmea_ids = []
        triggered_fmea_rpn = []
        if self._fmea is not None:
            risky_nodes = [n for n in ("A_FASTENING", "B_DISPENSING", "C_PRESS")
                          if (n == "A_FASTENING" and pred_a.test_outcome in ("RISK", "FAIL")) or
                             (n == "B_DISPENSING" and pred_b.test_outcome in ("RISK", "FAIL")) or
                             (n == "C_PRESS" and pred_c.combined_outcome in ("RISK", "FAIL"))]
            triggered = self._fmea.triggered_items(flagged, risky_nodes)
            triggered_fmea_ids = [item.item_id for item in triggered]
            triggered_fmea_rpn = [item.rpn for item in triggered]

        # IPC-A-610G inspection
        ipc_results = []
        ipc_defects = 0
        ipc_pis = 0
        if self._ipc is not None:
            ipc_results = self._ipc.inspect(pred_a, pred_b, pred_c)
            ipc_defects = self._ipc.defect_count(ipc_results)
            ipc_pis = self._ipc.process_indicator_count(ipc_results)
            self._ipc_history.append(ipc_results)

        snap = TwinSnapshot(
            snapshot_id=f"SNAP_{datetime.utcnow().strftime('%Y%m%d_%H%M%S_%f')}",
            timestamp=datetime.utcnow(),
            fastening=pred_a,
            dispensing=pred_b,
            press=pred_c,
            overall_risk=overall,
            risk_score=round(risk_score, 4),
            flagged_tests=flagged,
            rag_context=rag_context,
            triggered_fmea_ids=triggered_fmea_ids,
            triggered_fmea_rpn=triggered_fmea_rpn,
            ipc_defects=ipc_defects,
            ipc_process_indicators=ipc_pis,
            ipc_results=ipc_results,
            origin_type=origin_type,
            dq_event_ids=list(dq_event_ids or []),
        )
        self._history.append(snap)
        return snap

    # ------------------------------------------------------------------
    # DQ-gated telemetry path (DQ10 / DQ25 before any node or physics model)
    # ------------------------------------------------------------------

    def ingest_plc_cycle(
        self,
        messages: Iterable,
        gate: "TelemetryGate",
        query_rag: bool = False,
    ) -> Optional[TwinSnapshot]:
        """Gate one assembly cycle (A/B/C PLC messages) and predict only if all pass.

        Every message goes through ``gate.check`` (DQ01/03/04/09/10/25). If any is
        rejected, or a node is missing from the cycle, the twin state is not touched
        and the rejection is recorded. Readings are never back-filled with nominal
        values. The snapshot is REAL only if every admitted message was REAL.
        """
        by_node: dict = {}
        verdicts: list["TelemetryVerdict"] = []
        for msg in messages:
            verdicts.append(gate.check(msg))
            by_node[getattr(msg, "node", "?")] = msg
        rejected = [v for v in verdicts if not v.admitted]
        missing = [n for n in ("A_FASTENING", "B_DISPENSING", "C_PRESS") if n not in by_node]
        if rejected or missing:
            self.telemetry_rejections.append({
                "at": datetime.utcnow().isoformat(),
                "missing_nodes": missing,
                "rejected": [{"node": v.node, "station": v.station_id, "rules": v.failed_rules,
                              "messages": v.messages} for v in rejected],
            })
            return None

        a, b, c = by_node["A_FASTENING"], by_node["B_DISPENSING"], by_node["C_PRESS"]
        fastening = FasteningReading(torque_nm=a.torque_nm, depth_mm=a.depth_mm,
                                     cycle_time_s=a.cycle_time_s, station_id=a.station_id)
        dispensing = DispensingReading(mass_tmq_mg=b.mass_tmq_mg, mass_lm_mg=b.mass_lm_mg,
                                       nozzle_pressure_bar=b.nozzle_pressure_bar,
                                       humidity_pct=b.humidity_pct)
        press = PressReading(inlay_force_n=c.inlay_force_n, display_force_n=c.display_force_n,
                             dwell_time_s=c.dwell_time_s, frame_offset_mm=c.frame_offset_mm,
                             fixture_id=c.fixture_id)
        origins = {v.origin_type for v in verdicts}
        origin = "REAL" if origins == {"REAL"} else ("SYNTHETIC" if "SYNTHETIC" in origins else "DERIVED")
        return self.predict(fastening, dispensing, press, query_rag=query_rag,
                            origin_type=origin, dq_event_ids=[v.event_id for v in verdicts])

    def field_evidence_history(self) -> list[TwinSnapshot]:
        """Snapshots that may be used as field evidence or retraining data (REAL only, DQ10)."""
        return [s for s in self._history if s.is_field_evidence]

    def ipc_variability_history(self) -> list[list]:
        """IPC results per snapshot, for variability heatmap."""
        return self._ipc_history

    # ------------------------------------------------------------------
    # Batch simulation (no live PLC needed)
    # ------------------------------------------------------------------

    def run_batch_simulation(self, n: int = 50, seed: int = 42) -> list[TwinSnapshot]:
        """Simulate n assembly cycles and return snapshot history."""
        rng = np.random.default_rng(seed)

        a_readings = self.node_a.simulate_station22_batch(n=n, seed=seed)
        b_readings = self.node_b.simulate_dispenser_batch(n=n, seed=seed)
        c_readings = self.node_c.simulate_line_batch(n=n, seed=seed)

        snapshots = []
        for ra, rb, rc in zip(a_readings, b_readings, c_readings):
            snap = self.predict(ra, rb, rc, query_rag=False)
            snapshots.append(snap)
        return snapshots

    # ------------------------------------------------------------------
    # History & analytics
    # ------------------------------------------------------------------

    def yield_rate(self) -> float:
        """Fraction of PASS snapshots in history."""
        if not self._history:
            return 0.0
        passed = sum(1 for s in self._history if s.overall_risk == "PASS")
        return passed / len(self._history)

    def risk_trend(self, last_n: int = 20) -> list[float]:
        """Risk scores for the last N snapshots."""
        return [s.risk_score for s in self._history[-last_n:]]

    def top_failure_modes(self) -> dict[str, int]:
        """Count of each flagged test code across history."""
        counts: dict[str, int] = {}
        for snap in self._history:
            for code in snap.flagged_tests:
                counts[code] = counts.get(code, 0) + 1
        return dict(sorted(counts.items(), key=lambda x: -x[1]))

    # ------------------------------------------------------------------
    # RAG integration
    # ------------------------------------------------------------------

    def query_with_context(self, question: str) -> str:
        """Pass a question to the RAG pipeline and return the answer."""
        if self._rag is None:
            return "RAG pipeline not connected. Initialise DigitalTwin with rag_pipeline=RAGPipeline()."
        result = self._rag.query(question)
        return result.answer

    # ------------------------------------------------------------------
    # FMEA integration
    # ------------------------------------------------------------------

    def fmea_report(self) -> str:
        """Generate FMEA assessment report based on all snapshots in history."""
        if self._fmea is None:
            return "FMEA assessment not initialised."

        # Collect all triggered FMEA items from history
        all_triggered = {}
        for snap in self._history:
            for fid, rpn in zip(snap.triggered_fmea_ids, snap.triggered_fmea_rpn):
                if fid not in all_triggered:
                    all_triggered[fid] = rpn

        if not all_triggered:
            return "No FMEA items triggered across all snapshots."

        # Look up items
        item_map = {item.item_id: item for item in self._fmea.all_items}
        triggered_items = [item_map[fid] for fid in all_triggered if fid in item_map]
        triggered_items.sort(key=lambda x: -x.rpn)

        lines = [
            "=" * 80,
            "FMEA Risk Assessment Report",
            "=" * 80,
            f"Cycles analyzed: {len(self._history)}",
            f"High-risk FMEA items triggered: {len(triggered_items)}",
            "",
        ]

        for item in triggered_items[:20]:  # Top 20
            lines.append(f"[{item.item_id}] {item.failure_mode}")
            lines.append(f"  RPN: {item.severity}×{item.occurrence}×{item.detection} = {item.rpn}")
            lines.append(f"  Effects: {item.effects}")
            lines.append(f"  Recommended action: {item.recommended_actions}")
            lines.append(f"  Owner: {item.responsibility}")
            lines.append("")

        return "\n".join(lines)

    def high_risk_fmea(self, rpn_threshold: int = 100) -> list:
        """Return high-RPN FMEA items from the assessment."""
        if self._fmea is None:
            return []
        return self._fmea.high_rpn_items(threshold=rpn_threshold)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _compute_risk_score(
        self,
        a: FasteningPrediction,
        b: DispensingPrediction,
        c: PressPrediction,
    ) -> float:
        cfg = self.config
        score_a = _OUTCOME_SCORE.get(a.test_outcome, 0.5)
        score_b = _OUTCOME_SCORE.get(b.test_outcome, 0.5)
        score_c = _OUTCOME_SCORE.get(c.combined_outcome, 0.5)
        weighted = (
            cfg.fastening_weight * score_a
            + cfg.dispensing_weight * score_b
            + cfg.press_weight * score_c
        )
        # Any single FAIL must surface at least as RISK overall
        max_score = max(score_a, score_b, score_c)
        if max_score >= 1.0:
            return max(weighted, self.config.risk_threshold)
        return weighted

    def _classify_overall(self, risk_score: float) -> str:
        if risk_score >= self.config.fail_threshold:
            return "FAIL"
        if risk_score >= self.config.risk_threshold:
            return "RISK"
        return "PASS"

    def _collect_flagged_tests(
        self,
        a: FasteningPrediction,
        b: DispensingPrediction,
        c: PressPrediction,
    ) -> list[str]:
        flagged = []
        if a.test_outcome in ("RISK", "FAIL"):
            flagged.append(a.test_code)
        if b.test_outcome in ("RISK", "FAIL"):
            flagged.append(b.test_code)
        if c.fingerprint_outcome in ("RISK", "FAIL"):
            flagged.append("QC_ANDROID_FINGERPRINT_TEST_RESULT")
        if c.ip68_outcome in ("RISK", "FAIL"):
            flagged.append("IP68_VACUUM_LEAK_TEST")
        return flagged

    def _fetch_rag_context(
        self,
        a: FasteningPrediction,
        b: DispensingPrediction,
        c: PressPrediction,
        flagged_tests: list[str],
    ) -> str:
        if self._rag is None:
            return ""
        test_list = ", ".join(flagged_tests) if flagged_tests else "no failures predicted"
        question = (
            f"What are the root causes and corrective actions for the following predicted test failures: "
            f"{test_list}? "
            f"Node A contact resistance: {a.contact_resistance_mohm:.3f} mohm. "
            f"Node B junction temperature: {b.t_junction_c:.1f} °C. "
            f"Node C gap clearance: {c.gap_clearance_mm:.3f} mm."
        )
        try:
            result = self._rag.query(question)
            return result.answer
        except Exception:
            return ""

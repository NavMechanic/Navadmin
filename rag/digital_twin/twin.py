"""Digital Twin orchestrator — fuses Node A/B/C predictions with RAG context."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional, TYPE_CHECKING

import numpy as np

from rag.digital_twin.nodes.fastening_node import FasteningNode, FasteningReading, FasteningPrediction
from rag.digital_twin.nodes.dispensing_node import DispensingNode, DispensingReading, DispensingPrediction
from rag.digital_twin.nodes.press_node import PressNode, PressReading, PressPrediction

if TYPE_CHECKING:
    from rag.pipeline import RAGPipeline


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
    rag_context: Optional[str] = None   # relevant doc snippets from RAG

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.isoformat()
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


class DigitalTwin:
    """
    Combines all three manufacturing nodes into a unified transfer function:

        f(Process Inputs) → Predicted Functional Outputs + RAG context

    Optionally links to the RAG pipeline to pull relevant design/process docs
    alongside each prediction, enabling contextual RCA.
    """

    def __init__(
        self,
        config: TwinConfig = None,
        rag_pipeline: Optional["RAGPipeline"] = None,
    ) -> None:
        self.config = config or TwinConfig()
        self.node_a = FasteningNode()
        self.node_b = DispensingNode()
        self.node_c = PressNode()
        self._rag = rag_pipeline
        self._history: list[TwinSnapshot] = []

    # ------------------------------------------------------------------
    # Core prediction
    # ------------------------------------------------------------------

    def predict(
        self,
        fastening: FasteningReading,
        dispensing: DispensingReading,
        press: PressReading,
        query_rag: bool = True,
    ) -> TwinSnapshot:
        """Run all three nodes and produce a fused snapshot."""
        pred_a = self.node_a.predict(fastening)
        pred_b = self.node_b.predict(dispensing)
        pred_c = self.node_c.predict(press)

        risk_score = self._compute_risk_score(pred_a, pred_b, pred_c)
        overall = self._classify_overall(risk_score)
        flagged = self._collect_flagged_tests(pred_a, pred_b, pred_c)

        rag_context = None
        if query_rag and self._rag is not None:
            rag_context = self._fetch_rag_context(pred_a, pred_b, pred_c, flagged)

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
        )
        self._history.append(snap)
        return snap

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

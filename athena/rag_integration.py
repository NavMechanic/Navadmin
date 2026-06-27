"""
Athena ↔ RAG Integration — bridges Athena orchestration with RAG context.

Provides:
1. RCA (Root Cause Analysis) during quality inspection via RAG query
2. Defect → FMEA correlation
3. Ingest Athena run results as future knowledge
4. Failure prediction based on historical defect patterns
"""

from __future__ import annotations

from typing import Optional, List

from athena.models import AthenaRun, QualityRevenueResult, VisionInspectionResult


class AthenaRAGBridge:
    """
    Connects Athena orchestration results with RAG knowledge base and
    digital twin predictions.
    """

    def __init__(self, rag_pipeline=None, digital_twin=None) -> None:
        """
        Args:
            rag_pipeline: Optional RAGPipeline instance for RCA lookups.
            digital_twin: Optional DigitalTwin instance for FMEA correlation.
        """
        self.rag = rag_pipeline
        self.twin = digital_twin

    # ------------------------------------------------------------------
    # RCA — Root Cause Analysis for defects
    # ------------------------------------------------------------------

    def analyze_defect_via_rag(
        self,
        defect_type: str,
        unit_serial: str,
        defect_confidence: float,
        evaluate: bool = False,
    ) -> dict:
        """
        Query RAG for historical context on a defect type.
        Returns prior failure analysis, process adjustments, and part risks.
        """
        if not self.rag:
            return {"message": "RAG pipeline not available"}

        question = f"""
What are the known causes, failure modes, and corrective actions for
'{defect_type}' defects in Aviator assembly? Include any part risks,
process capability issues, or supplier quality alerts related to this
defect category.
""".strip()

        try:
            result = self.rag.query(question, evaluate=evaluate)
            return {
                "defect_type": defect_type,
                "unit_serial": unit_serial,
                "confidence": defect_confidence,
                "rca_answer": result.answer,
                "sources": result.sources,
                "evaluation": result.evaluation.model_dump() if result.evaluation else None,
            }
        except Exception as exc:
            return {"error": str(exc), "defect_type": defect_type}

    # ------------------------------------------------------------------
    # Correlate quality results with FMEA
    # ------------------------------------------------------------------

    def correlate_quality_with_fmea(
        self,
        quality_result: QualityRevenueResult,
    ) -> dict:
        """
        Match observed defects to PFMEA/DFMEA items and report triggered
        high-RPN modes.
        """
        if not self.twin or not self.twin.fmea_assessment:
            return {
                "message": "Digital twin FMEA assessment not available",
                "defect_count": len(
                    [i for i in quality_result.inspections if i.defect_severity.value != "none"]
                ),
            }

        fmea = self.twin.fmea_assessment
        defect_summary = {}
        triggered_items = []

        for insp in quality_result.inspections:
            for dt in insp.defect_types:
                defect_summary[dt] = defect_summary.get(dt, 0) + 1

        # Match defects to FMEA items
        for item in fmea.all_items:
            if any(d in item.failure_mode.lower() for d in defect_summary.keys()):
                if item.rpn >= fmea.high_rpn_threshold:
                    triggered_items.append({
                        "item_id": item.item_id,
                        "failure_mode": item.failure_mode,
                        "rpn": item.rpn,
                        "matched_defects": [
                            d for d in defect_summary.keys()
                            if d in item.failure_mode.lower()
                        ],
                    })

        return {
            "yield_rate": quality_result.yield_rate,
            "total_inspections": len(quality_result.inspections),
            "defect_types": defect_summary,
            "triggered_high_rpn_fmea_items": triggered_items,
            "total_high_rpn_triggered": len(triggered_items),
        }

    # ------------------------------------------------------------------
    # Ingest Athena run as RAG knowledge
    # ------------------------------------------------------------------

    def ingest_athena_run(self, run: AthenaRun) -> int:
        """
        Convert an Athena run into knowledge documents and ingest into RAG.
        Returns chunk count.
        """
        if not self.rag:
            return 0

        from rag.ingestion.loaders import Document

        # Build a rich narrative doc from the run
        doc_text = self._athena_run_to_text(run)
        doc = Document(
            source=f"athena-run:{run.run_id}",
            text=doc_text,
            metadata={
                "run_id": run.run_id,
                "status": run.status.value,
                "timestamp": run.started_at.isoformat(),
                "deal_id": run.demand.signal.deal_id if run.demand else None,
                "build_qty": run.demand.recommended_build_qty if run.demand else None,
                "yield_rate": run.quality.yield_rate if run.quality else None,
            },
        )

        return self.rag._chunk_and_index_documents([doc])

    # ------------------------------------------------------------------
    # Failure prediction based on Athena + FMEA + Digital Twin
    # ------------------------------------------------------------------

    def predict_run_risk(
        self,
        deal_id: str,
        build_qty: int,
    ) -> dict:
        """
        Before kicking off Athena, use historical runs and FMEA to
        estimate yield and risk.
        """
        if not self.rag:
            return {
                "message": "RAG pipeline not available; cannot predict",
            }

        question = f"""
Based on historical Athena production runs for deals similar to {deal_id}
with build quantities around {build_qty} units, what is the typical yield
rate? What are the top 3 failure modes? What quality metrics are expected?
""".strip()

        try:
            result = self.rag.query(question, evaluate=False)
            return {
                "deal_id": deal_id,
                "build_qty": build_qty,
                "prediction_context": result.answer,
                "sources": result.sources,
            }
        except Exception as exc:
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Defect trend analysis
    # ------------------------------------------------------------------

    def analyze_defect_trends(self, num_recent_runs: int = 10) -> dict:
        """
        Query RAG for trends in defect types, suppliers, and parts across
        recent Athena runs.
        """
        if not self.rag:
            return {"message": "RAG pipeline not available"}

        question = f"""
Across the most recent {num_recent_runs} Project Athena production runs,
what are the top defect types? Which parts or suppliers appear most
frequently? What process indicators suggest upcoming quality issues?
""".strip()

        try:
            result = self.rag.query(question, evaluate=False)
            return {
                "num_runs_analyzed": num_recent_runs,
                "trend_analysis": result.answer,
                "sources": result.sources,
            }
        except Exception as exc:
            return {"error": str(exc)}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _athena_run_to_text(run: AthenaRun) -> str:
        """Convert an AthenaRun to a narrative text document."""
        lines = [
            f"# Project Athena Production Run {run.run_id}",
            f"Status: {run.status.value.upper()}",
            f"Started: {run.started_at.isoformat()}",
            "",
        ]

        if run.demand:
            d = run.demand
            lines.extend([
                "## Demand Forecast (Pecan AI)",
                f"- Deal: {d.signal.deal_id} ({d.signal.account_name})",
                f"- Win probability: {d.signal.probability:.0%}",
                f"- Estimated units: {d.signal.estimated_units}",
                f"- Recommended build quantity: {d.recommended_build_qty}",
                "",
            ])

        if run.sourcing:
            s = run.sourcing
            lines.extend([
                "## Sourcing Lock (Duro PLM + Nexar)",
                f"- BOM: {s.bom.bom_id} rev {s.bom.revision}",
                f"- Total lines: {len(s.bom.lines)}",
                f"- Shortages: {s.shortage_count}",
                f"- Alternates mapped: {s.alternates_mapped}",
                f"- Sourcing risk: {s.sourcing_risk}",
                "",
            ])
            for a in s.availability[:5]:  # top 5
                lines.append(f"  - {a.mpn} ({a.status.value})")

        if run.simulation:
            sim = run.simulation
            pv = sim.physical_validation
            lines.extend([
                "## Simulation & Optimization (Lokad + Ansys)",
                f"- Physical validation: {'PASS' if pv.passed else 'FAIL'}",
                f"- Max stress: {pv.max_stress_mpa} MPa",
                f"- Thermal delta: {pv.thermal_delta_c} °C",
                f"- Vibration: {pv.vibration_g_rms} g RMS",
            ])
            if pv.failure_modes:
                lines.append(f"- Predicted failure modes: {', '.join(pv.failure_modes)}")
            lines.append("")

        if run.execution:
            ex = run.execution
            lines.extend([
                "## Execution (MacroFab + Fictiv)",
                f"- PCBA order: {ex.pcba_order.order_id}",
                f"  - ETA: {ex.pcba_order.estimated_ship_date}",
                f"- Mechanical order: {ex.mechanical_order.order_id}",
                f"  - ETA: {ex.mechanical_order.estimated_ship_date}",
                f"- Total cost: ${ex.total_cost_usd:,.2f}",
                f"- Lead time: {ex.lead_time_days} days",
                "",
            ])

        if run.quality:
            q = run.quality
            lines.extend([
                "## Quality & Revenue (Instrumental + TwinMaker + RaaS/YaSS)",
                f"- Units inspected: {len(q.inspections)}",
                f"- Yield rate: {q.yield_rate * 100:.1f}%",
                f"- RaaS (monthly): ${q.total_raas_monthly_usd:,.2f}",
                f"- YaSS (annual): ${q.total_yass_annual_usd:,.2f}",
                "",
            ])
            defect_summary = {}
            for insp in q.inspections:
                for dt in insp.defect_types:
                    defect_summary[dt] = defect_summary.get(dt, 0) + 1
            if defect_summary:
                lines.append("Top defects observed:")
                for dt, count in sorted(defect_summary.items(), key=lambda x: -x[1])[:5]:
                    lines.append(f"  - {dt}: {count}")

        if run.errors:
            lines.extend(["", "## Warnings / Errors"])
            for e in run.errors:
                lines.append(f"- {e}")

        return "\n".join(lines)


# Convenience function for use within Athena layers
def get_rca_context(
    rag_pipeline,
    defect_type: str,
    unit_serial: str = "",
) -> str:
    """Fetch RCA context for a defect type via RAG."""
    if not rag_pipeline:
        return ""
    bridge = AthenaRAGBridge(rag_pipeline=rag_pipeline)
    result = bridge.analyze_defect_via_rag(
        defect_type=defect_type,
        unit_serial=unit_serial,
        defect_confidence=1.0,
    )
    return result.get("rca_answer", "")

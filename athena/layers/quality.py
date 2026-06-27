"""
Layer 5 — Quality & Performance Monetization.
Instrumental.ai vision analytics → AWS IoT TwinMaker → RaaS/YaSS revenue engine.
Optionally integrates with RAG for root-cause analysis on defects.
"""

from __future__ import annotations

import random
import uuid
from datetime import datetime
from typing import List, Optional

import httpx

from athena.config import cfg
from athena.models import (
    DefectSeverity,
    QualityRevenueResult,
    RevenueShare,
    TwinMakerUpdate,
    VisionInspectionResult,
)


class QualityRevenueLayer:
    """
    Streams vision analytics from Instrumental.ai, pushes 3-D state into
    AWS IoT TwinMaker, and computes live RaaS/YaSS revenue shares per unit.
    Optionally queries RAG for RCA context when defects are detected.
    """

    def __init__(self, rag_pipeline=None) -> None:
        self._instrumental = httpx.Client(
            base_url=cfg.INSTRUMENTAL_BASE_URL,
            headers={"X-API-Key": cfg.INSTRUMENTAL_API_KEY},
            timeout=cfg.HTTP_TIMEOUT_S,
        )
        self.rag_pipeline = rag_pipeline

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def run(self, build_qty: int, serial_prefix: str = "SN") -> QualityRevenueResult:
        serials = [f"{serial_prefix}-{i + 1:04d}" for i in range(build_qty)]

        inspections = self._stream_vision(serials)
        twin_updates = self._push_twinmaker(inspections)
        revenue_shares = self._compute_revenue(inspections)

        defect_free = sum(1 for i in inspections if i.defect_severity == DefectSeverity.NONE)
        yield_rate = defect_free / len(inspections) if inspections else 0.0

        return QualityRevenueResult(
            inspections=inspections,
            twin_updates=twin_updates,
            revenue_shares=revenue_shares,
            yield_rate=round(yield_rate, 4),
            total_raas_monthly_usd=round(sum(r.raas_monthly_usd for r in revenue_shares), 2),
            total_yass_annual_usd=round(sum(r.yass_annual_usd for r in revenue_shares), 2),
        )

    # ------------------------------------------------------------------
    # Instrumental.ai — vision inspection
    # ------------------------------------------------------------------

    def _stream_vision(self, serials: List[str]) -> List[VisionInspectionResult]:
        if cfg.ATHENA_DRY_RUN or not cfg.INSTRUMENTAL_API_KEY:
            return self._mock_inspections(serials)

        results = []
        for serial in serials:
            resp = self._instrumental.get(f"/inspections/{serial}/latest")
            if resp.status_code == 404:
                # Unit not yet in system — treat as pending (no defect)
                results.append(
                    VisionInspectionResult(
                        unit_serial=serial,
                        defect_severity=DefectSeverity.NONE,
                        defect_types=[],
                        confidence=1.0,
                    )
                )
                continue
            resp.raise_for_status()
            data = resp.json()
            severity = DefectSeverity(data.get("severity", "none").lower())
            results.append(
                VisionInspectionResult(
                    unit_serial=serial,
                    defect_severity=severity,
                    defect_types=data.get("defect_types", []),
                    confidence=data.get("confidence", 1.0),
                    image_key=data.get("image_key"),
                )
            )
        return results

    def _enrich_with_rca(self, inspections: List[VisionInspectionResult]) -> List[dict]:
        """
        For units with defects, query RAG for RCA context on each defect type.
        Returns enriched inspection records with rca_analysis field.
        """
        if not self.rag_pipeline:
            return [{"inspection": i, "rca_analysis": None} for i in inspections]

        from athena.rag_integration import AthenaRAGBridge

        bridge = AthenaRAGBridge(rag_pipeline=self.rag_pipeline)
        enriched = []

        for insp in inspections:
            rca_data = None
            if insp.defect_severity != DefectSeverity.NONE and insp.defect_types:
                # Query RAG for the first (primary) defect type
                primary_defect = insp.defect_types[0]
                rca_data = bridge.analyze_defect_via_rag(
                    defect_type=primary_defect,
                    unit_serial=insp.unit_serial,
                    defect_confidence=insp.confidence,
                    evaluate=False,
                )

            enriched.append({
                "inspection": insp,
                "rca_analysis": rca_data,
            })

        return enriched

    # ------------------------------------------------------------------
    # AWS IoT TwinMaker
    # ------------------------------------------------------------------

    def _push_twinmaker(self, inspections: List[VisionInspectionResult]) -> List[TwinMakerUpdate]:
        if cfg.ATHENA_DRY_RUN or not cfg.AWS_ACCESS_KEY_ID:
            return self._mock_twin_updates(inspections)

        try:
            import boto3  # type: ignore

            client = boto3.client(
                "iottwinmaker",
                region_name=cfg.AWS_REGION,
                aws_access_key_id=cfg.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=cfg.AWS_SECRET_ACCESS_KEY,
            )
        except ImportError:
            return self._mock_twin_updates(inspections)

        updates = []
        for insp in inspections:
            props = {
                "defect_severity": {"doubleValue": self._severity_score(insp.defect_severity)},
                "defect_types": {"stringValue": ",".join(insp.defect_types)},
                "inspection_confidence": {"doubleValue": insp.confidence},
                "last_inspection_ts": {"stringValue": insp.timestamp.isoformat()},
            }
            client.batch_put_property_values(
                workspaceId=cfg.TWINMAKER_WORKSPACE_ID,
                entries=[
                    {
                        "entityId": insp.unit_serial,
                        "componentName": "inspection",
                        "properties": {
                            k: {"value": v} for k, v in props.items()
                        },
                    }
                ],
            )
            updates.append(
                TwinMakerUpdate(
                    asset_id=insp.unit_serial,
                    workspace_id=cfg.TWINMAKER_WORKSPACE_ID,
                    properties_updated=props,
                )
            )
        return updates

    # ------------------------------------------------------------------
    # RaaS / YaSS Revenue Engine
    # ------------------------------------------------------------------

    def _compute_revenue(self, inspections: List[VisionInspectionResult]) -> List[RevenueShare]:
        shares = []
        for insp in inspections:
            quality_score = self._quality_score(insp)
            tier = self._monetization_tier(quality_score)

            multiplier = cfg.REVENUE_QUALITY_MULTIPLIER if tier == "enterprise" else (
                1.2 if tier == "premium" else 1.0
            )

            raas = round(cfg.RAAS_BASE_MONTHLY_USD * multiplier * quality_score, 2)
            yass = round(cfg.YASS_BASE_ANNUAL_USD * multiplier * quality_score, 2)

            shares.append(
                RevenueShare(
                    unit_serial=insp.unit_serial,
                    raas_monthly_usd=raas,
                    yass_annual_usd=yass,
                    quality_score=round(quality_score, 4),
                    monetization_tier=tier,
                )
            )
        return shares

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _severity_score(severity: DefectSeverity) -> float:
        return {
            DefectSeverity.NONE: 0.0,
            DefectSeverity.MINOR: 1.0,
            DefectSeverity.MAJOR: 2.0,
            DefectSeverity.CRITICAL: 3.0,
        }[severity]

    @staticmethod
    def _quality_score(insp: VisionInspectionResult) -> float:
        penalty = {
            DefectSeverity.NONE: 0.0,
            DefectSeverity.MINOR: 0.05,
            DefectSeverity.MAJOR: 0.20,
            DefectSeverity.CRITICAL: 0.50,
        }[insp.defect_severity]
        return max(0.0, insp.confidence - penalty)

    @staticmethod
    def _monetization_tier(quality_score: float) -> str:
        if quality_score >= 0.90:
            return "enterprise"
        if quality_score >= 0.75:
            return "premium"
        return "standard"

    @staticmethod
    def _mock_inspections(serials: List[str]) -> List[VisionInspectionResult]:
        severity_weights = [
            (DefectSeverity.NONE, 0.88),
            (DefectSeverity.MINOR, 0.07),
            (DefectSeverity.MAJOR, 0.04),
            (DefectSeverity.CRITICAL, 0.01),
        ]
        severities, weights = zip(*severity_weights)
        results = []
        for serial in serials:
            sev = random.choices(severities, weights=weights, k=1)[0]
            defect_types: List[str] = []
            if sev == DefectSeverity.MINOR:
                defect_types = random.sample(["solder_bridge", "cold_joint", "flux_residue"], 1)
            elif sev == DefectSeverity.MAJOR:
                defect_types = random.sample(["missing_component", "tombstone", "lifted_pad"], 1)
            elif sev == DefectSeverity.CRITICAL:
                defect_types = ["open_circuit", "wrong_polarity"]

            results.append(
                VisionInspectionResult(
                    unit_serial=serial,
                    defect_severity=sev,
                    defect_types=defect_types,
                    confidence=round(random.uniform(0.90, 0.99), 4),
                    image_key=f"mock/{serial}.jpg",
                )
            )
        return results

    @staticmethod
    def _mock_twin_updates(inspections: List[VisionInspectionResult]) -> List[TwinMakerUpdate]:
        return [
            TwinMakerUpdate(
                asset_id=insp.unit_serial,
                workspace_id=cfg.TWINMAKER_WORKSPACE_ID,
                properties_updated={
                    "defect_severity": QualityRevenueLayer._severity_score(insp.defect_severity),
                    "defect_types": ",".join(insp.defect_types),
                    "confidence": insp.confidence,
                },
            )
            for insp in inspections
        ]

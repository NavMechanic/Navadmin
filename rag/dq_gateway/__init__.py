"""ATHENA Vendor & AI I/O Data Quality Gateway.

Every vendor artifact, AI/SLM/LLM output, CAD/FEA export, supplier quote, financial model and
shop-floor telemetry event passes through ``DQGateway.submit`` (or the telemetry fast path)
before it is indexed into Qdrant, updates the Digital Twin, or crosses a node edge.
"""
from .builder import build_manifest
from .gateway import (ArtifactSubmission, ConnectorAuthorization, DQGateway, DQGatewayError, ExceptionRecord,
                      FB_CATALOG, GateDecision, ingest_route)
from .handoffs import EDGES, GATES, NODE_APPROVAL_GATE, HandoffLedger
from .manifest import (MANIFEST_FIELDS, CanonicalIOManifest, lineage_hash, sha256_bytes, sha256_file, tree_bytes)
from .registry import ArtifactRecord, ArtifactRegistry, EvidenceRecord, RegistryError
from .rules import RULES, RuleResult
from .schemas import SCHEMAS
from .scorer import SCORE_THRESHOLD, WEIGHTS, decide_gate
from .telemetry import STATION_SPECS, TelemetryGate, TelemetryVerdict


class DQGateRejected(Exception):
    """Raised by consumers (RAG pipeline, twin) when an artifact may not be indexed or used."""

    def __init__(self, message: str, decision: "GateDecision | None" = None) -> None:
        super().__init__(message)
        self.decision = decision


__all__ = [
    "ArtifactSubmission", "ArtifactRecord", "ArtifactRegistry", "CanonicalIOManifest", "ConnectorAuthorization",
    "DQGateRejected", "DQGateway", "DQGatewayError", "EDGES", "EvidenceRecord", "ExceptionRecord", "FB_CATALOG",
    "GATES", "GateDecision", "HandoffLedger", "MANIFEST_FIELDS", "NODE_APPROVAL_GATE", "RULES", "RegistryError",
    "RuleResult", "SCHEMAS", "SCORE_THRESHOLD", "STATION_SPECS", "TelemetryGate", "TelemetryVerdict", "WEIGHTS",
    "build_manifest", "decide_gate", "ingest_route", "lineage_hash", "sha256_bytes", "sha256_file", "tree_bytes",
]

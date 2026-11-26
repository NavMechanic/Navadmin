"""Cross-node handoff ledger: the 30 edges E-001..E-030, gates G0..G8, and FB-12 revision invalidation.

Edges are taken verbatim from the reference workbook's "Cross Node Handoffs" sheet.

Rules enforced here:
  * a downstream consumer may only cite an upstream artifact that is RELEASED and ACTIVE;
  * a cross-node citation must match a defined edge (from-node, to-node, payload schema);
    intra-node citations, the shared layer X and G0 rights grants need no edge;
  * releasing a new revision supersedes older ones, and every artifact that pinned an older
    revision is marked REWORK — transitively — with an FB-12 exception (``on_release``).
"""
from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass
from typing import Optional

from .registry import ArtifactRecord, ArtifactRegistry


@dataclass(frozen=True)
class Edge:
    edge_id: str
    source: str
    target: str
    trigger: str
    payload: str
    gate: str


EDGES: tuple[Edge, ...] = tuple(Edge(*row) for row in (
    ("E-001", "Customer Portal", "N1", "Customer RFQ accepted", "customer_rfq.v1", "G0"),
    ("E-002", "Customer Portal", "N2", "Approved RFQ parsed", "customer_rfq.v1", "G1"),
    ("E-003", "Customer Portal", "N6", "Customer chooses SINGLE/MULTI/FULL", "customer_rfq.v1", "G0"),
    ("E-004", "N1", "N2", "Opportunity ranked with evidence", "market_opportunity.v1", "G1"),
    ("E-005", "N1", "N6", "Lead/customer qualification", "market_opportunity.v1", "G1"),
    ("E-006", "N1", "N7", "SAM evidence for pricing", "market_opportunity.v1", "G5"),
    ("E-007", "N2", "N3", "Approved PRD baseline", "product_prd.v1", "G3"),
    ("E-008", "N2", "N4", "Requirement-to-verification generation", "product_prd.v1", "G3"),
    ("E-009", "N2", "N5", "Preliminary BOM sourcing allowed", "product_prd.v1", "G4"),
    ("E-010", "N2", "N6", "Scope freeze for customer proposal", "product_prd.v1", "G5"),
    ("E-011", "N3", "N4", "EDA/CAE design ready for testing", "design_package.v1", "G3"),
    ("E-012", "N3", "N5", "Design-linked source BOM", "bom_item.v1", "G4"),
    ("E-013", "N3", "N7", "NRE and simulation charges", "simulation_report.v1", "G5"),
    ("E-014", "N4", "N3", "Corrective design change request", "capa_record.v1", "G3"),
    ("E-015", "N4", "N8", "Validated test/inspection events", "verification_result.v1", "G7"),
    ("E-016", "N4", "N6", "Technical compliance evidence", "verification_result.v1", "G5"),
    ("E-017", "N4", "N7", "QA and validation budget", "verification_result.v1", "G5"),
    ("E-018", "N5", "N3", "Suggested component alternate", "supplier_quote.v1", "G4"),
    ("E-019", "N5", "N7", "Quote-derived BOM costs", "supplier_quote.v1", "G5"),
    ("E-020", "N5", "N6", "Sourcing feasibility", "supply_risk.v1", "G5"),
    ("E-021", "N6", "N7", "Proposed commercial terms", "bid_package.v1", "G5"),
    ("E-022", "N7", "N6", "Approved quote/bid floor", "project_pnl.v1", "G5"),
    ("E-023", "N7", "N5", "Approved procurement envelope", "funding_plan.v1", "G5"),
    ("E-024", "N6", "Customer Portal", "Quotation/contract reviewed", "bid_package.v1", "G5"),
    ("E-025", "N6", "EMS Partner", "Signed manufacturing release", "contract_record.v1", "G6"),
    ("E-026", "EMS Partner", "N8", "Lot/serial genealogy", "lifecycle_event.v1", "G7"),
    ("E-027", "N8", "N4", "Quality trend/CAPA feedback", "capa_record.v1", "G8"),
    ("E-028", "N8", "N2", "Next-revision field feedback", "model_feedback.v1", "G8"),
    ("E-029", "N8", "N3", "ECO input", "capa_record.v1", "G3"),
    ("E-030", "N7", "Founder", "Project actual P&L", "project_pnl.v1", "G8"),
))
assert len(EDGES) == 30

GATES = {
    "G0": "Contract/data rights", "G1": "Data/schema", "G2": "Model validation", "G3": "Design authority",
    "G4": "Supplier", "G5": "Commercial", "G6": "Integration", "G7": "Customer acceptance", "G8": "Scale decision",
}
# Human approval gate per producing node (from the reference "Gates and Risks" affected-WBS mapping)
NODE_APPROVAL_GATE = {"N1": "G1", "N2": "G3", "N3": "G3", "N4": "G3", "N5": "G4", "N6": "G5", "N7": "G5",
                      "N8": "G7", "X": "G2"}
EXEMPT_SCHEMAS = frozenset({"rights_grant.v1"})


@dataclass(frozen=True)
class HandoffCheck:
    ok: bool
    edge_id: Optional[str]
    reason: str


def edge_endpoint(record: ArtifactRecord) -> str:
    return "Customer Portal" if record.schema_key == "customer_rfq.v1" else record.node_id


class HandoffLedger:
    def __init__(self, registry: ArtifactRegistry) -> None:
        self.registry = registry
        self._lock = threading.Lock()
        self._index = {(e.source, e.target, e.payload): e for e in EDGES}
        self.usage: Counter = Counter()
        self.log: list[dict] = []

    def edge_for(self, source: str, target: str, payload: str) -> Optional[Edge]:
        return self._index.get((source, target, payload))

    def authorize(self, source: ArtifactRecord, consumer_node: str) -> HandoffCheck:
        if source.schema_key in EXEMPT_SCHEMAS:
            return HandoffCheck(True, None, "G0 rights grant")
        if source.status != "ACTIVE":
            return HandoffCheck(False, None, f"{source.ref} is {source.status}: {source.status_reason}")
        origin = edge_endpoint(source)
        if origin == consumer_node or origin == "X":
            return HandoffCheck(True, None, "intra-node or shared layer")
        edge = self.edge_for(origin, consumer_node, source.schema_key)
        if edge is None:
            return HandoffCheck(False, None, f"no defined edge {origin} -> {consumer_node} for {source.schema_key}")
        return HandoffCheck(True, edge.edge_id, f"{edge.edge_id} {edge.trigger} ({edge.gate})")

    def record(self, edge_id: Optional[str], source_ref: str, consumer_ref: str) -> None:
        if edge_id is None:
            return
        with self._lock:
            self.usage[edge_id] += 1
            self.log.append({"edge_id": edge_id, "source": source_ref, "consumer": consumer_ref})

    def on_release(self, released: ArtifactRecord) -> list[tuple[str, str]]:
        """Supersede older revisions and cascade REWORK to consumers of them (FB-12). Returns (ref, reason)."""
        invalidated: list[tuple[str, str]] = []
        for old in self.registry.revisions(released.artifact_id):
            if old.revision != released.revision and old.status == "ACTIVE":
                self.registry.set_status(old.ref, "SUPERSEDED", f"superseded by {released.ref}")
        stale_prefix = f"{released.artifact_id}@"
        frontier = [c for c in self.registry.consumers_of(released.artifact_id)
                    if any(s.startswith(stale_prefix) and s != released.ref for s in c.sources)]
        seen: set[str] = set()
        while frontier:
            nxt = []
            for rec in frontier:
                if rec.ref in seen or rec.status != "ACTIVE":
                    continue
                seen.add(rec.ref)
                reason = f"FB-12: upstream {released.artifact_id} bumped to revision {released.revision}"
                self.registry.set_status(rec.ref, "REWORK", reason)
                invalidated.append((rec.ref, reason))
                nxt.extend(self.registry.consumers_of(rec.artifact_id))
            frontier = nxt
        return invalidated

    def edge_table(self) -> list[dict]:
        return [{"edge_id": e.edge_id, "from": e.source, "to": e.target, "payload": e.payload, "gate": e.gate,
                 "trigger": e.trigger, "handoffs": self.usage.get(e.edge_id, 0)} for e in EDGES]

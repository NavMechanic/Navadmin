"""Automated gate checks referenced by name from each activity's gate_spec.

Each check inspects live platform state for the tenant and returns pass/fail plus
detail. Checks fail closed: no data means the gate cannot pass.
"""
from __future__ import annotations

from sqlalchemy import select

from ..ai import evals
from ..ai.extraction import extract_rfq
from ..contracts.rfq import REQUIRED
from ..core.security import PUBLIC_PATHS, route_requires_auth
from ..models import Artifact, ArtifactRevision, Bid, TelemetryEvent, Tenant, Vendor
from ..nodes import n1_market, n2_product, n7_finance, n8_lifecycle
from ..orchestration.selftest import run_selftest
from ..thread import service as thread
from .gates import CheckContext, CheckResult


def _ancestor(ctx: CheckContext, revision_id: int, kind: str) -> ArtifactRevision | None:
    for a in thread.lineage(ctx.db, ctx.tenant_id, revision_id)["ancestors"]:
        if a["kind"] == kind:
            return ctx.db.get(ArtifactRevision, a["id"])
    return None


def rbac_enforced(ctx: CheckContext) -> CheckResult:
    if ctx.app is None:
        return CheckResult("rbac_enforced", False, "needs the running app to inspect routes")
    unprotected = [r.path for r in ctx.app.routes if getattr(r, "dependant", None) is not None
                   and r.path not in PUBLIC_PATHS and not route_requires_auth(r)]
    return CheckResult("rbac_enforced", not unprotected, {"unprotected_routes": unprotected})


def thread_lineage_complete(ctx: CheckContext) -> CheckResult:
    cov = thread.coverage(ctx.db, ctx.tenant_id)
    return CheckResult("", cov["non_root_revisions"] > 0 and cov["coverage"] == 1.0, cov)


def rfq_schema_contract(ctx: CheckContext) -> CheckResult:
    sample = "Supply voltage: 5 V\nSupply voltage: 12 V\nQuantity: 100 units\nOperating temperature: -20 to 60 °C"
    out = extract_rfq(sample)
    detail = {
        "required_fields_declared": list(REQUIRED),
        "conflict_detected": any(c["field"] == "supply_voltage" for c in out["conflicts"]),
        "provenance_on_every_field": all("source" in f and f["source"]["text"] for f in out["fields"].values()),
        "flags_for_review": out["needs_review"],
    }
    return CheckResult("", all(v for k, v in detail.items() if k != "required_fields_declared") and bool(REQUIRED), detail)


def tenant_isolation(ctx: CheckContext) -> CheckResult:
    r = evals.tenant_isolation_probe()
    return CheckResult("", r["passed"], r["results"])


def budget_capped(ctx: CheckContext) -> CheckResult:
    tenant = ctx.db.get(Tenant, ctx.tenant_id)
    probe = evals.budget_probe()
    ok = tenant is not None and tenant.llm_budget_usd > 0 and probe["passed"]
    return CheckResult("", ok, {"tenant_budget_usd": tenant.llm_budget_usd if tenant else None, **probe["results"]})


def extraction_accuracy(ctx: CheckContext) -> CheckResult:
    r = evals.evaluate_extraction(threshold=ctx.settings.extraction_accuracy_gate)
    return CheckResult("", r["passed"], {k: r[k] for k in ("accuracy", "threshold", "cases", "fields_total", "per_field")})


def orchestration_selftest(ctx: CheckContext) -> CheckResult:
    r = run_selftest(ctx.settings)
    return CheckResult("", r["passed"], r["results"])


def market_sam_supported(ctx: CheckContext) -> CheckResult:
    rev = thread.latest_revision(ctx.db, ctx.tenant_id, "MARKET")
    if rev is None:
        return CheckResult("", False, "no MARKET artifact recorded")
    ok, problems = n1_market.sam_supported(rev.content)
    return CheckResult("", ok and bool(rev.content.get("sam")), {"revision_id": rev.id, "problems": problems,
                                                                "sam_entries": len(rev.content.get("sam", []))})


def prd_traceability(ctx: CheckContext) -> CheckResult:
    test = thread.latest_revision(ctx.db, ctx.tenant_id, "TEST")
    if test is None:
        return CheckResult("", False, "no TEST artifact recorded")
    prd = _ancestor(ctx, test.id, "PRD")
    if prd is None:
        return CheckResult("", False, "latest TEST revision is not linked to a PRD")
    trace = n2_product.traceability(prd.content, test.content.get("test_plan", {}))
    return CheckResult("", trace["passed"], {"prd_revision": prd.id, "test_revision": test.id,
                                             **{k: trace[k] for k in ("critical_total", "covered", "uncovered")}})


def _latest_content_flag(ctx, kind, path, label):
    rev = thread.latest_revision(ctx.db, ctx.tenant_id, kind)
    if rev is None:
        return CheckResult("", False, f"no {kind} artifact recorded")
    value = rev.content
    for p in path:
        value = value.get(p, {}) if isinstance(value, dict) else {}
    return CheckResult("", value is True, {"revision_id": rev.id, label: value})


def design_reviewed(ctx: CheckContext) -> CheckResult:
    return _latest_content_flag(ctx, "SIM", ("review", "passed"), "review_passed")


def quality_release_ready(ctx: CheckContext) -> CheckResult:
    return _latest_content_flag(ctx, "TEST", ("gate", "passed"), "quality_gate_passed")


def sourcing_bids(ctx: CheckContext) -> CheckResult:
    return _latest_content_flag(ctx, "BOM", ("gate_passed",), "sourcing_gate_passed")


def bid_awarded_by_human(ctx: CheckContext) -> CheckResult:
    bids = ctx.db.scalars(select(Bid).where(Bid.tenant_id == ctx.tenant_id, Bid.status == "awarded")).all()
    good = [b.id for b in bids if b.decided_by and b.decided_by != b.submitted_by]
    return CheckResult("", bool(good) and len(good) == len(bids), {"awarded_bids": [b.id for b in bids], "human_awarded": good})


def finance_deterministic(ctx: CheckContext) -> CheckResult:
    rev = thread.latest_revision(ctx.db, ctx.tenant_id, "QUOTE")
    if rev is None or "costing" not in rev.content:
        return CheckResult("", False, "no QUOTE costing recorded")
    ok = n7_finance.verify(rev.content["costing"], ctx.settings.gst_rate)
    return CheckResult("", ok, {"revision_id": rev.id, "recomputed_hash_matches": ok})


def telemetry_synthetic_labelled(ctx: CheckContext) -> CheckResult:
    total = len(ctx.db.scalars(select(TelemetryEvent.id).where(TelemetryEvent.tenant_id == ctx.tenant_id).limit(1)).all())
    bad = n8_lifecycle.mislabelled_replay_count(ctx.db, ctx.tenant_id)
    return CheckResult("", total > 0 and bad == 0, {"has_events": total > 0, "mislabelled_replay_events": bad})


def security_harness(ctx: CheckContext) -> CheckResult:
    r = evals.security_harness(ctx.settings, ctx.app)
    return CheckResult("", r["passed"], r["summary"])


def vendors_qualified(ctx: CheckContext) -> CheckResult:
    q = ctx.db.scalars(select(Vendor).where(Vendor.tenant_id == ctx.tenant_id, Vendor.status == "qualified")).all()
    return CheckResult("", len(q) >= 2, {"qualified": [v.name for v in q]})


def vendor_scorecards(ctx: CheckContext) -> CheckResult:
    vs = ctx.db.scalars(select(Vendor).where(Vendor.tenant_id == ctx.tenant_id,
                                             Vendor.status.in_(("qualified", "rejected")))).all()
    missing = [v.name for v in vs if not (v.scorecard.get("decision") and v.scorecard.get("evidence"))]
    return CheckResult("", bool(vs) and not missing, {"vendors": len(vs), "missing_scorecards": missing})


def rfq_end_to_end_lineage(ctx: CheckContext) -> CheckResult:
    from ..integration.rfq_pipeline import REQUIRED_LINEAGE
    quotes = ctx.db.scalars(select(ArtifactRevision).join(Artifact)
                            .where(Artifact.tenant_id == ctx.tenant_id, Artifact.kind == "QUOTE")
                            .order_by(ArtifactRevision.id.desc())).all()
    for q in quotes:
        if q.content.get("status") == "issued":
            kinds = set(thread.lineage(ctx.db, ctx.tenant_id, q.id)["kinds"])
            missing = sorted(REQUIRED_LINEAGE - kinds)
            return CheckResult("", not missing, {"quote_revision": q.id, "lineage_kinds": sorted(kinds), "missing": missing})
    return CheckResult("", False, "no issued QUOTE found")


CHECKS = {
    "rbac_enforced": rbac_enforced, "thread_lineage_complete": thread_lineage_complete,
    "rfq_schema_contract": rfq_schema_contract, "tenant_isolation": tenant_isolation, "budget_capped": budget_capped,
    "extraction_accuracy": extraction_accuracy, "orchestration_selftest": orchestration_selftest,
    "market_sam_supported": market_sam_supported, "prd_traceability": prd_traceability,
    "vendors_qualified": vendors_qualified, "design_reviewed": design_reviewed,
    "quality_release_ready": quality_release_ready, "sourcing_bids": sourcing_bids,
    "bid_awarded_by_human": bid_awarded_by_human, "finance_deterministic": finance_deterministic,
    "telemetry_synthetic_labelled": telemetry_synthetic_labelled, "security_harness": security_harness,
    "vendor_scorecards": vendor_scorecards, "rfq_end_to_end_lineage": rfq_end_to_end_lineage,
}

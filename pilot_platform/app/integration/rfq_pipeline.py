"""Integration (activity U): one RFQ spans the nodes, with lineage, on the durable engine.

    ingest_rfq -> extract_rfq -> [review_extraction] -> build_prd -+-> design_pack -> quality_plan -+
                                                                  +-> sourcing ---------------------+-> costing -> [approve_quote] -> issue_quote

[..] are human approval steps. Every task writes a digital-thread revision derived from
its inputs, so the issued QUOTE's lineage reaches back through BOM, TEST, SIM and PRD to
the RFQ. Business-rule failures raise PermanentStepError (no pointless retries); if the
quote approval is rejected, the draft quote is voided by compensation.
"""
from __future__ import annotations

from datetime import date

from ..ai.extraction import extract_rfq
from ..db import utcnow
from ..nodes import n2_product, n3_design, n4_quality, n5_sourcing, n7_finance
from ..orchestration.engine import PermanentStepError, StepContext, StepDef, WorkflowDef, handler, register_workflow
from ..thread import service as thread


def _key(ctx: StepContext) -> str:
    key = ctx.input.get("rfq_key")
    if not key:
        raise PermanentStepError("rfq_key is required")
    return key


def _out(ctx: StepContext, step: str, field: str):
    return ctx.outputs[step][field]


@handler("rfq.ingest")
def ingest(ctx: StepContext) -> dict:
    if not str(ctx.input.get("rfq_text", "")).strip():
        raise PermanentStepError("rfq_text is required")
    rev = thread.create_revision(ctx.db, ctx.tenant_id, ctx.actor, "RFQ", _key(ctx),
                                 {"customer": ctx.input.get("customer"), "raw_text": ctx.input["rfq_text"], "stage": "received"},
                                 note="RFQ received")
    return {"rfq_revision_id": rev.id}


@handler("rfq.extract")
def extract(ctx: StepContext) -> dict:
    source = thread.get_revision(ctx.db, ctx.tenant_id, _out(ctx, "ingest_rfq", "rfq_revision_id"))
    extraction = extract_rfq(source.content["raw_text"])
    rev = thread.create_revision(ctx.db, ctx.tenant_id, ctx.actor, "RFQ", _key(ctx),
                                 {**source.content, "stage": "extracted", "extraction": extraction},
                                 derived_from=[source.id], note=f"extracted by {extraction['extractor']}")
    return {"rfq_revision_id": rev.id, "needs_review": extraction["needs_review"],
            "missing_required": extraction["missing_required"], "conflicts": [c["field"] for c in extraction["conflicts"]],
            "low_confidence": extraction["low_confidence"]}


@handler("prd.build")
def build_prd(ctx: StepContext) -> dict:
    rfq = thread.get_revision(ctx.db, ctx.tenant_id, _out(ctx, "extract_rfq", "rfq_revision_id"))
    prd = n2_product.build_prd(rfq.content["extraction"])
    if prd["blocking"]:
        raise PermanentStepError("PRD engineering checks failed: " + "; ".join(f["message"] for f in prd["blocking"]))
    rev = thread.create_revision(ctx.db, ctx.tenant_id, ctx.actor, "PRD", _key(ctx), prd, derived_from=[rfq.id])
    return {"prd_revision_id": rev.id, "requirements": len(prd["requirements"]),
            "warnings": [f["message"] for f in prd["findings"] if f["severity"] == "warning"]}


@handler("design.pack")
def design_pack(ctx: StepContext) -> dict:
    pack = ctx.input.get("design")
    if not pack:
        raise PermanentStepError("design pack is required")
    review = n3_design.review_design_pack(pack)
    if not review["passed"]:
        raise PermanentStepError("design review failed: " + "; ".join(review["issues"]))
    content = {"pack": {k: v for k, v in pack.items() if k != "simulations"}, "simulations": review["simulations"],
               "review": {"passed": True, "issues": [], "reviewed_by": pack["reviewed_by"]}}
    rev = thread.create_revision(ctx.db, ctx.tenant_id, ctx.actor, "SIM", _key(ctx), content,
                                 derived_from=[_out(ctx, "build_prd", "prd_revision_id")])
    return {"sim_revision_id": rev.id}


@handler("quality.plan")
def quality_plan(ctx: StepContext) -> dict:
    prd_id = _out(ctx, "build_prd", "prd_revision_id")
    prd = thread.get_revision(ctx.db, ctx.tenant_id, prd_id).content
    plan = n4_quality.build_test_plan(prd, ctx.input.get("tests"))
    rows = n4_quality.pfmea(ctx.input.get("pfmea", []))
    gate = n4_quality.quality_gate(prd, plan, rows, ctx.settings.pfmea_rpn_threshold)
    if not gate["passed"]:
        raise PermanentStepError("quality gate blocked: " + "; ".join(gate["blocking_gaps"] or ["no critical requirements"]))
    rev = thread.create_revision(ctx.db, ctx.tenant_id, ctx.actor, "TEST", _key(ctx),
                                 {"test_plan": plan, "pfmea": rows, "gate": gate},
                                 derived_from=[prd_id, _out(ctx, "design_pack", "sim_revision_id")])
    return {"test_revision_id": rev.id, "tests": len(plan["tests"])}


@handler("sourcing.bom")
def sourcing(ctx: StepContext) -> dict:
    s = ctx.input.get("sourcing") or {}
    as_of = date.fromisoformat(s["as_of"]) if s.get("as_of") else utcnow().date()
    result = n5_sourcing.run_sourcing(s.get("bom", []), s.get("bids", []), s.get("currency", "USD"), as_of,
                                      int(ctx.input["commercial"]["build_qty"]), s.get("single_source_justification"))
    if not result["gate_passed"]:
        raise PermanentStepError(f"sourcing gate failed: {result['comparable_count']} comparable bid(s) and no justification")
    rev = thread.create_revision(ctx.db, ctx.tenant_id, ctx.actor, "BOM", _key(ctx), result,
                                 derived_from=[_out(ctx, "build_prd", "prd_revision_id")])
    return {"bom_revision_id": rev.id, "unit_bom_cost": result["best_bid"]["unit_bom_cost"],
            "supplier": result["best_bid"]["supplier"], "currency": result["currency"]}


@handler("finance.costing")
def costing(ctx: StepContext) -> dict:
    inputs = {**ctx.input["commercial"], "unit_bom_cost": _out(ctx, "sourcing", "unit_bom_cost")}
    calc = n7_finance.cost_model(inputs, ctx.settings.gst_rate)
    rev = thread.create_revision(
        ctx.db, ctx.tenant_id, ctx.actor, "QUOTE", _key(ctx),
        {"status": "draft", "currency": _out(ctx, "sourcing", "currency"), "costing": calc},
        derived_from=[_out(ctx, "sourcing", "bom_revision_id"), _out(ctx, "quality_plan", "test_revision_id"),
                      _out(ctx, "build_prd", "prd_revision_id")])
    return {"quote_revision_id": rev.id, "price_ex_gst": calc["outputs"]["price_ex_gst"],
            "unit_price_ex_gst": calc["outputs"]["unit_price_ex_gst"], "calculation_hash": calc["calculation_hash"]}


@handler("finance.void_draft")
def void_draft(ctx: StepContext) -> dict:
    draft = thread.get_revision(ctx.db, ctx.tenant_id, _out(ctx, "costing", "quote_revision_id"))
    rev = thread.create_revision(ctx.db, ctx.tenant_id, "engine", "QUOTE", _key(ctx), {**draft.content, "status": "void"},
                                 derived_from=[draft.id], note="compensation: draft quote voided")
    return {"void_revision_id": rev.id}


@handler("quote.issue")
def issue_quote(ctx: StepContext) -> dict:
    draft = thread.get_revision(ctx.db, ctx.tenant_id, _out(ctx, "costing", "quote_revision_id"))
    approval = ctx.outputs["approve_quote"]
    rev = thread.create_revision(ctx.db, ctx.tenant_id, ctx.actor, "QUOTE", _key(ctx),
                                 {**draft.content, "status": "issued", "approved_by": approval["decided_by"]},
                                 derived_from=[draft.id], note=f"issued after approval by {approval['decided_by']}")
    return {"quote_revision_id": rev.id}


RFQ_TO_QUOTE = register_workflow(WorkflowDef("rfq_to_quote", (
    StepDef("ingest_rfq", "rfq.ingest"),
    StepDef("extract_rfq", "rfq.extract", ("ingest_rfq",)),
    StepDef("review_extraction", kind="approval", depends_on=("extract_rfq",), approver_roles=("engineer", "programme_manager")),
    StepDef("build_prd", "prd.build", ("review_extraction",)),
    StepDef("design_pack", "design.pack", ("build_prd",)),
    StepDef("quality_plan", "quality.plan", ("build_prd", "design_pack")),
    StepDef("sourcing", "sourcing.bom", ("build_prd",)),
    StepDef("costing", "finance.costing", ("quality_plan", "sourcing"), compensate="finance.void_draft"),
    StepDef("approve_quote", kind="approval", depends_on=("costing",), approver_roles=("founder", "programme_manager")),
    StepDef("issue_quote", "quote.issue", ("approve_quote",)),
), description="One RFQ through N2 product, N3 design, N4 quality, N5 sourcing and N7 finance to an approved quote"))

REQUIRED_LINEAGE = {"RFQ", "PRD", "SIM", "TEST", "BOM"}

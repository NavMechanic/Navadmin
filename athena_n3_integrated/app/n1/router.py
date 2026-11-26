"""N1 API (mounted on the N0 gateway under ``/v1/n1``). Every refusal is audited by N0's ``guarded``.

Roles: ``analyst`` (submit, report, optimize, extract), ``approver`` (attest evidence, label, qualify
the SLM, approve handoffs; implies N0 gateway_reviewer + release_manager), ``service`` (machine
ingestion; implies N0 producer + node_service). ``auditor`` can read.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, FastAPI, Query
from fastapi.responses import PlainTextResponse

from ..errors import N0Error
from ..lineage import register_attr_resolver
from ..security import Principal, principal_dependency
from . import N1_VERSION, engine
from . import slm_extractor as slm
from .config import N1Settings
from .connectors import CONNECTORS
from .models import (ActionOptimizationRequest, ActionOptimizationResult, AttestationRequest, BatchSubmission,
                     BuyerSubmission, ConnectorImport, ConnectorPreview, ConnectorPreviewRequest, EvidenceAttestation,
                     FeedbackCase, FeedbackRequest, HandoffRequest, HandoffResult, IntakeRequest, IntakeResult,
                     MarketObservation, MarketReport, ReportExplanation, ReportRequest, ReportSummary, SlmEvaluation,
                     SlmExtractionRequest, SlmExtractionResult, SubmissionResult)
from .service import N1Service
from .store import N1Store

READERS = ("analyst", "approver", "service", "auditor")


def _register_lineage() -> None:
    def batch(repo, tenant, ref):
        aid, rev = ref.rsplit("@", 1)
        b = N1Store(repo.s).get_batch(tenant, aid, rev)
        return {"kind": b.kind, "batch_id": b.batch_id, "synthetic": b.synthetic, "source_id": b.source_id} if b \
            else {"resolved": False}

    def evidence(repo, tenant, ref):
        eid, rev = ref.rsplit("@", 1)
        revs = [r for r in N1Store(repo.s).observation_revisions(tenant, eid) if str(r.revision) == rev]
        if not revs:
            return {"resolved": False}
        r = revs[0]
        return {"observation_type": r.record.get("observation_type"), "synthetic": r.synthetic,
                "evidence_sha256": r.evidence_sha256, "account_id": r.account_id}

    def account(repo, tenant, ref):
        revs = N1Store(repo.s).account_revisions(tenant, ref)
        return {"legal_name": revs[-1].legal_name, "revision": revs[-1].revision} if revs else {"resolved": False}

    def report(repo, tenant, ref):
        row = N1Store(repo.s).report_row(tenant, ref)
        return {"revision": row.revision, "report_sha256": row.report_sha256} if row else {"resolved": False}

    for kind, fn in (("n1_batch", batch), ("n1_evidence", evidence), ("n1_account", account), ("n1_report", report)):
        register_attr_resolver(kind, fn)


def install(settings: Optional[N1Settings] = None, *, slm_provider=None, explainer_provider=None, transport=None,
            limiter_factory=None) -> Callable[[FastAPI], None]:
    settings = settings or N1Settings.from_env()

    def _install(app: FastAPI) -> None:
        svc = N1Service(app.state, settings, slm_provider=slm_provider, explainer_provider=explainer_provider,
                        transport=transport, limiter_factory=limiter_factory)
        app.state.n1 = svc
        guarded = app.state.guarded
        auth = Depends(principal_dependency)
        r = APIRouter(prefix="/v1/n1", tags=["n1"])

        def run(p: Principal, event: str, subject: str, roles: tuple[str, ...], fn: Callable[[], Any]) -> Any:
            def go():
                if not p.has(*roles):
                    raise N0Error(403, "ROLE_REQUIRED", f"requires one of roles {list(roles)}")
                return fn()
            return guarded(p, event, subject, go)

        @r.get("/status")
        def status(p: Principal = auth) -> dict:
            def go():
                gold_sha = None
                qualified = False
                if svc.slm is not None:
                    _, gold_sha = slm.load_gold(settings.slm_gold_path)
                    with svc.db.session() as s:
                        qualified = N1Store(s).passing_evaluation(p.tenant_id, svc.slm.name, svc.slm.model,
                                                                  slm.PROMPT_REVISION, gold_sha) is not None
                return {"n1_version": N1_VERSION, "engine_version": engine.ENGINE_VERSION,
                        "connectors": {"un_comtrade": {"authenticated": settings.comtrade_api_key is not None},
                                       "sam_gov": {"configured": settings.sam_gov_api_key is not None},
                                       "ted_eu": {"configured": True},
                                       "india_cppp": {"configured": bool(settings.cppp_dropzone)},
                                       "crm": {"configured": True}},
                        "slm": {"provider": settings.slm_provider if svc.slm is None else svc.slm.name,
                                "enabled": svc.slm is not None, "qualified": qualified,
                                "prompt_revision": slm.PROMPT_REVISION, "gold_set_sha256": gold_sha},
                        "explainer": {"enabled": svc.explainer is not None,
                                      "provider": svc.explainer.name if svc.explainer else "disabled"}}
            return run(p, "N1_STATUS", "n1", READERS, go)

        # ---------------------------------------------------------------- N1.1 intake
        @r.post("/batches", response_model=SubmissionResult)
        def submit_batch(body: BatchSubmission, p: Principal = auth) -> SubmissionResult:
            return run(p, "N1_BATCH", f"batch:{body.batch.batch_id}", ("analyst", "service"),
                       lambda: svc.submit_batch(p, body))

        @r.post("/buyers", response_model=SubmissionResult)
        def submit_buyers(body: BuyerSubmission, p: Principal = auth) -> SubmissionResult:
            return run(p, "N1_BUYERS", f"batch:{body.records.batch_id}", ("analyst", "service"),
                       lambda: svc.submit_buyers(p, body))

        @r.post("/intake", response_model=IntakeResult)
        def intake(body: IntakeRequest, p: Principal = auth) -> IntakeResult:
            return run(p, "N1_INTAKE", f"{body.artifact_id}@{body.revision}", ("analyst", "service"),
                       lambda: svc.intake(p, body.artifact_id, body.revision))

        @r.post("/connectors/{name}/preview", response_model=ConnectorPreview)
        def preview(name: str, body: ConnectorPreviewRequest, p: Principal = auth) -> ConnectorPreview:
            return run(p, "N1_CONNECTOR_PREVIEW", f"connector:{name}", ("analyst", "service"),
                       lambda: svc.preview(p, name, body))

        @r.post("/connectors/import", response_model=SubmissionResult)
        def import_preview(body: ConnectorImport, p: Principal = auth) -> SubmissionResult:
            return run(p, "N1_CONNECTOR_IMPORT", f"n1_preview:{body.preview_id}", ("analyst", "service"),
                       lambda: svc.import_preview(p, body))

        @r.get("/buyers/{account_id}")
        def buyer(account_id: str, p: Principal = auth) -> dict:
            def go():
                with svc.db.session() as s:
                    revs = N1Store(s).account_revisions(p.tenant_id, account_id)
                if not revs:
                    raise N0Error(404, "ACCOUNT_NOT_FOUND", f"{account_id} not found in this tenant")
                return {"latest": revs[-1].model_dump(mode="json"),
                        "revisions": [a.model_dump(mode="json") for a in revs]}
            return run(p, "N1_READ", f"n1_account:{account_id}", READERS, go)

        @r.get("/evidence/{evidence_id}")
        def evidence(evidence_id: str, p: Principal = auth) -> dict:
            def go():
                with svc.db.session() as s:
                    st = N1Store(s)
                    revs = st.observation_revisions(p.tenant_id, evidence_id)
                    att = st.attestations_for(p.tenant_id).get(evidence_id)
                if not revs:
                    raise N0Error(404, "EVIDENCE_NOT_FOUND", f"{evidence_id} not found in this tenant")
                return {"latest": {"revision": revs[-1].revision, "evidence_sha256": revs[-1].evidence_sha256,
                                   "n0_artifact": f"{revs[-1].n0_artifact_id}@{revs[-1].n0_revision}",
                                   "account_id": revs[-1].account_id, "observation": revs[-1].record},
                        "revisions": [{"revision": x.revision, "evidence_sha256": x.evidence_sha256,
                                       "n0_artifact": f"{x.n0_artifact_id}@{x.n0_revision}"} for x in revs],
                        "attestation": att.model_dump(mode="json") if att else None,
                        "attestation_current": bool(att and att.evidence_sha256 == revs[-1].evidence_sha256)}
            return run(p, "N1_READ", f"n1_evidence:{evidence_id}", READERS, go)

        # ---------------------------------------------------------------- attestation / feedback
        @r.post("/attestations", response_model=EvidenceAttestation)
        def attest(body: AttestationRequest, p: Principal = auth) -> EvidenceAttestation:
            return run(p, "N1_ATTESTATION", f"n1_evidence:{body.evidence_id}", ("approver",),
                       lambda: svc.attest(p, body))

        @r.get("/attestations/pending")
        def pending(p: Principal = auth) -> list[dict]:
            return run(p, "N1_READ", "attestations:pending", READERS, lambda: svc.pending_attestations(p))

        @r.post("/feedback", response_model=FeedbackCase)
        def feedback(body: FeedbackRequest, p: Principal = auth) -> FeedbackCase:
            return run(p, "N1_FEEDBACK", f"{body.target_kind}:{body.target_id}", ("approver", "analyst"),
                       lambda: svc.feedback(p, body))

        # ---------------------------------------------------------------- reports
        @r.post("/reports", response_model=MarketReport)
        def build_report(body: ReportRequest, p: Principal = auth) -> MarketReport:
            return run(p, "N1_REPORT", f"scope:{body.scope_name}", ("analyst",), lambda: svc.build_report(p, body))

        @r.get("/reports", response_model=list[ReportSummary])
        def list_reports(limit: int = Query(50, ge=1, le=500), p: Principal = auth) -> list[ReportSummary]:
            return run(p, "N1_READ", "reports", READERS, lambda: svc.list_reports(p, limit))

        @r.get("/reports/{report_id}", response_model=MarketReport)
        def get_report(report_id: str, p: Principal = auth) -> MarketReport:
            return run(p, "N1_READ", f"n1_report:{report_id}", READERS, lambda: svc.get_report(p, report_id))

        @r.get("/reports/{report_id}/markdown", response_class=PlainTextResponse)
        def report_markdown(report_id: str, p: Principal = auth) -> PlainTextResponse:
            rep = run(p, "N1_READ", f"n1_report:{report_id}", READERS, lambda: svc.get_report(p, report_id))
            return PlainTextResponse(engine.render_markdown(rep), media_type="text/markdown; charset=utf-8")

        @r.post("/reports/{report_id}/explain", response_model=ReportExplanation)
        def explain(report_id: str, p: Principal = auth) -> ReportExplanation:
            return run(p, "N1_EXPLAIN", f"n1_report:{report_id}", ("analyst", "approver"),
                       lambda: svc.explain(p, report_id))

        @r.post("/reports/{report_id}/handoff", response_model=HandoffResult)
        def handoff(report_id: str, body: HandoffRequest, p: Principal = auth) -> HandoffResult:
            return run(p, "N1_HANDOFF", f"n1_report:{report_id}", ("approver",),
                       lambda: svc.handoff(p, report_id, body))

        # ---------------------------------------------------------------- N1.5
        @r.post("/optimize", response_model=ActionOptimizationResult)
        def optimize(body: ActionOptimizationRequest, p: Principal = auth) -> ActionOptimizationResult:
            return run(p, "N1_OPTIMIZATION", f"optimize:{body.report_id or '-'}", ("analyst", "approver"),
                       lambda: svc.optimize(p, body))

        # ---------------------------------------------------------------- SLM
        @r.post("/slm/evaluate", response_model=SlmEvaluation)
        def slm_evaluate(p: Principal = auth) -> SlmEvaluation:
            return run(p, "N1_SLM_EVALUATION", "slm", ("approver",), lambda: svc.slm_evaluate(p))

        @r.post("/slm/extract", response_model=SlmExtractionResult)
        def slm_extract(body: SlmExtractionRequest, p: Principal = auth) -> SlmExtractionResult:
            return run(p, "N1_SLM_EXTRACTION", f"evidence:{body.evidence_id}", ("analyst",),
                       lambda: svc.slm_extract(p, body))

        app.include_router(r)
        _register_lineage()

    return _install


__all__ = ["install", "CONNECTORS", "MarketObservation"]

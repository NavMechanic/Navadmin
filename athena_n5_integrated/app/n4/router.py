"""N4 API (mounted on the N0 gateway under ``/v1/n4``). Every refusal is audited by N0's ``guarded``.

Roles (separation of duties):
  test_engineer     lock intakes, plan tests, PFMEA / control plan / CAPA, register instruments and SOP
                    drafts, run live SCPI capture, import bench logs, assess, request drafts / illustrations
  lab_importer      import lab reports, SCPI logs, AOI inference and CSV, record calibration certificates
  quality_reviewer  approve SOP revisions, revoke calibrations, adjudicate AOI calls, verify CAPA
                    effectiveness, curate the AI corpus
  test_authority    approve assessments (against the exact evidence hash + design revision) and release
  quality_lead      as test_authority, plus accepted labs and personnel qualifications
  auditor           read
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, FastAPI, File, Form, Response, UploadFile
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from ..errors import N0Error
from ..lineage import register_attr_resolver
from ..security import Principal, principal_dependency
from . import APPROVAL_SCOPE, N4_VERSION, engine
from .config import N4Settings
from .db import N4AssessmentRow, N4IntakeRow, N4PlanRow, N4ReleaseRow
from .models import (AdjudicationRequest, CalibrationRequest, CapaRequest, CapaVerifyRequest, ControlPlanRequest,
                     CorpusDocumentRequest, IntakeRequest, LiveCaptureRequest, N4AcceptedLab, N4Adjudication,
                     N4CalibrationRecord, N4Capa, N4ControlPlan, N4DesignIntake, N4EvidenceIntake, N4Instrument,
                     N4Pfmea, N4Qualification, N4ReleaseRequest, N4ReliabilityIllustration, N4Review,
                     N4ReviewRequest, N4Suggestion, N4TestMethod, N4TestPlan, N4TraceabilityMatrix,
                     N4VerificationAssessment, N4VerificationRelease, PfmeaRequest, ReliabilityRequest,
                     SuggestionRequest, TestMethodRequest, TestPlanRequest)
from .service import N4Service

READERS = ("test_engineer", "lab_importer", "quality_reviewer", "test_authority", "quality_lead", "auditor")
ENGINEER = ("test_engineer",)
IMPORTERS = ("lab_importer", "test_engineer")
REVIEWER = ("quality_reviewer",)
AUTHORITY = ("test_authority", "quality_lead")
LEAD = ("quality_lead",)


def _register_lineage() -> None:
    def rec(row_cls, keys):
        def fn(repo, tenant, ref):
            row = repo.s.get(row_cls, ref)
            if row is None or row.tenant_id != tenant:
                return {"resolved": False}
            return {k: row.record.get(k) for k in keys}
        return fn

    register_attr_resolver("n4_intake", rec(N4IntakeRow, ("prd_artifact", "design_revision", "intake_sha256")))
    register_attr_resolver("n4_plan", rec(N4PlanRow, ("design_revision", "plan_sha256")))
    register_attr_resolver("n4_assessment", rec(N4AssessmentRow, ("decision", "evidence_sha256")))
    register_attr_resolver("n4_release", rec(N4ReleaseRow, ("status", "design_revision", "evidence_sha256")))


def _parse(model, text: str):
    try:
        return model.model_validate_json(text)
    except ValidationError as exc:
        raise N0Error(422, "REQUEST_INVALID", str(exc)[:2000]) from None


async def _read(files: list[UploadFile], cap: int) -> list[tuple[str, Optional[str], bytes]]:
    out = []
    for f in files:
        data = await f.read(cap + 1)
        if len(data) > cap:
            raise N0Error(413, "FILE_TOO_LARGE", f"{f.filename} exceeds {cap} bytes")
        out.append((f.filename or "", f.content_type, data))
    return out


def install(settings: Optional[N4Settings] = None, *, provider=None) -> Callable[[FastAPI], None]:
    settings = settings or N4Settings.from_env()

    def _install(app: FastAPI) -> None:
        svc = N4Service(app.state, settings, provider)
        app.state.n4 = svc
        guarded, auth = app.state.guarded, Depends(principal_dependency)
        cap = app.state.settings.max_upload_bytes
        r = APIRouter(prefix="/v1/n4", tags=["n4"])
        _register_lineage()

        def run(p: Principal, event: str, subject: str, roles: tuple[str, ...], fn: Callable[[], Any]) -> Any:
            def go():
                if not p.has(*roles):
                    raise N0Error(403, "ROLE_REQUIRED", f"requires one of roles {list(roles)}")
                return fn()
            return guarded(p, event, subject, go)

        def evidence_status(response: Response, ev: N4EvidenceIntake) -> N4EvidenceIntake:
            response.status_code = 201 if ev.status == "ACCEPTED" else 422
            return ev

        @r.get("/status")
        def status(p: Principal = auth) -> dict:
            return run(p, "N4_STATUS", "n4", READERS, lambda: {
                "n4_version": N4_VERSION, "engine_version": engine.ENGINE_VERSION, "approval_scope": APPROVAL_SCOPE,
                "ai": svc.ai_status()})

        # ------------------------------------------------------------ registries
        @r.post("/methods", status_code=201, response_model=N4TestMethod)
        async def method(body: str = Form(...), document: Optional[UploadFile] = File(None),
                         p: Principal = auth) -> N4TestMethod:
            req = _parse(TestMethodRequest, body)
            doc = (document.filename or "sop.pdf", await document.read(cap + 1)) if document else None
            return await run_in_threadpool(run, p, "N4_METHOD", f"n4_method:{req.method_id}", ENGINEER, lambda: svc.register_method(p, req, doc))

        @r.post("/methods/{method_id}/revisions/{revision}/approve", response_model=N4TestMethod)
        def approve_method(method_id: str, revision: str, p: Principal = auth) -> N4TestMethod:
            return run(p, "N4_METHOD_APPROVE", f"n4_method:{method_id}@{revision}", REVIEWER,
                       lambda: svc.approve_method(p, method_id, revision))

        @r.post("/instruments", status_code=201, response_model=N4Instrument)
        def instrument(body: N4Instrument, p: Principal = auth) -> N4Instrument:
            return run(p, "N4_INSTRUMENT", f"n4_instrument:{body.instrument_id}", ENGINEER + REVIEWER,
                       lambda: svc.register_instrument(p, body))

        @r.post("/calibrations", status_code=201, response_model=N4CalibrationRecord)
        async def calibration(body: str = Form(...), certificate: Optional[UploadFile] = File(None),
                              p: Principal = auth) -> N4CalibrationRecord:
            req = _parse(CalibrationRequest, body)
            cert = (certificate.filename or "certificate.pdf", await certificate.read(cap + 1)) if certificate else None
            return await run_in_threadpool(run, p, "N4_CALIBRATION", f"n4_calibration:{req.calibration_id}", ("lab_importer",) + REVIEWER,
                       lambda: svc.record_calibration(p, req, cert))

        @r.post("/calibrations/{calibration_id}/revoke", response_model=N4CalibrationRecord)
        def revoke(calibration_id: str, body: dict, p: Principal = auth) -> N4CalibrationRecord:
            return run(p, "N4_CALIBRATION_REVOKE", f"n4_calibration:{calibration_id}", REVIEWER,
                       lambda: svc.revoke_calibration(p, calibration_id, str(body.get("reason", ""))[:500] or "revoked"))

        @r.post("/labs", status_code=201, response_model=N4AcceptedLab)
        def lab(body: N4AcceptedLab, p: Principal = auth) -> N4AcceptedLab:
            return run(p, "N4_LAB", f"n4_lab:{body.lab_id}", LEAD, lambda: svc.accept_lab(p, body))

        @r.post("/qualifications", status_code=201, response_model=N4Qualification)
        def qualification(body: N4Qualification, p: Principal = auth) -> N4Qualification:
            return run(p, "N4_QUALIFICATION", f"principal:{body.principal_id}", LEAD,
                       lambda: svc.grant_qualification(p, body))

        # ------------------------------------------------------------ N4.1 intake
        @r.post("/intakes", status_code=201, response_model=N4DesignIntake)
        def intake(body: IntakeRequest, p: Principal = auth) -> N4DesignIntake:
            return run(p, "N4_INTAKE", f"prd:{body.prd_id}", ENGINEER, lambda: svc.lock_intake(p, body))

        @r.get("/intakes/{intake_id}")
        def get_intake(intake_id: str, p: Principal = auth) -> dict:
            return run(p, "N4_READ", f"n4_intake:{intake_id}", READERS, lambda: svc.intake_view(p.tenant_id, intake_id))

        # ------------------------------------------------------------ N4.2 plan
        @r.post("/intakes/{intake_id}/plans", status_code=201, response_model=N4TestPlan)
        def plan(intake_id: str, body: TestPlanRequest, p: Principal = auth) -> N4TestPlan:
            return run(p, "N4_PLAN", f"n4_intake:{intake_id}", ENGINEER, lambda: svc.create_plan(p, intake_id, body))

        @r.get("/plans/{plan_id}")
        def get_plan(plan_id: str, p: Principal = auth) -> dict:
            return run(p, "N4_READ", f"n4_plan:{plan_id}", READERS, lambda: svc.plan_view(p.tenant_id, plan_id))

        @r.get("/plans/{plan_id}/traceability", response_model=N4TraceabilityMatrix)
        def trace(plan_id: str, p: Principal = auth) -> N4TraceabilityMatrix:
            return run(p, "N4_READ", f"n4_plan:{plan_id}", READERS, lambda: svc.traceability(p.tenant_id, plan_id))

        # ------------------------------------------------------------ N4.3 PFMEA / control plan / CAPA
        @r.post("/plans/{plan_id}/pfmea", status_code=201, response_model=N4Pfmea)
        def pfmea(plan_id: str, body: PfmeaRequest, p: Principal = auth) -> N4Pfmea:
            return run(p, "N4_PFMEA", f"n4_plan:{plan_id}", ENGINEER, lambda: svc.submit_pfmea(p, plan_id, body))

        @r.post("/plans/{plan_id}/control-plan", status_code=201, response_model=N4ControlPlan)
        def control_plan(plan_id: str, body: ControlPlanRequest, p: Principal = auth) -> N4ControlPlan:
            return run(p, "N4_CONTROL_PLAN", f"n4_plan:{plan_id}", ENGINEER,
                       lambda: svc.submit_control_plan(p, plan_id, body))

        @r.post("/capas", status_code=201, response_model=N4Capa)
        def capa(body: CapaRequest, p: Principal = auth) -> N4Capa:
            return run(p, "N4_CAPA", f"n4_capa:{body.capa_id}", ENGINEER + REVIEWER, lambda: svc.open_capa(p, body))

        @r.post("/capas/{capa_id}/verify", response_model=N4Capa)
        def verify_capa(capa_id: str, body: CapaVerifyRequest, p: Principal = auth) -> N4Capa:
            return run(p, "N4_CAPA_VERIFY", f"n4_capa:{capa_id}", REVIEWER, lambda: svc.verify_capa(p, capa_id, body))

        # ------------------------------------------------------------ N4.4 evidence adapters
        @r.post("/plans/{plan_id}/evidence/scpi", response_model=N4EvidenceIntake)
        async def scpi(plan_id: str, response: Response, log_name: str = Form(...), source_id: str = Form(...),
                       files: list[UploadFile] = File(...), p: Principal = auth) -> N4EvidenceIntake:
            data = await _read(files, cap)
            return await run_in_threadpool(run, p, "N4_EVIDENCE_SCPI", f"n4_plan:{plan_id}", IMPORTERS,
                       lambda: evidence_status(response, svc.ingest_scpi(p, plan_id, log_name, data, source_id)))

        @r.post("/plans/{plan_id}/evidence/scpi/live", response_model=N4EvidenceIntake)
        def scpi_live(plan_id: str, body: LiveCaptureRequest, response: Response,
                      p: Principal = auth) -> N4EvidenceIntake:
            return run(p, "N4_EVIDENCE_SCPI_LIVE", f"n4_plan:{plan_id}", ENGINEER,
                       lambda: evidence_status(response, svc.capture_live(p, plan_id, body)))

        @r.post("/plans/{plan_id}/evidence/lab", response_model=N4EvidenceIntake)
        async def lab_report(plan_id: str, response: Response, manifest: str = Form(...), source_id: str = Form(...),
                             files: list[UploadFile] = File(...), p: Principal = auth) -> N4EvidenceIntake:
            data = await _read(files, cap)
            return await run_in_threadpool(run, p, "N4_EVIDENCE_LAB", f"n4_plan:{plan_id}", ("lab_importer",),
                       lambda: evidence_status(response, svc.ingest_lab(p, plan_id, manifest, data, source_id)))

        @r.post("/plans/{plan_id}/evidence/aoi", response_model=N4EvidenceIntake)
        async def aoi(plan_id: str, response: Response, inference: str = Form(...), source_id: str = Form(...),
                      files: list[UploadFile] = File(...), p: Principal = auth) -> N4EvidenceIntake:
            data = await _read(files, cap)
            return await run_in_threadpool(run, p, "N4_EVIDENCE_AOI", f"n4_plan:{plan_id}", IMPORTERS,
                       lambda: evidence_status(response, svc.ingest_aoi(p, plan_id, inference, data, source_id)))

        @r.post("/plans/{plan_id}/evidence/csv", response_model=N4EvidenceIntake)
        async def csv_import(plan_id: str, response: Response, source_id: str = Form(...),
                             files: list[UploadFile] = File(...), p: Principal = auth) -> N4EvidenceIntake:
            data = await _read(files, cap)
            return await run_in_threadpool(run, p, "N4_EVIDENCE_CSV", f"n4_plan:{plan_id}", IMPORTERS,
                       lambda: evidence_status(response, svc.ingest_csv(p, plan_id, data, source_id)))

        @r.get("/evidence/{evidence_id}", response_model=N4EvidenceIntake)
        def get_evidence(evidence_id: str, p: Principal = auth) -> N4EvidenceIntake:
            return run(p, "N4_READ", f"n4_evidence:{evidence_id}", READERS,
                       lambda: svc.evidence(p.tenant_id, evidence_id))

        @r.post("/adjudications", status_code=201, response_model=N4Adjudication)
        def adjudicate(body: AdjudicationRequest, p: Principal = auth) -> N4Adjudication:
            return run(p, "N4_ADJUDICATE", f"n4_evidence:{body.evidence_id}", REVIEWER,
                       lambda: svc.adjudicate(p, body))

        @r.get("/aoi/training-labels")
        def training_labels(p: Principal = auth) -> dict:
            return run(p, "N4_READ", "n4_aoi_labels", REVIEWER + ("auditor",),
                       lambda: svc.training_labels(p.tenant_id))

        # ------------------------------------------------------------ N4.5 / N4.6
        @r.post("/plans/{plan_id}/assess", status_code=201, response_model=N4VerificationAssessment)
        def assess(plan_id: str, p: Principal = auth) -> N4VerificationAssessment:
            return run(p, "N4_ASSESS", f"n4_plan:{plan_id}", ENGINEER, lambda: svc.assess(p, plan_id))

        @r.get("/assessments/{assessment_id}", response_model=N4VerificationAssessment)
        def get_assessment(assessment_id: str, p: Principal = auth) -> N4VerificationAssessment:
            return run(p, "N4_READ", f"n4_assessment:{assessment_id}", READERS,
                       lambda: svc.assessment(p.tenant_id, assessment_id))

        @r.post("/reviews", status_code=201, response_model=N4Review)
        def review(body: N4ReviewRequest, p: Principal = auth) -> N4Review:
            return run(p, "N4_REVIEW", f"n4_assessment:{body.assessment_id}", AUTHORITY, lambda: svc.review(p, body))

        # ------------------------------------------------------------ N4.7 release
        @r.post("/plans/{plan_id}/releases", response_model=N4VerificationRelease)
        def release(plan_id: str, body: N4ReleaseRequest, response: Response,
                    p: Principal = auth) -> N4VerificationRelease:
            def go():
                out = svc.release(p, plan_id, body)
                response.status_code = {"RELEASED": 201, "PARTIAL": 207}.get(out.status, 409)
                return out
            return run(p, "N4_RELEASE", f"n4_plan:{plan_id}", AUTHORITY, go)

        @r.get("/releases/{release_id}")
        def get_release(release_id: str, p: Principal = auth) -> dict:
            return run(p, "N4_READ", f"n4_release:{release_id}", READERS,
                       lambda: svc.release_view(p.tenant_id, release_id))

        @r.get("/plans/{plan_id}/n8-draft")
        def n8_draft(plan_id: str, p: Principal = auth) -> dict:
            return run(p, "N4_READ", f"n4_plan:{plan_id}", READERS, lambda: svc.n8_draft(p.tenant_id, plan_id))

        @r.get("/subjects/{kind}/{subject_id}/status")
        def subject_status(kind: str, subject_id: str, p: Principal = auth) -> dict:
            return run(p, "N4_READ", f"n4_{kind}:{subject_id}", READERS,
                       lambda: svc.status(p.tenant_id, kind, subject_id))

        # ------------------------------------------------------------ AI (gated) and reliability (research)
        @r.post("/corpus", status_code=201)
        def corpus(body: CorpusDocumentRequest, p: Principal = auth) -> dict:
            return run(p, "N4_CORPUS", f"n4_corpus:{body.doc_id}", REVIEWER, lambda: svc.add_corpus_document(p, body))

        @r.post("/plans/{plan_id}/suggestions", status_code=201, response_model=N4Suggestion)
        def suggestions(plan_id: str, body: SuggestionRequest, p: Principal = auth) -> N4Suggestion:
            return run(p, "N4_SUGGEST", f"n4_plan:{plan_id}", ENGINEER, lambda: svc.suggest(p, plan_id, body))

        @r.post("/plans/{plan_id}/reliability", status_code=201, response_model=N4ReliabilityIllustration)
        def reliability(plan_id: str, body: ReliabilityRequest, p: Principal = auth) -> N4ReliabilityIllustration:
            return run(p, "N4_RELIABILITY", f"n4_plan:{plan_id}", ENGINEER, lambda: svc.reliability(p, plan_id, body))

        app.include_router(r)
    return _install


__all__ = ["install"]

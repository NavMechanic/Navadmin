"""N3 API (mounted on the N0 gateway under ``/v1/n3``). Every refusal is audited by N0's ``guarded``.

Roles (separation of duties):
  design_engineer   lock baselines, plan and route work, deliver in-house files, run tool jobs, assess,
                    propose rework actions
  vendor_submitter  external partner identity (JWT ``vendor_id`` claim): deliver files to work packages
                    routed to that vendor, read those packages
  design_authority  onboard vendors, review assessments, release to N4 / N5 (implies N0
                    gateway_reviewer + release_manager). Never for deliveries they submitted.
  auditor           read
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, FastAPI, File, Form, Response, UploadFile

from ..errors import N0Error
from ..lineage import register_attr_resolver
from ..security import Principal, principal_dependency
from . import APPROVAL_SCOPE, N3_VERSION, engine
from .config import N3Settings
from .db import N3AssessmentRow, N3BaselineRow, N3ReleaseRow, N3WorkPackageRow
from .models import (BaselineRequest, FeedbackRequest, N3Action, N3ActionRequest, N3Assessment, N3DesignBaseline,
                     N3Feedback, N3Release, N3Review, N3ReviewRequest, N3Routing, N3ToolJob, N3VendorDelivery,
                     N3VendorProfile, N3WorkPackage, N3WorkPackageRequest, PlanRequest, ReleaseRequest, RouteRequest,
                     StatusView, ToolJobRequest)
from .service import N3Service
from .tool_runner import ToolRunner

READERS = ("design_engineer", "design_authority", "auditor")
ENGINEER = ("design_engineer",)
AUTHORITY = ("design_authority",)


def _register_lineage() -> None:
    def rec(row_cls, keys):
        def fn(repo, tenant, ref):
            row = repo.s.get(row_cls, ref)
            if row is None or row.tenant_id != tenant:
                return {"resolved": False}
            return {k: row.record.get(k) for k in keys}
        return fn

    register_attr_resolver("n3_baseline", rec(N3BaselineRow, ("prd_artifact", "prd_payload_sha256", "baseline_sha256",
                                                              "synthetic")))
    register_attr_resolver("n3_work_package", rec(N3WorkPackageRow, ("kind", "scope", "package_sha256")))
    register_attr_resolver("n3_assessment", rec(N3AssessmentRow, ("decision", "design_revision", "assessment_sha256")))
    register_attr_resolver("n3_release", rec(N3ReleaseRow, ("scope", "design_revision", "status")))


def install(settings: Optional[N3Settings] = None, *, runner: Optional[ToolRunner] = None) -> Callable[[FastAPI], None]:
    settings = settings or N3Settings.from_env()

    def _install(app: FastAPI) -> None:
        svc = N3Service(app.state, settings, runner)
        app.state.n3 = svc
        guarded, auth = app.state.guarded, Depends(principal_dependency)
        r = APIRouter(prefix="/v1/n3", tags=["n3"])
        _register_lineage()

        def run(p: Principal, event: str, subject: str, roles: tuple[str, ...], fn: Callable[[], Any]) -> Any:
            def go():
                if not p.has(*roles):
                    raise N0Error(403, "ROLE_REQUIRED", f"requires one of roles {list(roles)}")
                return fn()
            return guarded(p, event, subject, go)

        @r.get("/status")
        def status(p: Principal = auth) -> dict:
            return run(p, "N3_STATUS", "n3", READERS, lambda: {
                "n3_version": N3_VERSION, "engine_version": engine.ENGINE_VERSION, "approval_scope": APPROVAL_SCOPE,
                **svc.tools()})

        # ------------------------------------------------------------ N3.1 intake / baseline lock
        @r.post("/baselines", status_code=201, response_model=N3DesignBaseline)
        def lock(body: BaselineRequest, p: Principal = auth) -> N3DesignBaseline:
            return run(p, "N3_BASELINE_LOCK", f"prd:{body.prd_id}", ENGINEER, lambda: svc.lock_baseline(p, body))

        @r.get("/baselines/{baseline_id}")
        def baseline(baseline_id: str, p: Principal = auth) -> dict:
            return run(p, "N3_READ", f"n3_baseline:{baseline_id}", READERS,
                       lambda: svc.baseline_view(p.tenant_id, baseline_id))

        # ------------------------------------------------------------ N3.2 planning
        @r.post("/baselines/{baseline_id}/plan", status_code=201, response_model=list[N3WorkPackage])
        def plan(baseline_id: str, body: PlanRequest, p: Principal = auth) -> list[N3WorkPackage]:
            return run(p, "N3_PLAN", f"n3_baseline:{baseline_id}", ENGINEER, lambda: svc.plan(p, baseline_id, body))

        @r.post("/work-packages", status_code=201, response_model=N3WorkPackage)
        def create_wp(body: N3WorkPackageRequest, p: Principal = auth) -> N3WorkPackage:
            return run(p, "N3_WORK_PACKAGE", f"n3_baseline:{body.baseline_id}", ENGINEER,
                       lambda: svc.create_work_package(p, body))

        @r.get("/work-packages/{wp_id}")
        def get_wp(wp_id: str, p: Principal = auth) -> dict:
            return run(p, "N3_READ", f"n3_work_package:{wp_id}", READERS + ("vendor_submitter",),
                       lambda: svc.work_package_view(p, wp_id))

        # ------------------------------------------------------------ N3.3 routing
        @r.post("/vendors", status_code=201, response_model=N3VendorProfile)
        def onboard(body: N3VendorProfile, p: Principal = auth) -> N3VendorProfile:
            return run(p, "N3_VENDOR_ONBOARD", f"n3_vendor:{body.vendor_id}", AUTHORITY,
                       lambda: svc.onboard_vendor(p, body))

        @r.post("/work-packages/{wp_id}/route", response_model=N3Routing)
        def route(wp_id: str, body: RouteRequest, response: Response, p: Principal = auth) -> N3Routing:
            def go():
                out = svc.route(p, wp_id, body)
                response.status_code = 201 if out.authorized else 409
                return out
            return run(p, "N3_ROUTE", f"n3_work_package:{wp_id}", ENGINEER, go)

        # ------------------------------------------------------------ N3.4 execution
        @r.post("/work-packages/{wp_id}/deliveries", response_model=N3VendorDelivery)
        def deliver(wp_id: str, response: Response, manifest: str = Form(...),
                    files: list[UploadFile] = File(...), p: Principal = auth) -> N3VendorDelivery:
            def go():
                cap = app.state.settings.max_upload_bytes
                payload = []
                for f in files:
                    data = f.file.read(cap + 1)
                    if len(data) > cap:
                        raise N0Error(413, "FILE_TOO_LARGE", f"{f.filename} exceeds {cap} bytes")
                    payload.append((f.filename or "", f.content_type, data))
                out = svc.deliver(p, wp_id, manifest, payload)
                response.status_code = 201 if out.status == "ACCEPTED" else 422
                return out
            return run(p, "N3_DELIVERY", f"n3_work_package:{wp_id}", ("design_engineer", "vendor_submitter"), go)

        @r.post("/work-packages/{wp_id}/jobs", response_model=N3ToolJob)
        def submit_job(wp_id: str, body: ToolJobRequest, response: Response, p: Principal = auth) -> N3ToolJob:
            def go():
                job = svc.submit_job(p, wp_id, body)
                response.status_code = 202 if job.status in ("QUEUED", "RUNNING") else 201
                return job
            return run(p, "N3_TOOL_JOB", f"n3_work_package:{wp_id}", ENGINEER, go)

        @r.get("/jobs/{job_id}", response_model=N3ToolJob)
        def get_job(job_id: str, p: Principal = auth) -> N3ToolJob:
            return run(p, "N3_READ", f"n3_job:{job_id}", READERS, lambda: svc.job(p.tenant_id, job_id))

        # ------------------------------------------------------------ N3.5 verification
        @r.post("/work-packages/{wp_id}/assess", status_code=201, response_model=N3Assessment)
        def assess(wp_id: str, p: Principal = auth) -> N3Assessment:
            return run(p, "N3_ASSESS", f"n3_work_package:{wp_id}", ENGINEER, lambda: svc.assess(p, wp_id))

        @r.get("/assessments/{assessment_id}", response_model=N3Assessment)
        def get_assessment(assessment_id: str, p: Principal = auth) -> N3Assessment:
            return run(p, "N3_READ", f"n3_assessment:{assessment_id}", READERS,
                       lambda: svc.assessment(p.tenant_id, assessment_id))

        # ------------------------------------------------------------ N3.6 decision support + gate
        @r.post("/assessments/{assessment_id}/actions", status_code=201, response_model=N3Action)
        def actions(assessment_id: str, body: N3ActionRequest, p: Principal = auth) -> N3Action:
            return run(p, "N3_ACTIONS", f"n3_assessment:{assessment_id}", ENGINEER,
                       lambda: svc.propose_actions(p, assessment_id, body))

        @r.post("/reviews", status_code=201, response_model=N3Review)
        def review(body: N3ReviewRequest, p: Principal = auth) -> N3Review:
            return run(p, "N3_REVIEW", f"n3_assessment:{body.assessment_id}", AUTHORITY, lambda: svc.review(p, body))

        # ------------------------------------------------------------ N3.7 handoff
        @r.post("/baselines/{baseline_id}/releases", response_model=N3Release)
        def release(baseline_id: str, body: ReleaseRequest, response: Response, p: Principal = auth) -> N3Release:
            def go():
                out = svc.release(p, baseline_id, body)
                response.status_code = {"RELEASED": 201, "PARTIAL": 207}.get(out.status, 409)
                return out
            return run(p, "N3_RELEASE", f"n3_baseline:{baseline_id}", AUTHORITY, go)

        @r.get("/releases/{release_id}")
        def get_release(release_id: str, p: Principal = auth) -> dict:
            return run(p, "N3_READ", f"n3_release:{release_id}", READERS,
                       lambda: svc.release_view(p.tenant_id, release_id))

        @r.post("/feedback", status_code=201, response_model=N3Feedback)
        def feedback(body: FeedbackRequest, p: Principal = auth) -> N3Feedback:
            return run(p, "N3_FEEDBACK", f"n3_{body.target_kind.lower()}:{body.target_id}", ENGINEER + AUTHORITY,
                       lambda: svc.feedback(p, body))

        @r.get("/subjects/{kind}/{subject_id}/status", response_model=StatusView)
        def subject_status(kind: str, subject_id: str, p: Principal = auth) -> StatusView:
            return run(p, "N3_READ", f"n3_{kind}:{subject_id}", READERS, lambda: svc.status(p.tenant_id, kind, subject_id))

        app.include_router(r)
    return _install

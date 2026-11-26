"""Vendor atlas, connector registry and vendor job endpoints."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..atlas import connectors, jobs, service
from ..atlas.importer import import_atlas, import_view
from ..atlas.signatures import VerifiedCallback, verified_vendor_callback
from ..core.errors import ValidationFailed
from ..core.security import Principal, current_principal, require_roles
from ..db import get_db
from ..models import AtlasImport, VendorConnector, VendorJob
from ..vendors.service import vendor_view as engagement_view

router = APIRouter()


# --- atlas import & query ------------------------------------------------------------

@router.post("/atlas/imports", tags=["vendor atlas"], status_code=201)
async def upload_atlas(request: Request, filename: str = "atlas.xlsx",
                       p: Principal = Depends(require_roles("vendor_lead", "programme_manager")),
                       db: Session = Depends(get_db)):
    """Body: the raw .xlsx bytes (Content-Type application/vnd.openxmlformats-officedocument.spreadsheetml.sheet)."""
    settings = request.app.state.settings
    data = await request.body()
    if len(data) > settings.atlas_max_bytes:
        raise HTTPException(413, f"atlas file exceeds {settings.atlas_max_bytes} bytes")
    imp, duplicate = import_atlas(db, p.tenant_id, p.username, data, filename, settings)
    db.commit()
    if imp.status == "rejected":
        raise ValidationFailed("atlas rejected: fix the listed rows and re-upload", import_id=imp.id,
                               rejected_rows=len(imp.errors), errors=imp.errors[:50])
    return {**import_view(imp), "duplicate": duplicate}


@router.get("/atlas/imports", tags=["vendor atlas"])
def list_imports(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(AtlasImport).where(AtlasImport.tenant_id == p.tenant_id).order_by(AtlasImport.id.desc())).all()
    return [import_view(i) for i in rows]


@router.get("/atlas/vendors", tags=["vendor atlas"])
def search_atlas(q: str | None = None, node: str | None = None, segment: str | None = None, selection: str | None = None,
                 price_evidence: str | None = None, ai_evidence: str | None = None, include_withdrawn: bool = False,
                 limit: int = 50, offset: int = 0, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return service.search(db, p.tenant_id, q, node, segment, selection, price_evidence, ai_evidence, include_withdrawn,
                          max(1, min(limit, 500)), max(0, offset))


@router.get("/atlas/facets", tags=["vendor atlas"])
def atlas_facets(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return service.facets(db, p.tenant_id)


@router.get("/atlas/stack", tags=["vendor atlas"])
def atlas_stack(nodes: str = "N1,N2,N3,N4,N5,N6,N7,N8", p: Principal = Depends(current_principal),
                db: Session = Depends(get_db)):
    wanted = [n.strip() for n in nodes.split(",") if n.strip()]
    if not wanted or any(n not in {f"N{i}" for i in range(1, 9)} | {"X"} for n in wanted):
        raise ValidationFailed("nodes must be a comma list of N1..N8 or X")
    return service.stack_for_nodes(db, p.tenant_id, wanted)


@router.get("/atlas/due-diligence/criteria", tags=["vendor atlas"])
def dd_criteria(request: Request, p: Principal = Depends(current_principal)):
    return {"threshold": request.app.state.settings.vendor_dd_min_score, "max_score": 5, "criteria": service.CRITERIA}


@router.get("/atlas/vendors/{atlas_id}", tags=["vendor atlas"])
def get_atlas_vendor(atlas_id: str, request: Request, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    v = service.get_atlas_vendor(db, p.tenant_id, atlas_id)
    dd = service.get_dd(db, p.tenant_id, v)
    conns = [c.connector_id for c in db.scalars(select(VendorConnector).where(VendorConnector.tenant_id == p.tenant_id)).all()
             if atlas_id in c.reference["atlas_ids"]]
    return {**service.vendor_view(v, dd, full=True), "connectors": conns,
            "due_diligence_detail": service.dd_view(dd, request.app.state.settings.vendor_dd_min_score)}


class OutreachIn(BaseModel):
    status: str
    note: str = ""


@router.put("/atlas/vendors/{atlas_id}/outreach", tags=["vendor atlas"])
def set_outreach(atlas_id: str, body: OutreachIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    v = service.set_outreach(db, p, atlas_id, body.status, body.note)
    db.commit()
    return {"atlas_id": v.atlas_id, "outreach_status": v.outreach_status}


class AssessIn(BaseModel):
    score: int = Field(ge=0, le=5)
    passed: bool
    evidence: list[str]
    notes: str = ""


@router.put("/atlas/vendors/{atlas_id}/due-diligence/{criterion}", tags=["vendor atlas"])
def assess(atlas_id: str, criterion: str, body: AssessIn, request: Request, p: Principal = Depends(current_principal),
           db: Session = Depends(get_db)):
    dd = service.assess(db, p, atlas_id, criterion, body.score, body.passed, body.evidence, body.notes)
    db.commit()
    return service.dd_view(dd, request.app.state.settings.vendor_dd_min_score)


class DecisionIn(BaseModel):
    decision: str
    notes: str = ""


@router.post("/atlas/vendors/{atlas_id}/due-diligence/decision", tags=["vendor atlas"])
def dd_decide(atlas_id: str, body: DecisionIn, request: Request, p: Principal = Depends(current_principal),
              db: Session = Depends(get_db)):
    settings = request.app.state.settings
    dd = service.decide(db, p, atlas_id, body.decision, body.notes, settings.vendor_dd_min_score)
    db.commit()
    return service.dd_view(dd, settings.vendor_dd_min_score)


class PromoteIn(BaseModel):
    connector_id: str | None = None
    endpoint: str = ""
    handoff_format: str = ""


@router.post("/atlas/vendors/{atlas_id}/promote", tags=["vendor atlas"], status_code=201)
def promote(atlas_id: str, body: PromoteIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    vendor = service.promote(db, p, atlas_id, body.connector_id, body.endpoint, body.handoff_format)
    db.commit()
    return engagement_view(vendor) | {"atlas_vendor_id": vendor.atlas_vendor_id, "connector_id": vendor.connector_id}


# --- connector registry ------------------------------------------------------------------

@router.get("/vendor-connectors", tags=["vendor connectors"])
def list_connectors(node: str | None = None, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(VendorConnector).where(VendorConnector.tenant_id == p.tenant_id)
                      .order_by(VendorConnector.connector_id)).all()
    return [connectors.connector_view(db, p.tenant_id, c) for c in rows if not node or c.reference["node"] == node]


@router.post("/vendor-connectors/seed", tags=["vendor connectors"])
def seed(p: Principal = Depends(require_roles("programme_manager")), db: Session = Depends(get_db)):
    added = connectors.seed_connectors(db, p.tenant_id)
    db.commit()
    return {"added": added, "total": len(connectors.REFERENCE)}


@router.get("/vendor-connectors/{connector_id}", tags=["vendor connectors"])
def get_connector(connector_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return connectors.connector_view(db, p.tenant_id, connectors.get_connector(db, p.tenant_id, connector_id), full=True)


class EntitlementIn(BaseModel):
    evidence: list[str]


@router.post("/vendor-connectors/{connector_id}/entitlement", tags=["vendor connectors"])
def entitlement(connector_id: str, body: EntitlementIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    c = connectors.verify_entitlement(db, p, connector_id, body.evidence)
    db.commit()
    return connectors.connector_view(db, p.tenant_id, c)


@router.post("/vendor-connectors/{connector_id}/activate", tags=["vendor connectors"])
def activate(connector_id: str, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    c, secret = connectors.activate(db, p, connector_id)
    db.commit()
    return {**connectors.connector_view(db, p.tenant_id, c), "webhook_secret": secret,
            "callback_path": f"/vendor-callbacks/{p.tenant_id}/{connector_id}",
            "note": "Store the webhook secret now; it is not shown again."}


class SuspendIn(BaseModel):
    reason: str = Field(min_length=1)


@router.post("/vendor-connectors/{connector_id}/suspend", tags=["vendor connectors"])
def suspend(connector_id: str, body: SuspendIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    c = connectors.suspend(db, p, connector_id, body.reason)
    db.commit()
    return connectors.connector_view(db, p.tenant_id, c)


class RunIn(BaseModel):
    work_package: dict
    idempotency_key: str


@router.post("/vendor-connectors/{connector_id}/run", tags=["vendor jobs"], status_code=201)
def run(connector_id: str, body: RunIn, request: Request, p: Principal = Depends(current_principal),
        db: Session = Depends(get_db)):
    job = jobs.run_job(db, p, request.app.state.settings, connector_id, body.work_package, body.idempotency_key)
    db.commit()
    return jobs.job_view(job)


# --- vendor callbacks (HMAC, no user session) ------------------------------------------------

@router.post("/vendor-callbacks/{tenant_id}/{connector_id}", tags=["vendor jobs"])
def vendor_callback(tenant_id: str, connector_id: str, request: Request,
                    cb: VerifiedCallback = Depends(verified_vendor_callback), db: Session = Depends(get_db)):
    try:
        payload = json.loads(cb.body)
    except ValueError:
        raise ValidationFailed("callback body must be JSON")
    if not isinstance(payload, dict):
        raise ValidationFailed("callback body must be a JSON object")
    result = jobs.receive_callback(db, request.app.state.settings, cb.tenant_id, cb.connector_id, payload)
    db.commit()
    return result


# --- vendor jobs ----------------------------------------------------------------------------

@router.get("/vendor-jobs", tags=["vendor jobs"])
def list_jobs(status: str | None = None, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    q = select(VendorJob).where(VendorJob.tenant_id == p.tenant_id)
    if status:
        q = q.where(VendorJob.status == status)
    return [jobs.job_view(j) for j in db.scalars(q.order_by(VendorJob.id.desc()).limit(200)).all()]


@router.get("/vendor-jobs/{job_id}", tags=["vendor jobs"])
def get_job(job_id: int, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return jobs.job_view(jobs.get_job(db, p.tenant_id, job_id))


@router.post("/vendor-jobs/{job_id}/retry", tags=["vendor jobs"])
def retry(job_id: int, request: Request, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    job = jobs.retry_job(db, p, request.app.state.settings, job_id)
    db.commit()
    return jobs.job_view(job)


class ReviewIn(BaseModel):
    accept: bool
    notes: str = ""


@router.post("/vendor-jobs/{job_id}/review", tags=["vendor jobs"])
def review(job_id: int, body: ReviewIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    job = jobs.review(db, p, job_id, body.accept, body.notes)
    db.commit()
    return jobs.job_view(job)

"""Digital thread, LLM gateway, retrieval, extraction and evaluation endpoints."""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..ai import evals, gateway, retrieval
from ..ai.extraction import extract_rfq
from ..core import audit
from ..core.security import WRITER_ROLES, Principal, current_principal, require_roles
from ..db import get_db
from ..models import Artifact, ArtifactRevision, Document, LLMTrace, Tenant
from ..thread import service as thread

router = APIRouter()


class RevisionIn(BaseModel):
    kind: str
    key: str
    content: dict
    derived_from: list[int] = Field(default_factory=list)
    note: str = ""


@router.post("/thread/revisions", tags=["digital thread"], status_code=201)
def create_revision(body: RevisionIn, p: Principal = Depends(require_roles(*WRITER_ROLES)), db: Session = Depends(get_db)):
    rev = thread.create_revision(db, p.tenant_id, p.username, body.kind, body.key, body.content, body.derived_from, body.note)
    db.commit()
    return thread.revision_view(db, rev)


@router.get("/thread/artifacts", tags=["digital thread"])
def list_artifacts(kind: str | None = None, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    q = select(Artifact).where(Artifact.tenant_id == p.tenant_id)
    if kind:
        q = q.where(Artifact.kind == kind)
    out = []
    for a in db.scalars(q.order_by(Artifact.id)).all():
        latest = db.scalars(select(ArtifactRevision).where(ArtifactRevision.artifact_id == a.id)
                            .order_by(ArtifactRevision.rev.desc())).first()
        out.append({"id": a.id, "kind": a.kind, "key": a.key, "latest_revision_id": latest.id, "latest_rev": latest.rev})
    return out


@router.get("/thread/coverage", tags=["digital thread"])
def coverage(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return thread.coverage(db, p.tenant_id)


@router.get("/thread/revisions/{rid}", tags=["digital thread"])
def get_revision(rid: int, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return thread.revision_view(db, thread.get_revision(db, p.tenant_id, rid))


@router.get("/thread/revisions/{rid}/lineage", tags=["digital thread"])
def lineage(rid: int, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return thread.lineage(db, p.tenant_id, rid)


@router.get("/thread/revisions/{rid}/descendants", tags=["digital thread"])
def descendants(rid: int, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return thread.descendants(db, p.tenant_id, rid)


@router.get("/thread/revisions/{rid}/diff/{other}", tags=["digital thread"])
def diff(rid: int, other: int, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    a, b = thread.get_revision(db, p.tenant_id, rid), thread.get_revision(db, p.tenant_id, other)
    return {"from": rid, "to": other, "changes": thread.diff(a.content, b.content)}


class DocumentIn(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    text: str = Field(min_length=1, max_length=200_000)
    acl_roles: list[str] = Field(default_factory=list, description="Empty = every user in the tenant")


@router.post("/ai/documents", tags=["llm gateway"], status_code=201)
def add_document(body: DocumentIn, p: Principal = Depends(require_roles(*WRITER_ROLES)), db: Session = Depends(get_db)):
    doc = Document(tenant_id=p.tenant_id, title=body.title, text=body.text, acl_roles=body.acl_roles, created_by=p.username)
    db.add(doc)
    db.flush()
    flags = retrieval.injection_flags(body.text)
    audit.record(db, p.tenant_id, p.username, "document.added", "document", doc.id, {"injection_flags": len(flags)})
    db.commit()
    return {"id": doc.id, "title": doc.title, "acl_roles": doc.acl_roles, "injection_flags": flags}


@router.get("/ai/documents", tags=["llm gateway"])
def list_documents(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    docs = db.scalars(select(Document).where(Document.tenant_id == p.tenant_id).order_by(Document.id)).all()
    return [{"id": d.id, "title": d.title, "acl_roles": d.acl_roles} for d in docs if retrieval.permitted(d, p.roles)]


class SearchIn(BaseModel):
    query: str = Field(min_length=1)
    k: int = Field(4, ge=1, le=20)


@router.post("/ai/search", tags=["llm gateway"])
def search(body: SearchIn, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    return retrieval.search(db, p.tenant_id, p.roles, body.query, body.k)


class CompleteIn(BaseModel):
    task: str = Field("general", max_length=80)
    prompt: str = Field(min_length=1, max_length=100_000)
    use_retrieval: bool = False
    query: str | None = None
    k: int = Field(4, ge=1, le=20)


@router.post("/ai/complete", tags=["llm gateway"])
def complete(body: CompleteIn, request: Request, p: Principal = Depends(require_roles(*WRITER_ROLES)),
             db: Session = Depends(get_db)):
    return gateway.complete(db, request.app.state.settings, p, body.task, body.prompt,
                            use_retrieval=body.use_retrieval, query=body.query, k=body.k)


@router.get("/ai/traces", tags=["llm gateway"])
def traces(limit: int = 50, p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    rows = db.scalars(select(LLMTrace).where(LLMTrace.tenant_id == p.tenant_id)
                      .order_by(LLMTrace.id.desc()).limit(min(limit, 500))).all()
    return [{"id": t.id, "actor": t.actor, "task": t.task, "provider": t.provider, "model": t.model, "status": t.status,
             "input_tokens": t.input_tokens, "output_tokens": t.output_tokens, "cost_usd": t.cost_usd,
             "latency_ms": round(t.latency_ms, 2), "retrieved_doc_ids": t.retrieved_doc_ids, "flags": t.flags,
             "error": t.error, "at": t.created_at.isoformat()} for t in rows]


@router.get("/ai/budget", tags=["llm gateway"])
def budget(p: Principal = Depends(current_principal), db: Session = Depends(get_db)):
    tenant = db.get(Tenant, p.tenant_id)
    spent = gateway.spent_this_month(db, p.tenant_id)
    return {"limit_usd": tenant.llm_budget_usd, "spent_this_month_usd": float(spent),
            "remaining_usd": round(tenant.llm_budget_usd - float(spent), 6)}


class ExtractIn(BaseModel):
    text: str = Field(min_length=1, max_length=200_000)


@router.post("/ai/extract", tags=["extraction"])
def extract(body: ExtractIn, p: Principal = Depends(current_principal)):
    return extract_rfq(body.text)


@router.get("/ai/evals/extraction", tags=["evaluation"])
def eval_extraction(request: Request, p: Principal = Depends(current_principal)):
    return evals.evaluate_extraction(threshold=request.app.state.settings.extraction_accuracy_gate)


@router.get("/ai/evals/security", tags=["evaluation"])
def eval_security(request: Request, p: Principal = Depends(current_principal)):
    return evals.security_harness(request.app.state.settings, request.app)

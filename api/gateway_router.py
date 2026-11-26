"""FastAPI router for the ATHENA Vendor & AI I/O Data Quality Gateway.

Endpoints (reference API ids):
  A01 POST /api/v1/rfqs                                   customer RFQ (customer_rfq.v1)
  A03 POST /api/v1/projects/{project_id}/artifacts         any registered *.v1 artifact (JSON)
  A04 POST /api/v1/projects/{project_id}/artifact-upload   native files (CAD/FEA/PDF…), X-Content-SHA256 required
  A06 POST /api/v1/workflows/{event_id}/approve            human sign-off, HOLD → RELEASED (reviewer = caller)
  A08 POST /api/v1/vendor-connectors/{connector_id}/run    CON-001..CON-047 egress authorization (No Rights, No Access)
  A09 POST /api/v1/vendor-callbacks/{connector_id}         vendor webhook, HMAC-SHA256 signed, nonce replay-protected
  A21 POST /api/v1/lifecycle/events                        batch of lifecycle_event.v1 (EMS → N8, edge E-026)
  plus GET  /api/v1/decisions/{event_id}, /api/v1/dq/control-tower, /api/v1/schemas/{name}

Transport controls (in addition to every gateway rule):
  * Bearer token → (tenant_id, actor_id). Tenant and actor never come from the body.
  * ``Idempotency-Key`` is required on every POST. Same key + same body replays the stored
    response (``Idempotent-Replay: true``); same key + different body → 409.
  * Artifact bytes travel as base64 and are hashed server-side; ``X-Content-SHA256`` (required
    on A04, optional elsewhere) must match, and DQ05 checks them against ``output_file_sha256``.
  * A09: ``X-Athena-Signature: sha256=<hex HMAC(secret, f"{ts}.{nonce}." + raw_body)>`` with
    ``X-Athena-Timestamp`` (unix seconds, ±300 s) and a single-use ``X-Athena-Nonce``.

HTTP status: RELEASED → 201, HOLD → 202, REWORK/BLOCK/QUARANTINE → 422 (decision in the body).

Run:  ATHENA_DQ_TOKENS='{"<token>": {"tenant_id": "demo", "actor_id": "demo.engineer"}}' \\
      uvicorn api.gateway_router:create_app_from_env --factory
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from typing import Any, Callable, Optional

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rag.dq_gateway import SCHEMAS, ArtifactSubmission, DQGateway, DQGatewayError, GateDecision

_STATUS_BY_GATE = {"RELEASED": 201, "HOLD": 202}
MAX_BATCH = 500


# ----------------------------------------------------------------------------------------------
# request models
# ----------------------------------------------------------------------------------------------
class SubmissionIn(BaseModel):
    """Wire form of an ArtifactSubmission. Approval and simulator flags are never accepted here."""
    model_config = ConfigDict(extra="forbid")

    manifest: dict[str, Any]
    payload: dict[str, Any] = {}
    content_b64: str = Field(description="base64 of the artifact bytes hashed into output_file_sha256")
    files_b64: dict[str, str] = Field(default_factory=dict, description="native files named in payload.files")
    required_critical: list[str] = []
    side_effect: Optional[str] = None
    use_for_training: bool = False
    ai_provenance: Optional[dict[str, Any]] = None


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    notes: str = ""


class ConnectorRunIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    project_id: str
    artifact_refs: list[str]


class LifecycleBatchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    events: list[SubmissionIn] = Field(min_length=1, max_length=MAX_BATCH)


# ----------------------------------------------------------------------------------------------
# auth, idempotency, replay protection
# ----------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Principal:
    tenant_id: str
    actor_id: str
    roles: frozenset = frozenset()


@dataclass
class ConnectorSecret:
    secret: str
    tenant_id: str
    actor_id: Optional[str] = None


class IdempotencyStore:
    """(tenant, method+path, key) → (sha256 of body, status, response JSON). In-memory, LRU-bounded."""

    def __init__(self, max_entries: int = 10_000) -> None:
        self._data: OrderedDict = OrderedDict()
        self._locks: dict = {}
        self._guard = threading.Lock()
        self.max_entries = max_entries

    def lock_for(self, key: tuple) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def get(self, key: tuple) -> Optional[tuple[str, int, Any]]:
        with self._guard:
            return self._data.get(key)

    def put(self, key: tuple, body_hash: str, status: int, content: Any) -> None:
        with self._guard:
            self._data[key] = (body_hash, status, content)
            self._data.move_to_end(key)
            while len(self._data) > self.max_entries:
                old, _ = self._data.popitem(last=False)
                self._locks.pop(old, None)


class NonceCache:
    def __init__(self, ttl_s: int) -> None:
        self.ttl_s, self._seen, self._lock = ttl_s, {}, threading.Lock()

    def use(self, connector_id: str, nonce: str, now: float) -> bool:
        with self._lock:
            for k in [k for k, t in self._seen.items() if now - t > self.ttl_s]:
                del self._seen[k]
            if (connector_id, nonce) in self._seen:
                return False
            self._seen[(connector_id, nonce)] = now
            return True


def sign_callback(secret: str, timestamp: str, nonce: str, body: bytes) -> str:
    """Signature a vendor sends in X-Athena-Signature (also used by tests and connector SDKs)."""
    mac = hmac.new(secret.encode(), f"{timestamp}.{nonce}.".encode() + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def _b64(value: str, what: str) -> bytes:
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(400, f"{what} is not valid base64")


def _to_submission(body: SubmissionIn, transport: str, header_sha: Optional[str], require_sha: bool) -> ArtifactSubmission:
    content = _b64(body.content_b64, "content_b64")
    digest = hashlib.sha256(content).hexdigest()
    if require_sha and not header_sha:
        raise HTTPException(400, "X-Content-SHA256 header is required")
    if header_sha and not hmac.compare_digest(header_sha.lower(), digest):
        raise HTTPException(400, f"X-Content-SHA256 does not match the uploaded bytes (got {digest})")
    files = {name: _b64(v, f"files_b64[{name}]") for name, v in body.files_b64.items()}
    try:
        return ArtifactSubmission(manifest=body.manifest, payload=body.payload, content=content, files=files,
                                  required_critical=body.required_critical, side_effect=body.side_effect,
                                  use_for_training=body.use_for_training, ai_provenance=body.ai_provenance,
                                  transport=transport)
    except ValidationError as exc:
        raise HTTPException(422, json.loads(exc.json(include_url=False)))


def _decision_response(d: GateDecision) -> tuple[int, dict]:
    return _STATUS_BY_GATE.get(d.quality_gate, 422), d.to_dict()


# ----------------------------------------------------------------------------------------------
# router
# ----------------------------------------------------------------------------------------------
def create_gateway_router(gateway: DQGateway, tokens: dict[str, dict], *,
                          connector_secrets: Optional[dict[str, dict]] = None,
                          idempotency: Optional[IdempotencyStore] = None,
                          callback_skew_s: int = 300,
                          clock: Callable[[], float] = time.time) -> APIRouter:
    principals = {tok: Principal(v["tenant_id"], v["actor_id"], frozenset(v.get("roles", ())))
                  for tok, v in tokens.items()}
    secrets_ = {cid: ConnectorSecret(**v) for cid, v in (connector_secrets or {}).items()}
    store = idempotency or IdempotencyStore()
    nonces = NonceCache(ttl_s=2 * callback_skew_s)
    router = APIRouter(prefix="/api/v1", tags=["dq-gateway"])

    def principal(authorization: Optional[str] = Header(default=None)) -> Principal:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(401, "bearer token required", headers={"WWW-Authenticate": "Bearer"})
        presented = authorization[7:].strip()
        for tok, p in principals.items():
            if hmac.compare_digest(tok.encode(), presented.encode()):
                return p
        raise HTTPException(401, "invalid token", headers={"WWW-Authenticate": "Bearer"})

    async def idempotent(request: Request, tenant_id: str, key: Optional[str],
                         handler: Callable[[], tuple[int, Any]]) -> JSONResponse:
        if not key or len(key) > 255:
            raise HTTPException(400, "Idempotency-Key header is required (1–255 chars)")
        raw = await request.body()
        body_hash = hashlib.sha256(raw).hexdigest()
        k = (tenant_id, f"{request.method} {request.url.path}", key)
        with store.lock_for(k):
            prior = store.get(k)
            if prior is not None:
                if prior[0] != body_hash:
                    raise HTTPException(409, "Idempotency-Key was already used with a different request body")
                return JSONResponse(prior[2], status_code=prior[1], headers={"Idempotent-Replay": "true"})
            status, content = handler()
            store.put(k, body_hash, status, content)
            return JSONResponse(content, status_code=status)

    def submit(sub: ArtifactSubmission, p: Principal) -> tuple[int, dict]:
        return _decision_response(gateway.submit(sub, p.tenant_id, p.actor_id))

    # A01 ------------------------------------------------------------------------------------
    @router.post("/rfqs", summary="A01 submit a customer RFQ", status_code=201)
    async def a01_rfq(body: SubmissionIn, request: Request, p: Principal = Depends(principal),
                      idempotency_key: Optional[str] = Header(default=None),
                      x_content_sha256: Optional[str] = Header(default=None)):
        if body.manifest.get("output_schema") != "customer_rfq.v1":
            raise HTTPException(422, "A01 accepts customer_rfq.v1 only")
        sub = _to_submission(body, "HTTP", x_content_sha256, require_sha=False)
        return await idempotent(request, p.tenant_id, idempotency_key, lambda: submit(sub, p))

    # A03 ------------------------------------------------------------------------------------
    @router.post("/projects/{project_id}/artifacts", summary="A03 submit an artifact", status_code=201)
    async def a03_artifact(project_id: str, body: SubmissionIn, request: Request, p: Principal = Depends(principal),
                           idempotency_key: Optional[str] = Header(default=None),
                           x_content_sha256: Optional[str] = Header(default=None)):
        if body.manifest.get("project_id") != project_id:
            raise HTTPException(400, "manifest.project_id does not match the path")
        sub = _to_submission(body, "HTTP", x_content_sha256, require_sha=False)
        return await idempotent(request, p.tenant_id, idempotency_key, lambda: submit(sub, p))

    # A04 ------------------------------------------------------------------------------------
    @router.post("/projects/{project_id}/artifact-upload", summary="A04 upload native artifact files", status_code=201)
    async def a04_upload(project_id: str, body: SubmissionIn, request: Request, p: Principal = Depends(principal),
                         idempotency_key: Optional[str] = Header(default=None),
                         x_content_sha256: Optional[str] = Header(default=None)):
        if body.manifest.get("project_id") != project_id:
            raise HTTPException(400, "manifest.project_id does not match the path")
        sub = _to_submission(body, "UPLOAD", x_content_sha256, require_sha=True)
        return await idempotent(request, p.tenant_id, idempotency_key, lambda: submit(sub, p))

    # A06 ------------------------------------------------------------------------------------
    @router.post("/workflows/{event_id}/approve", summary="A06 approve a HOLD artifact (reviewer = caller)")
    async def a06_approve(event_id: str, body: ApproveIn, request: Request, p: Principal = Depends(principal),
                          idempotency_key: Optional[str] = Header(default=None)):
        def run() -> tuple[int, dict]:
            if gateway.decision(event_id, p.tenant_id) is None:
                raise HTTPException(404, f"decision {event_id} not found")
            try:
                d = gateway.approve(event_id, p.actor_id, p.tenant_id, notes=body.notes)
            except DQGatewayError as exc:
                msg = str(exc)
                raise HTTPException(409 if "only HOLD" in msg else 403, msg)
            return (200 if d.quality_gate == "RELEASED" else 422), d.to_dict()
        return await idempotent(request, p.tenant_id, idempotency_key, run)

    # A08 ------------------------------------------------------------------------------------
    @router.post("/vendor-connectors/{connector_id}/run", summary="A08 authorize a vendor connector run")
    async def a08_connector(connector_id: str, body: ConnectorRunIn, request: Request,
                            p: Principal = Depends(principal), idempotency_key: Optional[str] = Header(default=None)):
        def run() -> tuple[int, dict]:
            auth = gateway.authorize_connector(connector_id, body.project_id, body.artifact_refs, p.tenant_id)
            out = asdict(auth)
            if auth.ok:
                out["egress"] = [{"artifact_ref": r, "no_train": gateway.registry.get(r).rights == "APPROVED_NO_TRAIN"}
                                 for r in body.artifact_refs]
            return (200 if auth.ok else 403), out
        return await idempotent(request, p.tenant_id, idempotency_key, run)

    # A09 ------------------------------------------------------------------------------------
    @router.post("/vendor-callbacks/{connector_id}", summary="A09 signed vendor callback",
                 openapi_extra={"requestBody": {"required": True, "content": {
                     "application/json": {"schema": SubmissionIn.model_json_schema()}}}})
    async def a09_callback(connector_id: str, request: Request,
                           idempotency_key: Optional[str] = Header(default=None),
                           x_athena_timestamp: Optional[str] = Header(default=None),
                           x_athena_nonce: Optional[str] = Header(default=None),
                           x_athena_signature: Optional[str] = Header(default=None),
                           x_content_sha256: Optional[str] = Header(default=None)):
        cs = secrets_.get(connector_id)
        if cs is None:
            raise HTTPException(401, "unknown connector")
        if not (x_athena_timestamp and x_athena_nonce and x_athena_signature):
            raise HTTPException(401, "X-Athena-Timestamp, X-Athena-Nonce and X-Athena-Signature are required")
        raw = await request.body()
        expected = sign_callback(cs.secret, x_athena_timestamp, x_athena_nonce, raw)
        if not hmac.compare_digest(expected, x_athena_signature):
            raise HTTPException(401, "invalid signature")
        try:
            ts = float(x_athena_timestamp)
        except ValueError:
            raise HTTPException(401, "X-Athena-Timestamp must be unix seconds")
        now = clock()
        if abs(now - ts) > callback_skew_s:
            raise HTTPException(401, "callback timestamp outside the allowed window")
        try:
            body = SubmissionIn.model_validate_json(raw)
        except ValidationError as exc:
            raise HTTPException(422, json.loads(exc.json(include_url=False)))
        # replay check last, so a rejected request does not burn the nonce
        if not nonces.use(connector_id, x_athena_nonce, now):
            raise HTTPException(409, "nonce already used (replay)")
        sub = _to_submission(body, "WEBHOOK", x_content_sha256, require_sha=False)
        actor = cs.actor_id or f"vendor:{connector_id}"
        return await idempotent(request, cs.tenant_id, idempotency_key,
                                lambda: _decision_response(gateway.submit(sub, cs.tenant_id, actor)))

    # A21 ------------------------------------------------------------------------------------
    @router.post("/lifecycle/events", summary="A21 batch of lifecycle events")
    async def a21_lifecycle(body: LifecycleBatchIn, request: Request, p: Principal = Depends(principal),
                            idempotency_key: Optional[str] = Header(default=None)):
        bad = [i for i, e in enumerate(body.events) if e.manifest.get("output_schema") != "lifecycle_event.v1"]
        if bad:
            raise HTTPException(422, f"events {bad} are not lifecycle_event.v1")
        subs = [_to_submission(e, "HTTP", None, require_sha=False) for e in body.events]

        def run() -> tuple[int, dict]:
            results = [gateway.submit(s, p.tenant_id, p.actor_id).to_dict() for s in subs]
            summary: dict[str, int] = {}
            for r in results:
                summary[r["quality_gate"]] = summary.get(r["quality_gate"], 0) + 1
            return 200, {"summary": summary, "results": results}
        return await idempotent(request, p.tenant_id, idempotency_key, run)

    # read-only ----------------------------------------------------------------------------
    @router.get("/decisions/{event_id}")
    def get_decision(event_id: str, p: Principal = Depends(principal)):
        d = gateway.decision(event_id, p.tenant_id)
        if d is None:
            raise HTTPException(404, f"decision {event_id} not found")
        return d.to_dict()

    @router.get("/dq/control-tower")
    def control_tower(p: Principal = Depends(principal)):
        from dashboard.components.dq_control_tower import control_tower_data
        return json.loads(json.dumps(control_tower_data(gateway, p.tenant_id), default=str))

    @router.get("/schemas/{name}")
    def schema(name: str):
        spec = SCHEMAS.get(name)
        if spec is None:
            raise HTTPException(404, f"unknown schema {name}")
        return {"name": name, "node": spec.node, "critical": list(spec.critical),
                "json_schema": spec.model.model_json_schema()}

    return router


def create_app(gateway: Optional[DQGateway] = None, tokens: Optional[dict[str, dict]] = None, **kw: Any) -> FastAPI:
    gw = gateway or DQGateway()
    app = FastAPI(title="ATHENA DQ Gateway", version="1.0")
    app.state.gateway = gw
    app.include_router(create_gateway_router(gw, tokens or {}, **kw))
    return app


def create_app_from_env() -> FastAPI:
    """uvicorn factory. ATHENA_DQ_TOKENS / ATHENA_CONNECTOR_SECRETS are JSON objects;
    ATHENA_DQ_DEMO=1 seeds the synthetic demo tenant; ATHENA_DQ_REGISTRY persists the registry."""
    from rag.dq_gateway import ArtifactRegistry
    tokens = json.loads(os.environ.get("ATHENA_DQ_TOKENS", "{}"))
    if not tokens:
        raise RuntimeError("ATHENA_DQ_TOKENS is empty: no caller could authenticate")
    secrets_ = json.loads(os.environ.get("ATHENA_CONNECTOR_SECRETS", "{}"))
    gw = DQGateway(registry=ArtifactRegistry(os.environ.get("ATHENA_DQ_REGISTRY") or None))
    if os.environ.get("ATHENA_DQ_DEMO") == "1":
        from rag.dq_gateway.demo import seed_demo
        seed_demo(gw)
    return create_app(gw, tokens, connector_secrets=secrets_)

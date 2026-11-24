"""Shared fixtures: an isolated N0 app per test (temp SQLite + temp object store), two tenants,
one bearer token per role, and helpers that keep SHA-256 seals consistent."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.rules import canonical_json, sha256_hex

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"
RFQ_SOURCE = "SRC-ACME-RFQ-PORTAL"


def _p(pid: str, tenant: str, *roles: str, **kw: Any) -> dict:
    return {"principal_id": pid, "tenant_id": tenant, "roles": list(roles), **kw}


TOKENS = {
    "tok-alice": _p("alice", "acme", "producer"),
    "tok-bob": _p("bob", "acme", "producer", clearance="INTERNAL"),
    "tok-sam": _p("sam", "acme", "source_admin"),
    "tok-rita": _p("rita", "acme", "rights_approver"),
    "tok-rex": _p("rex", "acme", "gateway_reviewer"),
    "tok-rm": _p("rm", "acme", "release_manager"),
    "tok-aud": _p("aud", "acme", "auditor"),
    "tok-n2": _p("svc-n2", "acme", "node_service", nodes=["N2"]),
    "tok-pat": _p("pat", "acme", "producer", "release_manager", "gateway_reviewer"),
    "tok-gp": _p("gina", "globex", "producer"),
    "tok-gadmin": _p("gadmin", "globex", "source_admin"),
    "tok-grights": _p("grights", "globex", "rights_approver"),
}


def load_example(name: str) -> dict:
    return json.loads((EXAMPLES / name).read_text())


def reseal(artifact: dict) -> dict:
    """Recompute source_sha256 for a payload-only artifact after a test edits the payload."""
    if not artifact.get("file_refs"):
        artifact["source_sha256"] = sha256_hex(canonical_json(artifact["payload"]))
    return artifact


def rfq(artifact_id: str = "RFQ-T-1", revision: str = "A", **overrides: Any) -> dict:
    a = copy.deepcopy(load_example("valid_rfq.json"))
    a.update(artifact_id=artifact_id, revision=revision, **overrides)
    a["payload"]["rfq_id"] = artifact_id
    return reseal(a)


def set_req(artifact: dict, parameter: str, **changes: Any) -> dict:
    for r in artifact["payload"]["requirements"]:
        if r["parameter"] == parameter:
            r.update(changes)
    return reseal(artifact)


def drop_req(artifact: dict, parameter: str) -> dict:
    artifact["payload"]["requirements"] = [r for r in artifact["payload"]["requirements"]
                                           if r["parameter"] != parameter]
    return reseal(artifact)


def codes(result: dict) -> set[str]:
    return {v["code"] for v in result["decision"]["violated_rules"]}


class N0:
    def __init__(self, client: TestClient) -> None:
        self.c = client

    @staticmethod
    def h(who: Optional[str]) -> dict:
        return {"Authorization": f"Bearer tok-{who}"} if who else {}

    def post(self, path: str, who: Optional[str] = "alice", **kw: Any):
        return self.c.post(path, headers=self.h(who), **kw)

    def get(self, path: str, who: Optional[str] = "aud", **kw: Any):
        return self.c.get(path, headers=self.h(who), **kw)

    def source(self, source_id: str = RFQ_SOURCE, tenant: str = "acme", approve: bool = True,
               permitted_use: tuple = ("RFQ_ANALYSIS", "PRODUCT_DEFINITION", "LLM_EXTRACTION"), **kw: Any) -> dict:
        admin, approver = ("sam", "rita") if tenant == "acme" else ("gadmin", "grights")
        body = {"source_id": source_id, "tenant_id": tenant, "publisher": "Acme Industrial Controls",
                "license": "LicenseRef-Acme-MNDA-2026", "permitted_use": list(permitted_use), **kw}
        r = self.post("/v1/sources/register", admin, json=body)
        assert r.status_code == 201, r.text
        if approve:
            r = self.post(f"/v1/sources/{tenant}/{source_id}/approve", approver,
                          json={"decision": "APPROVED", "reason": "mutual NDA countersigned"})
            assert r.status_code == 200, r.text
            return r.json()["source"]
        return r.json()

    def upload(self, data: bytes, filename: str = "rfq.txt", source_id: str = RFQ_SOURCE, tenant: str = "acme",
               who: str = "alice", mime: str = "text/plain", **form: Any):
        return self.post("/v1/artifacts/upload", who, data={"tenant_id": tenant, "source_id": source_id, **form},
                         files={"file": (filename, data, mime)})

    def evaluate(self, artifact: dict, who: str = "alice", **kw: Any) -> dict:
        r = self.post("/v1/gateway/evaluate", who, json={"artifact": artifact, **kw})
        assert r.status_code == 200, r.text
        return r.json()

    def approve_release(self, artifact_id: str, revision: str = "A", who: str = "rm", decision: str = "APPROVED"):
        return self.post("/v1/approvals", who, json={"artifact_id": artifact_id, "exact_revision": revision,
                                                     "scope": "RELEASE", "decision": decision,
                                                     "reason": "reviewed for downstream costing"})

    def resolve_review(self, artifact_id: str, revision: str = "A", who: str = "rex", decision: str = "APPROVED"):
        return self.post("/v1/approvals", who, json={"artifact_id": artifact_id, "exact_revision": revision,
                                                     "scope": "REVIEW_RESOLUTION", "decision": decision,
                                                     "reason": "values checked against the customer PDF"})

    def release(self, artifact_id: str, revision: str = "A", targets: tuple = ("N2",), who: str = "rm",
                source_node: str = "N0", **kw: Any):
        return self.post("/v1/handoffs/release", who, json={"artifact_id": artifact_id, "revision": revision,
                                                            "source_node": source_node,
                                                            "target_nodes": list(targets), **kw})


@pytest.fixture
def make_n0(tmp_path):
    counter = {"n": 0}

    def factory(provider=None, **overrides: Any) -> N0:
        counter["n"] += 1
        d = tmp_path / f"n0-{counter['n']}"
        settings = Settings(database_url=f"sqlite:///{d}/n0.db", storage_root=str(d / "objects"), tokens=TOKENS,
                            **{"llm_provider": "deterministic", **overrides})
        app = create_app(settings, provider=provider)
        n0 = N0(TestClient(app))
        n0.app = app
        return n0
    return factory


@pytest.fixture
def n0(make_n0) -> N0:
    return make_n0()


@pytest.fixture
def n0_src(n0) -> N0:
    """App with the example RFQ source registered and APPROVED."""
    n0.source()
    return n0

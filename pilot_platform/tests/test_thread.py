import pytest

from app.core.errors import ValidationFailed
from app.models import Tenant
from app.thread import service as thread


def test_non_root_kinds_need_lineage(client, auth):
    r = client.post("/thread/revisions", json={"kind": "PRD", "key": "P", "content": {}}, headers=auth("engineer"))
    assert r.status_code == 422
    assert client.post("/thread/revisions", json={"kind": "RFQ", "key": "P", "content": {}},
                       headers=auth("viewer")).status_code == 403


def test_revisions_lineage_diff_and_idempotency(client, auth):
    h = auth("engineer")
    rfq = client.post("/thread/revisions", json={"kind": "RFQ", "key": "R", "content": {"qty": 10}}, headers=h).json()
    prd = client.post("/thread/revisions", json={"kind": "PRD", "key": "R", "content": {"reqs": ["a"]},
                                               "derived_from": [rfq["id"]]}, headers=h).json()
    again = client.post("/thread/revisions", json={"kind": "PRD", "key": "R", "content": {"reqs": ["a"]},
                                                 "derived_from": [rfq["id"]]}, headers=h).json()
    assert again["id"] == prd["id"] and again["rev"] == 1

    prd2 = client.post("/thread/revisions", json={"kind": "PRD", "key": "R", "content": {"reqs": ["a", "b"]}}, headers=h).json()
    assert prd2["rev"] == 2 and prd2["parents"] == [prd["id"]]  # auto-linked to previous revision

    lineage = client.get(f"/thread/revisions/{prd2['id']}/lineage", headers=h).json()
    assert lineage["kinds"] == ["PRD", "RFQ"] and {a["id"] for a in lineage["ancestors"]} == {prd["id"], rfq["id"]}
    desc = client.get(f"/thread/revisions/{rfq['id']}/descendants", headers=h).json()
    assert {d["id"] for d in desc} == {prd["id"], prd2["id"]}
    changes = client.get(f"/thread/revisions/{prd['id']}/diff/{prd2['id']}", headers=h).json()["changes"]
    assert changes == [{"path": "reqs", "op": "changed", "old": ["a"], "new": ["a", "b"]}]
    cov = client.get("/thread/coverage", headers=h).json()
    assert cov["coverage"] == 1.0 and cov["non_root_revisions"] == 2


def test_lineage_cannot_cross_tenants(db):
    db.add(Tenant(id="other", name="Other"))
    db.flush()
    foreign = thread.create_revision(db, "other", "x", "RFQ", "theirs", {"raw_text": "secret"})
    with pytest.raises(ValidationFailed, match="cross-tenant"):
        thread.create_revision(db, "pilot", "x", "PRD", "ours", {"x": 1}, derived_from=[foreign.id])
    with pytest.raises(Exception):
        thread.get_revision(db, "pilot", foreign.id)


def test_unknown_kind_rejected(db):
    with pytest.raises(ValidationFailed):
        thread.create_revision(db, "pilot", "x", "INVOICE", "k", {})

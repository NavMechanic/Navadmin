import pytest
from sqlalchemy import text

from app.config import Settings
from app.core import audit
from app.models import ArtifactRevision, ImmutableRowError, User
from app.thread import service as thread


def test_authentication_required_and_validated(client):
    assert client.get("/programmes").status_code == 401
    assert client.get("/programmes", headers={"Authorization": "Bearer not-a-jwt"}).status_code == 401
    assert client.post("/auth/login", json={"username": "pm", "password": "wrong"}).status_code == 401
    assert client.get("/health").status_code == 200


def test_disabled_user_is_rejected_even_with_valid_token(client, auth, db):
    headers = auth("analyst")
    assert client.get("/auth/me", headers=headers).status_code == 200
    db.query(User).filter(User.username == "analyst").update({"active": False})
    db.commit()
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_only_admin_manages_users(client, auth):
    body = {"username": "new.engineer", "password": "a-long-password-1", "roles": ["engineer"]}
    assert client.post("/admin/users", json=body, headers=auth("pm")).status_code == 403
    assert client.post("/admin/users", json=body, headers=auth("admin")).status_code == 201
    assert client.post("/auth/login", json={"username": "new.engineer", "password": "a-long-password-1"}).status_code == 200
    bad = {**body, "username": "x.bad", "roles": ["superuser"]}
    assert client.post("/admin/users", json=bad, headers=auth("admin")).status_code == 422


def test_every_non_public_route_requires_auth(client, auth):
    report = client.get("/ai/evals/security", headers=auth("viewer")).json()
    assert report["checks"]["all_routes_authenticated"]["passed"] is True


def test_audit_chain_detects_tampering(client, auth, db, pid):
    client.post(f"/programmes/{pid}/activities/A/start", headers=auth("founder"))
    ok = client.get("/audit/verify", headers=auth("pm")).json()
    assert ok["valid"] is True and ok["events"] >= 2
    db.execute(text("UPDATE audit_events SET actor = 'mallory' WHERE seq = 1"))  # raw SQL bypasses the ORM guard
    db.commit()
    bad = client.get("/audit/verify", headers=auth("pm")).json()
    assert bad["valid"] is False and bad["broken_at_seq"] == 1


def test_artifact_revisions_and_audit_rows_are_immutable(db):
    rev = thread.create_revision(db, "pilot", "t", "RFQ", "imm", {"raw_text": "a"})
    db.commit()
    rev.content = {"raw_text": "changed"}
    with pytest.raises(ImmutableRowError):
        db.flush()
    db.rollback()
    ev = audit.record(db, "pilot", "t", "x", "y", 1)
    db.commit()
    db.delete(ev)
    with pytest.raises(ImmutableRowError):
        db.flush()


def test_production_settings_refuse_unsafe_defaults():
    with pytest.raises(RuntimeError, match="JWT"):
        Settings(env="production").validate()
    with pytest.raises(RuntimeError, match="SEED_DEMO"):
        Settings(env="production", jwt_secret="x" * 40).validate()
    Settings(env="production", jwt_secret="x" * 40, seed_demo=False).validate()

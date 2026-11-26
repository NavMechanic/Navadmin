"""ECN/ECO workflow, impact analysis and the vault webhook."""
from __future__ import annotations

import hashlib
import json

import pytest

CELL = "PB-CEL-10K3S"
ALT_CELL = "PB-CEL-10K4S"


@pytest.fixture(scope="module")
def alternate_cell(client, api, engineer):
    """A qualified alternate to substitute to."""
    client.post(f"{api}/bom/parts", headers=engineer, json={
        "part_number": ALT_CELL, "name": "Li-polymer cell pack (alternate vendor)",
        "part_type": "COMPONENT", "supply_mode": "SKD_IMPORT",
        "supplier": "Vendor M — Battery Cells", "sourcing_status": "Alternate qualified",
        "safety_critical": True})
    client.post(f"{api}/bom/parts/{ALT_CELL}/revisions", headers=engineer,
                json={"rev": "A", "unit_cost_usd": "2.18",
                      "change_summary": "Alternate vendor qualification build"})
    client.post(f"{api}/bom/parts/{ALT_CELL}/revisions/A/release", headers=engineer)
    return ALT_CELL


@pytest.fixture(scope="module")
def cell_swap(client, api, engineer, alternate_cell) -> dict:
    """The specification's example: a battery cell swap."""
    r = client.post(f"{api}/ecn", headers=engineer, json={
        "title": "Substitute powerbank cell pack to the alternate vendor",
        "reason": "Primary cell vendor lead time moved to 16 weeks against a 10-week "
                  "plan; qualify the alternate to protect the festive ramp.",
        "category": "SUPPLY_CONTINUITY", "primary_sku": "PB-10K-PKD",
        "affected_items": [{"part_number": CELL, "change_type": "SUBSTITUTE",
                            "replacement_part_number": ALT_CELL,
                            "detail": "Same form factor and capacity."}]})
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------ creation
def test_ecn_is_numbered_and_starts_in_draft(cell_swap):
    assert cell_swap["ecn_number"].startswith("ECN-")
    assert cell_swap["state"] == "DRAFT"
    assert cell_swap["impact_analysed_at"] is not None


def test_substitute_without_a_replacement_is_refused(client, api, engineer):
    """A substitution with no replacement cannot be costed, qualified or ordered."""
    r = client.post(f"{api}/ecn", headers=engineer, json={
        "title": "Swap the cell for something", "reason": "Lead time is unacceptable.",
        "affected_items": [{"part_number": CELL, "change_type": "SUBSTITUTE"}]})
    assert r.status_code == 422
    assert "replacement_part_number" in r.text


def test_replacement_must_already_exist(client, api, engineer):
    r = client.post(f"{api}/ecn", headers=engineer, json={
        "title": "Swap to a part nobody has created",
        "reason": "Testing that a phantom replacement is refused.",
        "affected_items": [{"part_number": CELL, "change_type": "SUBSTITUTE",
                            "replacement_part_number": "PB-CEL-PHANTOM"}]})
    assert r.status_code == 422
    assert "does not exist" in r.json()["detail"]


def test_date_effectivity_requires_a_date(client, api, engineer):
    r = client.post(f"{api}/ecn", headers=engineer, json={
        "title": "Change with a date effectivity and no date",
        "reason": "Should be rejected by the schema.",
        "effectivity_type": "DATE",
        "affected_items": [{"part_number": CELL, "change_type": "SPEC_CHANGE"}]})
    assert r.status_code == 422


# ------------------------------------------------------------ impact analysis
def test_impact_flags_the_directly_linked_pfmea_row(cell_swap):
    impacts = {i["object_ref"]: i for i in cell_swap["impacts"]}
    assert "P-03" in impacts, "the cell-to-BMS weld row analyses the changed part"
    assert impacts["P-03"]["state"] == "PENDING_RE_REVIEW"
    assert "Analyses" in impacts["P-03"]["reason"]


def test_impact_reaches_high_severity_rows_without_a_structural_link(cell_swap):
    """A cell change alters the assumptions behind the BMS over-charge analysis
    even though the two parts are structural siblings."""
    impacts = {i["object_ref"]: i for i in cell_swap["impacts"]}
    assert "D-01" in impacts
    assert "conservatively" in impacts["D-01"]["reason"]


def test_impact_reaches_the_kit_two_levels_up(cell_swap):
    kit_impacts = [i for i in cell_swap["impacts"] if i["sku"] == "KIT-NPA-01"]
    assert kit_impacts, "the kit's released BOM export is stale after a cell change"
    assert any("KIT-NPA-01 / PB-10K-PKD" in (i["where_used_path"] or "")
               for i in kit_impacts)


def test_impact_flags_certificates_for_a_safety_critical_part(cell_swap):
    schemes = {i["object_ref"] for i in cell_swap["impacts"]
               if i["object_type"] == "CERTIFICATE"}
    assert "UN38_3:PB-10K-PKD" in schemes
    assert "IEC_62133_2:PB-10K-PKD" in schemes


def test_impact_carries_a_where_used_path_for_every_record(cell_swap):
    """An impact list nobody can judge gets closed without being read."""
    assert all(i["where_used_path"] for i in cell_swap["impacts"])
    assert all(i["reason"] for i in cell_swap["impacts"])


def test_impact_does_not_flag_unrelated_skus(cell_swap):
    """A powerbank cell change must not flag the adapter's Hi-Pot control."""
    refs = {(i["object_type"], i["object_ref"]) for i in cell_swap["impacts"]}
    assert ("CONTROL_PLAN_ITEM", "TC-AD-05") not in refs
    assert ("DFMEA_ROW", "D-02") not in refs


def test_flagged_records_are_marked_on_the_records_themselves(client, api, gatekeeper,
                                                              cell_swap):
    """The impact row is the ECN's view; ``review_state`` is what the gate reads."""
    rows = client.get(f"{api}/fmea", params={"sku": "PB-10K-PKD"},
                      headers=gatekeeper).json()
    pending = {r["row_ref"] for r in rows if r["review_state"] == "PENDING_RE_REVIEW"}
    assert "P-03" in pending


# ------------------------------------------------------------ gate interaction
def test_open_change_blocks_the_gate(client, api, gatekeeper, cell_swap):
    body = client.post(f"{api}/plm/gates/G2_DESIGN_EVT/evaluate", headers=gatekeeper,
                       json={"sku": "PB-10K-PKD"}).json()
    ids = {b["rule_id"] for b in body["blocking_reasons"]}
    assert "RULE_NO_UNAPPROVED_ECN" in ids
    assert "RULE_NO_PENDING_RE_REVIEW" in ids


# ------------------------------------------------------------ decision
def test_approval_is_refused_while_re_reviews_are_outstanding(client, api,
                                                              change_board, cell_swap):
    r = client.post(f"{api}/ecn/{cell_swap['ecn_number']}/decision", headers=change_board,
                    json={"decision": "approve"})
    assert r.status_code == 409
    assert "awaiting re-review" in r.json()["detail"]


def test_engineer_cannot_approve_their_own_change(client, api, engineer, cell_swap):
    r = client.post(f"{api}/ecn/{cell_swap['ecn_number']}/decision", headers=engineer,
                    json={"decision": "approve"})
    assert r.status_code == 403


def test_completing_re_reviews_unblocks_approval(client, api, quality, change_board,
                                                 gatekeeper, cell_swap):
    ecn_no = cell_swap["ecn_number"]
    impacts = client.get(f"{api}/ecn/{ecn_no}/impacts", params={"pending_only": True},
                         headers=quality).json()
    assert impacts
    for impact in impacts:
        r = client.post(f"{api}/ecn/{ecn_no}/impacts/{impact['id']}", headers=quality,
                        json={"state": "RE_REVIEWED",
                              "note": "Assessed against the alternate cell datasheet."})
        assert r.status_code == 200

    r = client.post(f"{api}/ecn/{ecn_no}/decision", headers=change_board,
                    json={"decision": "approve"})
    assert r.status_code == 200
    assert r.json()["state"] == "APPROVED"

    # and the gate's pending-re-review block is gone
    body = client.post(f"{api}/plm/gates/G2_DESIGN_EVT/evaluate", headers=gatekeeper,
                       json={"sku": "PB-10K-PKD"}).json()
    assert "RULE_NO_PENDING_RE_REVIEW" not in {b["rule_id"] for b in body["blocking_reasons"]}


def test_re_analysis_preserves_completed_reviews(client, api, engineer, cell_swap):
    """Re-running impact analysis must not discard finished work."""
    ecn_no = cell_swap["ecn_number"]
    before = client.get(f"{api}/ecn/{ecn_no}/impacts", headers=engineer).json()
    reviewed = {i["object_ref"] for i in before if i["state"] == "RE_REVIEWED"}
    assert reviewed

    client.post(f"{api}/ecn/{ecn_no}/impact-analysis", headers=engineer)
    after = client.get(f"{api}/ecn/{ecn_no}/impacts", headers=engineer).json()
    still = {i["object_ref"] for i in after if i["state"] == "RE_REVIEWED"}
    assert reviewed <= still


# ------------------------------------------------------------ vault webhook
def _payload(**over) -> bytes:
    body = {
        "event_id": "evt-test-100", "event_type": "revision.released",
        "occurred_at": "2026-08-14T09:00:00Z", "part_number": "AD-PCB-20W",
        "revision": "C", "file_format": "STEP AP242",
        "file_name": "AD-PCB-20W_MASTER_REV_C.step",
        "checksum_sha256": hashlib.sha256(b"rev-c").hexdigest(),
        "artifact_type": "CAD_MASTER_MODEL", "is_parametric": True,
        "change_summary": "Primary-secondary creepage increased to 3.6 mm.",
    }
    body.update(over)
    return json.dumps(body).encode()


def _sign(raw: bytes) -> str:
    from app.services.vault import sign
    return sign(raw)


def test_unsigned_webhook_is_refused(client, api, integration):
    r = client.post(f"{api}/webhooks/cad-vault/revision", content=_payload(),
                    headers={**integration, "content-type": "application/json"})
    assert r.status_code == 401
    assert "Missing X-Athena-Signature" in r.json()["detail"]


def test_bad_signature_is_refused(client, api, integration):
    r = client.post(f"{api}/webhooks/cad-vault/revision", content=_payload(),
                    headers={**integration, "content-type": "application/json",
                             "x-athena-signature": "sha256=" + "0" * 64})
    assert r.status_code == 401


def test_signed_webhook_creates_a_revision_and_an_artifact(client, api, integration):
    raw = _payload()
    r = client.post(f"{api}/webhooks/cad-vault/revision", content=raw,
                    headers={**integration, "content-type": "application/json",
                             "x-athena-signature": _sign(raw)})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["created_revision"] is True
    assert body["created_artifact"] is True
    # a revision against an already-released part is an engineering change
    assert body["triggered_ecn"] is not None


def test_webhook_is_idempotent(client, api, integration):
    raw = _payload()
    first = client.post(f"{api}/webhooks/cad-vault/revision", content=raw,
                        headers={**integration, "content-type": "application/json",
                                 "x-athena-signature": _sign(raw)}).json()
    second = client.post(f"{api}/webhooks/cad-vault/revision", content=raw,
                         headers={**integration, "content-type": "application/json",
                                  "x-athena-signature": _sign(raw)}).json()
    assert second["duplicate"] is True
    assert second["created_revision"] is False
    assert second["revision_id"] == first["revision_id"]


def test_file_format_must_match_the_artifact_type(client, api, integration):
    raw = _payload(event_id="evt-test-101", artifact_type="GERBER")
    r = client.post(f"{api}/webhooks/cad-vault/revision", content=raw,
                    headers={**integration, "content-type": "application/json",
                             "x-athena-signature": _sign(raw)})
    assert r.status_code == 422
    assert "RS-274X" in r.json()["detail"]


def test_non_parametric_step_is_not_a_master_model(client, api, integration):
    raw = _payload(event_id="evt-test-102", is_parametric=False)
    r = client.post(f"{api}/webhooks/cad-vault/revision", content=raw,
                    headers={**integration, "content-type": "application/json",
                             "x-athena-signature": _sign(raw)})
    assert r.status_code == 422
    assert "is_parametric" in r.json()["detail"]


def test_same_name_different_content_is_refused(client, api, integration):
    """Silently overwriting a released file would break the audit trail."""
    raw = _payload(event_id="evt-test-103",
                   checksum_sha256=hashlib.sha256(b"different").hexdigest())
    r = client.post(f"{api}/webhooks/cad-vault/revision", content=raw,
                    headers={**integration, "content-type": "application/json",
                             "x-athena-signature": _sign(raw)})
    assert r.status_code == 409
    assert "different checksum" in r.json()["detail"]


def test_engineer_cannot_post_a_vault_webhook(client, api, engineer):
    raw = _payload(event_id="evt-test-104")
    r = client.post(f"{api}/webhooks/cad-vault/revision", content=raw,
                    headers={**engineer, "content-type": "application/json",
                             "x-athena-signature": _sign(raw)})
    assert r.status_code == 403

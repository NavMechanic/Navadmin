"""The stage-gate engine and the published evaluation contract.

Ordering is deliberate. ``test_a_*`` observe the seeded (blocked) state,
``test_m_*`` remediate, and ``test_z_*`` assert the gate then passes and
transitions. Running them out of order would test a state that never occurs.
"""
from __future__ import annotations

import pytest

KIT = "KIT-NPA-01"
G2 = "G2_DESIGN_EVT"


# ------------------------------------------------------------ the contract
def test_a_blocked_gate_returns_422_with_the_published_schema(client, api, gatekeeper):
    r = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                    json={"sku": KIT})
    assert r.status_code == 422, "a blocked gate must not answer 200"
    body = r.json()

    # the five keys the specification names
    assert body["gate"] == G2
    assert body["status"] == "BLOCKED"
    assert body["evaluation_timestamp"].endswith("Z")
    assert isinstance(body["blocking_reasons"], list) and body["blocking_reasons"]
    assert isinstance(body["passed_artifacts"], list)

    for reason in body["blocking_reasons"]:
        assert set(reason) >= {"rule_id", "status", "detail"}
        assert reason["status"] == "FAILED"
        assert reason["detail"].strip()


def test_a_blocking_reasons_are_the_two_the_specification_names(client, api, gatekeeper):
    body = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                       json={"sku": KIT}).json()
    ids = {b["rule_id"] for b in body["blocking_reasons"]}
    assert ids == {"RULE_CREEPAGE_CHECK", "RULE_UNASSIGNED_FMEA"}


def test_a_creepage_detail_states_measured_required_and_clause(client, api, gatekeeper):
    """A blocking reason that says 'requirements not met' is useless."""
    body = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                       json={"sku": "AD-20W-PKD"}).json()
    detail = next(b["detail"] for b in body["blocking_reasons"]
                  if b["rule_id"] == "RULE_CREEPAGE_CHECK")
    assert "2.4mm" in detail
    assert "3.0mm" in detail
    assert "IEC 62368-1" in detail


def test_a_unassigned_fmea_names_the_row_and_the_function(client, api, gatekeeper):
    body = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                       json={"sku": "PB-10K-PKD"}).json()
    reason = next(b for b in body["blocking_reasons"]
                  if b["rule_id"] == "RULE_UNASSIGNED_FMEA")
    assert "D-01" in reason["detail"]
    assert "over-charge" in reason["detail"]
    # a department is not an owner, and the message has to say which department
    assert "Design Eng." in reason["detail"]
    assert "D-01" in reason["object_refs"]


def test_a_passed_artifacts_lists_confirmed_evidence(client, api, gatekeeper):
    body = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                       json={"sku": KIT}).json()
    arts = body["passed_artifacts"]
    assert "CAD_MASTER_V2.STEP" in arts
    assert "PRELIMINARY_BOM_REV3.CSV" in arts
    assert arts == sorted(set(arts)), "evidence must be deduplicated and ordered"


def test_a_blocked_gate_state_is_recorded_not_just_returned(client, api, gatekeeper):
    client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper, json={"sku": KIT})
    gates = client.get(f"{api}/plm/gates", params={"sku": KIT}, headers=gatekeeper).json()
    g2 = next(g for g in gates if g["gate_code"] == G2)
    assert g2["state"] == "BLOCKED"


def test_a_evaluation_is_persisted_immutably(client, api, gatekeeper):
    client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper, json={"sku": KIT})
    hist = client.get(f"{api}/plm/gates/{G2}/evaluations", params={"sku": KIT},
                      headers=gatekeeper).json()
    assert hist, "every evaluation must leave a record"
    assert hist[0]["payload"]["blocking_reasons"], "the payload is stored verbatim"


# ------------------------------------------------------------ scope
def test_a_kit_scope_sees_child_sku_evidence(client, api, gatekeeper):
    """Creepage belongs to the adapter and the FMEA row to the powerbank; both
    surface on the kit, because a kit whose children are not ready is not ready."""
    kit = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                      json={"sku": KIT}).json()
    ad = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                     json={"sku": "AD-20W-PKD"}).json()
    assert "RULE_CREEPAGE_CHECK" in {b["rule_id"] for b in kit["blocking_reasons"]}
    assert "RULE_CREEPAGE_CHECK" in {b["rule_id"] for b in ad["blocking_reasons"]}


def test_a_creepage_is_not_applicable_to_the_neckband(client, api, gatekeeper):
    body = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                       json={"sku": "NB-100-PKD"}).json()
    assert "RULE_CREEPAGE_CHECK" not in {b["rule_id"] for b in body["blocking_reasons"]}
    assert body["rules_not_applicable"] >= 1


def test_a_advisories_do_not_block(client, api, gatekeeper):
    body = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                       json={"sku": "AD-20W-PKD"}).json()
    advisory_ids = {a["rule_id"] for a in body["advisories"]}
    blocking_ids = {b["rule_id"] for b in body["blocking_reasons"]}
    assert "RULE_NRND_COMPONENT_DESIGNED_IN" in advisory_ids
    assert "RULE_SAFETY_PARAMETER_PLACEHOLDERS" in advisory_ids
    assert not (advisory_ids & blocking_ids)


# ------------------------------------------------------------ RBAC
def test_a_engineer_evaluation_is_a_dry_run(client, api, engineer):
    body = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=engineer,
                       json={"sku": "NB-100-PKD", "apply_transition": True}).json()
    assert body["transition_applied"] is False
    assert "RULE_TRANSITION_PERMISSION" in {a["rule_id"] for a in body["advisories"]}


def test_a_unauthenticated_evaluation_is_401(client, api):
    r = client.post(f"{api}/plm/gates/{G2}/evaluate", json={"sku": KIT})
    assert r.status_code == 401


def test_a_engineer_cannot_waive(client, api, engineer):
    r = client.post(f"{api}/plm/gates/{G2}/waive", headers=engineer,
                    json={"sku": KIT, "rule_ids": ["RULE_CREEPAGE_CHECK"],
                          "reason": "we would very much like to ship this quarter"})
    assert r.status_code == 403
    assert "gatekeeper" in r.json()["detail"]


# ------------------------------------------------------------ gate ordering
def test_a_gate_cannot_be_passed_out_of_order(client, api, gatekeeper):
    body = client.post(f"{api}/plm/gates/G5/evaluate", headers=gatekeeper,
                       json={"sku": "NB-100-PKD"}).json()
    ids = {b["rule_id"] for b in body["blocking_reasons"]}
    assert "RULE_PREDECESSOR_GATE_PASSED" in ids


def test_a_higher_gates_inherit_lower_blocking_rules(client, api, gatekeeper):
    g2_rules = {r["rule_id"] for r in
                client.get(f"{api}/plm/gates/G2/rules", headers=gatekeeper).json()}
    g4_rules = {r["rule_id"] for r in
                client.get(f"{api}/plm/gates/G4/rules", headers=gatekeeper).json()}
    blocking_g2 = {r["rule_id"] for r in
                   client.get(f"{api}/plm/gates/G2/rules", headers=gatekeeper).json()
                   if r["severity"] == "BLOCKING"}
    assert blocking_g2 <= g4_rules
    assert len(g4_rules) > len(g2_rules)


def test_a_unknown_gate_id_is_404(client, api, gatekeeper):
    r = client.post(f"{api}/plm/gates/G9_IMAGINARY/evaluate", headers=gatekeeper,
                    json={"sku": KIT})
    assert r.status_code == 404


def test_a_gate_alias_forms_resolve(client, api, gatekeeper):
    for alias in ("G2", "g2", "G2_DESIGN_EVT", "g2_design_evt"):
        r = client.post(f"{api}/plm/gates/{alias}/evaluate", headers=gatekeeper,
                        json={"sku": KIT})
        assert r.json()["gate"] == G2


# ------------------------------------------------------------ waiver
def test_a_waiver_of_a_non_failing_rule_is_refused(client, api, gatekeeper):
    """A pre-emptive waiver is how a real failure gets hidden later."""
    r = client.post(f"{api}/plm/gates/{G2}/waive", headers=gatekeeper,
                    json={"sku": "NB-100-PKD", "rule_ids": ["RULE_CREEPAGE_CHECK"],
                          "reason": "not currently failing so this should be refused"})
    assert r.status_code == 409
    assert "not currently failing" in r.json()["detail"]


def test_a_partial_waiver_is_refused(client, api, gatekeeper):
    r = client.post(f"{api}/plm/gates/{G2}/waive", headers=gatekeeper,
                    json={"sku": KIT, "rule_ids": ["RULE_CREEPAGE_CHECK"],
                          "reason": "waiving only one of the two failing rules here"})
    assert r.status_code == 409
    assert "would still block" in r.json()["detail"]


def test_a_waiver_reason_must_be_substantive(client, api, gatekeeper):
    r = client.post(f"{api}/plm/gates/{G2}/waive", headers=gatekeeper,
                    json={"sku": KIT, "rule_ids": ["RULE_CREEPAGE_CHECK"],
                          "reason": "because because because ok"})
    assert r.status_code == 422


# ------------------------------------------------------------ remediation
def test_m_assign_owner_clears_the_fmea_block(client, api, quality, gatekeeper):
    for row_ref, owner in (("D-01", "r.iyer"), ("P-05", "d.rao")):
        r = client.post(f"{api}/fmea/{row_ref}/owner", headers=quality,
                        json={"owner_username": owner, "target_date": "2026-09-30"})
        assert r.status_code == 200
        assert r.json()["owner_username"] == owner

    body = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                       json={"sku": KIT}).json()
    assert "RULE_UNASSIGNED_FMEA" not in {b["rule_id"] for b in body["blocking_reasons"]}


def test_m_verifying_creepage_clears_the_isolation_block(client, api, engineer,
                                                         gatekeeper):
    r = client.post(f"{api}/design-parameters/CREEPAGE_AD_PCB_PRI_SEC/verify",
                    headers=engineer, params={"measured_value": 3.6})
    assert r.status_code == 200
    assert r.json()["meets_minimum"] is True
    assert r.json()["is_placeholder"] is False

    body = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                       json={"sku": "AD-20W-PKD"}).json()
    assert "RULE_CREEPAGE_CHECK" not in {b["rule_id"] for b in body["blocking_reasons"]}


def test_m_a_below_minimum_measurement_still_blocks(client, api, engineer, gatekeeper):
    """Verifying is not the same as passing. Recording 2.9 mm must still fail."""
    client.post(f"{api}/design-parameters/CREEPAGE_AD_PCB_PRI_SEC/verify",
                headers=engineer, params={"measured_value": 2.9})
    body = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                       json={"sku": "AD-20W-PKD"}).json()
    detail = next(b["detail"] for b in body["blocking_reasons"]
                  if b["rule_id"] == "RULE_CREEPAGE_CHECK")
    assert "2.9mm" in detail
    # put it back above the minimum for the tests that follow
    client.post(f"{api}/design-parameters/CREEPAGE_AD_PCB_PRI_SEC/verify",
                headers=engineer, params={"measured_value": 3.6})


def test_z_gate_passes_and_transitions_when_everything_is_satisfied(client, api,
                                                                    gatekeeper):
    r = client.post(f"{api}/plm/gates/{G2}/evaluate", headers=gatekeeper,
                    json={"sku": "AD-20W-PKD"})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "PASSED"
    assert body["blocking_reasons"] == []
    assert body["transition_applied"] is True
    assert body["gate_state"] == "PASSED"
    # re-evaluating a passed gate is idempotent: it stays passed and is recorded again
    assert body["previous_gate_state"] in {"OPEN", "BLOCKED", "PASSED"}

    gates = client.get(f"{api}/plm/gates", params={"sku": "AD-20W-PKD"},
                       headers=gatekeeper).json()
    assert next(g for g in gates if g["gate_code"] == G2)["state"] == "PASSED"
    assert next(g for g in gates if g["gate_code"] == G2)["closed_by"] == "p.nair"


def test_z_g3_blocks_on_the_placeholders_g2_only_warned_about(client, api, gatekeeper):
    """Advisory at G2, blocking at the design freeze."""
    body = client.post(f"{api}/plm/gates/G3/evaluate", headers=gatekeeper,
                       json={"sku": "AD-20W-PKD"}).json()
    ids = {b["rule_id"] for b in body["blocking_reasons"]}
    assert "RULE_NO_PLACEHOLDER_AT_FREEZE" in ids
    assert "RULE_GERBER_RELEASED" in ids, "an IN_REVIEW Gerber is not a released one"


def test_z_g4_blocks_on_market_access_certificates(client, api, gatekeeper):
    body = client.post(f"{api}/plm/gates/G4/evaluate", headers=gatekeeper,
                       json={"sku": "NB-100-PKD"}).json()
    reason = next((b for b in body["blocking_reasons"]
                   if b["rule_id"] == "RULE_MARKET_ACCESS_CERTIFICATES"), None)
    assert reason is not None
    assert "BIS_CRS" in reason["detail"] or "WPC_ETA" in reason["detail"]

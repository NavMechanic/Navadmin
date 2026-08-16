"""CAPA endpoints and the propose/apply separation.

The assertion this module exists for is
``test_field_quality_cannot_apply_a_rerating``. Everything else supports it.

Occurrence feeds Action Priority, and Action Priority is what the G2 gate rule
blocks on. A field-data service that could write occurrence could lower an AP
from HIGH to MEDIUM and unblock a gate, automatically, with nobody having
decided anything. Field evidence is exactly what *should* drive a re-rating and
exactly what must not apply one unattended.
"""
from __future__ import annotations

import itertools

import pytest

from app.services import action_priority as ap_svc

API = "/api/v1"


def _capa_body(**over):
    body = {
        "title": "Powerbank sudden shutdown under 20 W PD load",
        "description": ("Field cluster CLU-2026-089: sudden shutdown under sustained "
                        "20 W PD draw, concentrated in one BMS lot."),
        "source": "FIELD_CLUSTER",
        "source_ref": "CLU-2026-089",
        "sku": "PB-10K-PKD",
        "component_lot": "PB-BMS-A05-LOT04",
        "severity": "HIGH",
        "assigned_to": "s.khan",
        "affected_volume_pct": 0.0042,
        "affected_unit_count": 52,
        "population_count": 12380,
        "evidence": {"cluster_id": "CLU-2026-089", "algorithm": "DBSCAN"},
    }
    body.update(over)
    return body


class TestActionPriorityTable:
    """Invariants, not cells.

    The AP table is published in a copyrighted handbook. Asserting individual
    cells against a table transcribed from memory would only prove the table
    matches itself. These assert the properties the real table must satisfy
    whatever its exact cells are — and they are what would catch a transcription
    error that inverted a band.
    """

    def test_monotonic_in_every_input(self):
        rank = ap_svc.rank
        ap = ap_svc.action_priority
        for s, o, d in itertools.product(range(1, 11), repeat=3):
            if s < 10:
                assert rank(ap(s + 1, o, d)) >= rank(ap(s, o, d)), (s, o, d)
            if o < 10:
                assert rank(ap(s, o + 1, d)) >= rank(ap(s, o, d)), (s, o, d)
            if d < 10:
                assert rank(ap(s, o, d + 1)) >= rank(ap(s, o, d)), (s, o, d)

    def test_a_frequent_safety_failure_is_always_high(self):
        for s, o in itertools.product((9, 10), range(6, 11)):
            for d in range(1, 11):
                assert ap_svc.action_priority(s, o, d) == "HIGH", (s, o, d)

    def test_no_discernible_effect_is_never_high(self):
        for o, d in itertools.product(range(1, 11), repeat=2):
            assert ap_svc.action_priority(1, o, d) == "LOW"

    def test_detection_alone_cannot_clear_a_frequent_safety_failure(self):
        """Perfect detection does not make an unsafe part safe — it screens it."""
        assert ap_svc.action_priority(10, 8, 1) == "HIGH"

    def test_out_of_range_is_refused(self):
        with pytest.raises(ValueError, match="between 1 and 10"):
            ap_svc.action_priority(11, 5, 5)

    def test_the_table_declares_it_is_unverified(self):
        assert ap_svc.TABLE_VERIFIED is False, (
            "if the table has been verified against the licensed handbook, flip the "
            "flag and delete this test — but do not flip it without doing that")
        assert "unverified" in ap_svc.explain(5, 5, 5)["caveat"].lower()


class TestCapaCreation:
    def test_node6_can_open_a_capa(self, client, token_for):
        r = client.post(f"{API}/plm/capa", json=_capa_body(),
                        headers=token_for("node6.fleet"))
        assert r.status_code == 201, r.text
        body = r.json()
        assert body["capa_number"].startswith("CAPA-")
        assert body["state"] == "OPEN"
        assert body["source"] == "FIELD_CLUSTER"
        assert body["affected_volume_pct"] == pytest.approx(0.0042)

    def test_opening_is_idempotent_on_the_source_reference(self, client, token_for):
        """A retried clustering run must not open a second CAPA.

        Two CAPAs for one cluster is how the same field failure gets
        investigated twice by two people who do not know about each other.
        """
        h = token_for("node6.fleet")
        first = client.post(f"{API}/plm/capa",
                            json=_capa_body(source_ref="CLU-IDEMPOTENT-1"), headers=h)
        assert first.status_code == 201
        second = client.post(f"{API}/plm/capa",
                             json=_capa_body(source_ref="CLU-IDEMPOTENT-1"), headers=h)
        assert second.status_code == 200
        assert second.json()["capa_number"] == first.json()["capa_number"]
        assert any("returned rather than duplicated" in n
                   for n in second.json()["notes"])

    def test_a_proposed_rerating_requires_a_row_and_a_rationale(self, client, token_for):
        h = token_for("node6.fleet")
        r = client.post(f"{API}/plm/capa", json=_capa_body(
            source_ref="CLU-NO-ROW", proposed_occurrence=6), headers=h)
        assert r.status_code == 422
        assert "linked_fmea_row_ref" in r.text

        r = client.post(f"{API}/plm/capa", json=_capa_body(
            source_ref="CLU-NO-REASON", proposed_occurrence=6,
            linked_fmea_row_ref="D-01"), headers=h)
        assert r.status_code == 422
        assert "rationale" in r.text

    def test_affected_count_cannot_exceed_the_population(self, client, token_for):
        r = client.post(f"{API}/plm/capa", json=_capa_body(
            source_ref="CLU-IMPOSSIBLE", affected_unit_count=99,
            population_count=10), headers=token_for("node6.fleet"))
        assert r.status_code == 422

    def test_a_rerating_on_another_products_row_is_refused(self, client, token_for):
        """D-01 is a powerbank row; proposing it against the adapter must fail."""
        r = client.post(f"{API}/plm/capa", json=_capa_body(
            source_ref="CLU-WRONG-SKU", sku="AD-20W-PKD",
            linked_fmea_row_ref="D-01", proposed_occurrence=6,
            occurrence_rationale="Field cluster suggests a higher rate."),
            headers=token_for("node6.fleet"))
        assert r.status_code == 422
        assert "belongs to" in r.text


class TestProposeApplySeparation:
    """The whole reason this module exists."""

    def _open_with_proposal(self, client, token_for, ref: str, occurrence: int):
        r = client.post(f"{API}/plm/capa", json=_capa_body(
            source_ref=ref, sku="PB-10K-PKD", linked_fmea_row_ref="D-01",
            proposed_occurrence=occurrence,
            occurrence_rationale=("Field return rate on lot PB-BMS-A05-LOT04 is "
                                  "0.42%, an order above the assumed rate.")),
            headers=token_for("node6.fleet"))
        assert r.status_code == 201, r.text
        return r.json()

    def test_opening_a_capa_does_not_change_the_fmea_occurrence(
            self, client, token_for):
        before = client.get(f"{API}/fmea", headers=token_for("a.mehta")).json()
        d01_before = next(r for r in before if r["row_ref"] == "D-01")

        body = self._open_with_proposal(client, token_for, "CLU-PROPOSE-1", 6)
        assert body["rerating_state"] == "PROPOSED"

        after = client.get(f"{API}/fmea", headers=token_for("a.mehta")).json()
        d01_after = next(r for r in after if r["row_ref"] == "D-01")
        assert d01_after["occurrence"] == d01_before["occurrence"], (
            "opening a CAPA changed an FMEA occurrence rating")
        assert d01_after["action_priority"] == d01_before["action_priority"]
        # But the row IS flagged for re-review — that makes a gate stricter.
        assert d01_after["review_state"] == "PENDING_RE_REVIEW"

    def test_field_quality_cannot_apply_a_rerating(self, client, token_for):
        """node6.fleet holds field_quality. It must be refused here.

        If this test ever passes with a 200, an automated clustering run can
        move an FMEA row's Action Priority, and a HIGH-to-MEDIUM move is what
        stops the row blocking G2.
        """
        capa = self._open_with_proposal(client, token_for, "CLU-PRIVILEGE", 6)
        r = client.post(f"{API}/plm/capa/{capa['capa_number']}/rerating",
                        json={"apply": True, "reason": "Applying my own proposal."},
                        headers=token_for("node6.fleet"))
        assert r.status_code == 403, (
            "the service that proposed the re-rating applied it")
        assert "field_quality" in r.text or "Requires one of" in r.text

    def test_quality_can_apply_and_the_ap_is_recomputed(self, client, token_for):
        capa = self._open_with_proposal(client, token_for, "CLU-APPLY", 8)
        r = client.post(f"{API}/plm/capa/{capa['capa_number']}/rerating",
                        json={"apply": True,
                              "reason": "Teardown confirms the BMS lot is the cause."},
                        headers=token_for("s.khan"))
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["rerating_state"] == "APPLIED"
        assert body["rerating_applied_by"] == "s.khan"
        assert body["current_occurrence"] == 8
        assert body["occurrence_before"] != 8
        # AP after must match the table for the row's S and D.
        rows = client.get(f"{API}/fmea", headers=token_for("a.mehta")).json()
        d01 = next(r for r in rows if r["row_ref"] == "D-01")
        assert d01["action_priority"] == ap_svc.action_priority(
            d01["severity"], 8, d01["detection"])

    def test_applying_records_who_and_what_changed(self, client, token_for):
        capa = self._open_with_proposal(client, token_for, "CLU-AUDIT", 8)
        client.post(f"{API}/plm/capa/{capa['capa_number']}/rerating",
                    json={"apply": True, "reason": "Confirmed by teardown analysis."},
                    headers=token_for("s.khan"))
        body = client.get(f"{API}/plm/capa/{capa['capa_number']}",
                          headers=token_for("a.mehta")).json()
        assert body["rerating_applied_at"]
        assert body["action_priority_before"]
        assert body["action_priority_after"]

    def test_a_rerating_can_be_rejected(self, client, token_for):
        capa = self._open_with_proposal(client, token_for, "CLU-REJECT", 7)
        r = client.post(f"{API}/plm/capa/{capa['capa_number']}/rerating",
                        json={"apply": False,
                              "reason": "Cluster traced to a charger, not the cell."},
                        headers=token_for("s.khan"))
        assert r.status_code == 200
        assert r.json()["rerating_state"] == "REJECTED"

    def test_deciding_twice_is_refused(self, client, token_for):
        capa = self._open_with_proposal(client, token_for, "CLU-TWICE", 7)
        h = token_for("s.khan")
        first = client.post(f"{API}/plm/capa/{capa['capa_number']}/rerating",
                            json={"apply": False, "reason": "Not systemic on review."},
                            headers=h)
        assert first.status_code == 200
        second = client.post(f"{API}/plm/capa/{capa['capa_number']}/rerating",
                             json={"apply": True, "reason": "Changed my mind entirely."},
                             headers=h)
        assert second.status_code == 409

    def test_a_downgrade_is_refused_while_the_table_is_unverified(
            self, client, token_for):
        """Raising priority on an unverified table is safe. Lowering it is not."""
        rows = client.get(f"{API}/fmea", headers=token_for("a.mehta")).json()
        d02 = next(r for r in rows if r["row_ref"] == "D-02")
        current_ap = ap_svc.action_priority(d02["severity"], d02["occurrence"],
                                            d02["detection"])
        lower = next((o for o in range(1, d02["occurrence"])
                      if ap_svc.rank(ap_svc.action_priority(
                          d02["severity"], o, d02["detection"]))
                      < ap_svc.rank(current_ap)), None)
        if lower is None:
            pytest.skip("no occurrence value lowers this row's AP")

        capa = client.post(f"{API}/plm/capa", json=_capa_body(
            source_ref="CLU-DOWNGRADE", sku=d02["sku"], linked_fmea_row_ref="D-02",
            proposed_occurrence=lower,
            occurrence_rationale="Field data suggests a lower rate than assumed."),
            headers=token_for("node6.fleet")).json()
        r = client.post(f"{API}/plm/capa/{capa['capa_number']}/rerating",
                        json={"apply": True, "reason": "Field evidence is thin here."},
                        headers=token_for("s.khan"))
        assert r.status_code == 409
        assert "lowering its Action Priority" in r.text


class TestClosure:
    def test_closing_requires_a_note(self, client, token_for):
        capa = client.post(f"{API}/plm/capa", json=_capa_body(source_ref="CLU-CLOSE-1"),
                           headers=token_for("node6.fleet")).json()
        r = client.post(f"{API}/plm/capa/{capa['capa_number']}/state",
                        json={"state": "CLOSED"}, headers=token_for("s.khan"))
        assert r.status_code == 422

    def test_a_capa_with_an_undecided_rerating_cannot_close(self, client, token_for):
        """Otherwise the evidence leaves no trace and the next occurrence looks new."""
        capa = client.post(f"{API}/plm/capa", json=_capa_body(
            source_ref="CLU-CLOSE-2", sku="PB-10K-PKD", linked_fmea_row_ref="D-01",
            proposed_occurrence=7,
            occurrence_rationale="Field rate exceeds the assumed rate."),
            headers=token_for("node6.fleet")).json()
        r = client.post(f"{API}/plm/capa/{capa['capa_number']}/state",
                        json={"state": "CLOSED", "note": "Cluster resolved in the field."},
                        headers=token_for("s.khan"))
        assert r.status_code == 409
        assert "still PROPOSED" in r.text

    def test_a_capa_without_a_rerating_closes_cleanly(self, client, token_for):
        capa = client.post(f"{API}/plm/capa", json=_capa_body(source_ref="CLU-CLOSE-3"),
                           headers=token_for("node6.fleet")).json()
        r = client.post(f"{API}/plm/capa/{capa['capa_number']}/state",
                        json={"state": "CLOSED",
                              "note": "Root cause was a charger firmware bug, fixed OTA."},
                        headers=token_for("s.khan"))
        assert r.status_code == 200
        assert r.json()["state"] == "CLOSED"
        assert r.json()["closed_by"] == "s.khan"

"""DQ Control Tower — gate counts, N1–N8 scorecards, FB-01..FB-12 exceptions, telemetry, approvals.

``control_tower_data`` is a pure function over the gateway state (no Streamlit), so it is unit
tested and reusable by the API. ``render_dq_control_tower`` draws it and hosts the one-click
HOLD → RELEASED approval form, which always goes through ``DQGateway.approve`` (registered
approver for the node's gate, four-eyes, full rule re-evaluation) — the UI cannot release
anything by itself.
"""
from __future__ import annotations

from typing import Any, Optional

from rag.dq_gateway import FB_CATALOG, GATES, NODE_APPROVAL_GATE, SCORE_THRESHOLD, DQGateway, DQGatewayError

NODE_TITLES = {
    "N1": "Market intelligence", "N2": "Product definition (PRD)", "N3": "Design & engineering",
    "N4": "Verification & validation", "N5": "Sourcing & supply", "N6": "Commercial / bid",
    "N7": "Finance & P&L", "N8": "Lifecycle & field", "X": "Shared layer",
}
_GATE_LABELS = (("RELEASED", "Released"), ("BLOCK", "Blocked"), ("REWORK", "Rework"),
                ("HOLD", "Held"), ("QUARANTINE", "Quarantined"))


def control_tower_data(gateway: DQGateway, tenant_id: str, recent: int = 25) -> dict[str, Any]:
    """Snapshot of everything the Control Tower shows, scoped to one tenant."""
    stats = gateway.stats(tenant_id)
    decisions = sorted(gateway.decisions(tenant_id), key=lambda d: d.decided_at, reverse=True)

    scorecard = []
    for node in ("N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8"):
        n = stats["nodes"][node]
        scorecard.append({
            "node": node, "title": NODE_TITLES[node], "approval_gate": NODE_APPROVAL_GATE[node],
            "events": n["events"], "released": n["released"], "held": n["hold"], "rework": n["rework"],
            "blocked": n["block"], "quarantined": n["quarantine"], "avg_score": n["avg_score"],
            "release_rate_pct": None if n["release_rate"] is None else round(100 * n["release_rate"], 1),
        })

    open_exc = gateway.open_exceptions(tenant_id)
    exceptions = [{
        "exception_id": e.exception_id, "fb_code": e.fb_code, "title": e.title, "node": e.node_id,
        "artifact": e.artifact_ref, "rules": ", ".join(e.rule_ids), "message": e.message,
        "created_at": e.created_at.isoformat(),
    } for e in sorted(open_exc, key=lambda e: e.created_at, reverse=True)]
    fb_counts = {fb: 0 for fb in FB_CATALOG}
    for e in open_exc:
        fb_counts[e.fb_code] = fb_counts.get(e.fb_code, 0) + 1
    fb_summary = [{"fb_code": fb, "title": FB_CATALOG[fb][0], "open": fb_counts[fb],
                   "rules": ", ".join(sorted(FB_CATALOG[fb][1])) or "upstream revision bump"}
                  for fb in FB_CATALOG]

    pending = [{
        "event_id": d.event_id, "artifact": d.artifact_ref, "node": d.node_id, "schema": d.output_schema,
        "origin": d.origin_type, "score": d.weighted_score,
        "gate_needed": NODE_APPROVAL_GATE.get(d.node_id or "X", "G2"),
        "gate_title": GATES[NODE_APPROVAL_GATE.get(d.node_id or "X", "G2")],
        "submitted_by": d.actor_id, "reasons": "; ".join(d.reasons),
    } for d in decisions if d.quality_gate == "HOLD"]

    recent_rows = [{
        "decided_at": d.decided_at.isoformat(timespec="seconds"), "event_id": d.event_id,
        "artifact": d.artifact_ref, "node": d.node_id, "gate": d.quality_gate, "score": d.weighted_score,
        "origin": d.origin_type, "failed_rules": ", ".join(r.rule_id for r in d.failed_rules),
        "recommended_action": d.recommended_action,
    } for d in decisions[:recent]]

    tel = stats["telemetry"]
    admitted, rejected = tel.get("admitted", 0), tel.get("rejected", 0)
    telemetry = {
        "admitted": admitted, "rejected": rejected,
        "rejection_rate_pct": round(100 * rejected / (admitted + rejected), 1) if admitted + rejected else None,
        "by_rule": {k.split(":", 1)[1]: v for k, v in sorted(tel.items()) if k.startswith("rule:")},
        "recent_rejections": list(gateway.telemetry_rejections)[-10:][::-1],
    }

    return {
        "tenant_id": tenant_id,
        "gates": {label: stats["gates"][g] for g, label in _GATE_LABELS},
        "total_events": len(decisions), "score_threshold": SCORE_THRESHOLD,
        "scorecard": scorecard, "exceptions": exceptions, "fb_summary": fb_summary,
        "pending_approvals": pending, "recent_decisions": recent_rows, "telemetry": telemetry,
        "learning_set": stats["learning_set"],
    }


def approve_from_ui(gateway: DQGateway, tenant_id: str, event_id: str, reviewer_id: str,
                    notes: str = "") -> tuple[bool, str]:
    """Approval form handler. Returns (released, message); never raises into the UI."""
    if not event_id or not reviewer_id.strip():
        return False, "Select a held artifact and enter your reviewer ID."
    try:
        d = gateway.approve(event_id, reviewer_id.strip(), tenant_id, notes=notes)
    except DQGatewayError as exc:
        return False, f"Approval refused: {exc}"
    if d.quality_gate == "RELEASED":
        return True, f"{d.artifact_ref} RELEASED by {reviewer_id.strip()} (score {d.weighted_score})."
    return False, (f"Re-evaluation at approval time gave {d.quality_gate}: "
                   + "; ".join(d.reasons or [r.rule_id for r in d.failed_rules]))


def render_dq_control_tower(gateway: DQGateway, tenant_id: str, approver_hint: Optional[str] = None,
                            twin: Any = None) -> None:
    import pandas as pd
    import streamlit as st

    data = control_tower_data(gateway, tenant_id)
    st.subheader("ATHENA DQ Control Tower")
    st.caption(f"Tenant `{tenant_id}` · {data['total_events']} gated events · score ≥ {data['score_threshold']:.0f} "
               "is advisory; hard controls (Critical rules, sign-off) override it.")

    cols = st.columns(5)
    for col, (label, value) in zip(cols, data["gates"].items()):
        col.metric(label, value)

    flash = st.session_state.pop("dq_flash", None)
    if flash:
        (st.success if flash[0] else st.error)(flash[1])

    st.markdown("#### Pending human approval (HOLD → RELEASED)")
    pending = data["pending_approvals"]
    if not pending:
        st.info("No artifacts are waiting for sign-off.")
    else:
        st.dataframe(pd.DataFrame(pending), hide_index=True)
        labels = {p["event_id"]: f"{p['artifact']} · {p['node']} · needs {p['gate_needed']} ({p['gate_title']})"
                  for p in pending}
        with st.form("dq_approval_form", clear_on_submit=False):
            event_id = st.selectbox("Held artifact", list(labels), format_func=labels.get, key="dq_appr_event")
            reviewer = st.text_input("Reviewer ID (registered approver)", value=approver_hint or "", key="dq_appr_reviewer")
            notes = st.text_input("Notes", key="dq_appr_notes")
            submitted = st.form_submit_button("✅ Approve & release", type="primary")
        if submitted:
            st.session_state["dq_flash"] = approve_from_ui(gateway, tenant_id, event_id, reviewer, notes)
            st.rerun()

    st.markdown("#### N1–N8 node scorecards")
    st.dataframe(pd.DataFrame(data["scorecard"]), hide_index=True)

    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### Active exceptions")
        if data["exceptions"]:
            st.dataframe(pd.DataFrame(data["exceptions"]), hide_index=True)
        else:
            st.success("No open exceptions.")
    with right:
        st.markdown("#### FB-01..FB-12")
        st.dataframe(pd.DataFrame(data["fb_summary"])[["fb_code", "title", "open"]], hide_index=True)

    st.markdown("#### Station telemetry gate (DQ01/03/04/09/10/25)")
    tel = data["telemetry"]
    t1, t2, t3 = st.columns(3)
    t1.metric("Admitted", tel["admitted"])
    t2.metric("Rejected", tel["rejected"])
    t3.metric("Rejection rate", "—" if tel["rejection_rate_pct"] is None else f"{tel['rejection_rate_pct']}%")
    if twin is not None:
        st.caption(f"Twin snapshots usable as field evidence (REAL only): {len(twin.field_evidence_history())}")
    if tel["recent_rejections"]:
        with st.expander("Recent telemetry rejections"):
            st.json(tel["recent_rejections"])

    with st.expander("Recent gate decisions"):
        if data["recent_decisions"]:
            st.dataframe(pd.DataFrame(data["recent_decisions"]), hide_index=True)

"""ATHENA N1 — Market & Opportunity dashboard (Streamlit).

    streamlit run dashboard/n1_dashboard.py

Live mode: set ``N1_API_URL`` plus ``N1_ANALYST_TOKEN`` and ``N1_APPROVER_TOKEN`` (JWTs) in the
environment or in Streamlit secrets. The handoff button sends the approver's token, so only an approver
can release to N2/N6/N7.
Demo mode (no ``N1_API_URL``, or ``N1_DASHBOARD_DEMO=1``) runs the in-process synthetic demo, so verified
real SAM is 0 there by design and the handoff is refused.

The dashboard only reads and triggers N1 API calls. All arithmetic, guardrails and N0 checks happen
server-side.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _secret(name: str):
    try:
        if name in st.secrets:
            return st.secrets[name]
    except Exception:
        pass
    return os.environ.get(name)


@st.cache_resource(show_spinner="Starting the synthetic N1 demo…")
def _demo_client():
    from examples.run_local_demo import build_demo, demo_token
    tmp = tempfile.mkdtemp(prefix="n1-dashboard-")
    client, _, _ = build_demo(tmp)
    return client, {"analyst": demo_token("demo.analyst"), "approver": demo_token("demo.approver")}


def _api():
    url = _secret("N1_API_URL")
    if url and os.environ.get("N1_DASHBOARD_DEMO") != "1":
        import httpx
        client = httpx.Client(base_url=url, timeout=30)
        return client, {"analyst": _secret("N1_ANALYST_TOKEN"), "approver": _secret("N1_APPROVER_TOKEN")}, "live"
    client, tokens = _demo_client()
    return client, tokens, "demo"


def _h(token):
    return {"Authorization": f"Bearer {token}"} if token else {}


st.set_page_config(page_title="ATHENA N1 — Market & Opportunity", page_icon="📈", layout="wide")
client, tokens, mode = _api()
st.title("ATHENA N1 — Market & Opportunity Intelligence")
st.caption(f"Mode: **{mode}** · every figure below comes from the deterministic N1 engine behind the N0 gate")

reports = client.get("/v1/n1/reports", headers=_h(tokens["analyst"]))
if reports.status_code != 200:
    st.error(f"Could not list reports: HTTP {reports.status_code}")
    st.stop()
rows = [r for r in reports.json() if not r["superseded"]] or reports.json()
if not rows:
    st.info("No reports yet. Build one with POST /v1/n1/reports.")
    st.stop()
choice = st.selectbox("Report", [r["report_id"] for r in rows],
                      format_func=lambda rid: next(f"{r['scope_name']} · rev {r['revision']} · {rid}"
                                                   for r in rows if r["report_id"] == rid))
rep = client.get(f"/v1/n1/reports/{choice}", headers=_h(tokens["analyst"])).json()
sam = rep["sam"]

c1, c2, c3, c4 = st.columns(4)
c1.metric("Verified Real SAM", f"{float(sam['verified_real_sam']):,.2f} {sam['base_currency']}")
c2.metric("Indicative spend", f"{float(sam['indicative_spend']):,.2f}")
c3.metric("Hypothetical spend", f"{float(sam['hypothetical_addressable_spend']):,.2f}")
c4.metric("Excluded by guardrails", f"{float(sam['excluded_spend']):,.2f}")
st.caption(sam["disclaimer"])

st.subheader("Buyer shortlist")
st.dataframe([{"rank": i, "buyer": o["legal_name"], "country": o["country"],
               "heuristic priority (0–100)": o["heuristic_priority_score"],
               "verified spend": float(o["verified_spend_base"]), "risk flags": ", ".join(o["risk_flags"]),
               "evidence": ", ".join(o["evidence_ids"]),
               "N2 seed": o["rfq_seed_evidence_id"] or "; ".join(o["rfq_seed_blockers"])}
              for i, o in enumerate(rep["opportunities"], 1)], use_container_width=True, hide_index=True)
st.caption(rep["disclaimers"][1])

left, right = st.columns(2)
with left:
    st.subheader("Risk flags")
    if rep["risk_flags"]:
        st.dataframe([{"code": f["code"], "account": f["account_id"], "evidence": f["evidence_id"],
                       "detail": f["detail"]} for f in rep["risk_flags"]], use_container_width=True, hide_index=True)
    else:
        st.write("None")
with right:
    st.subheader("Pending evidence attestations")
    pending = client.get("/v1/n1/attestations/pending", headers=_h(tokens["analyst"]))
    items = pending.json() if pending.status_code == 200 else []
    if items:
        st.dataframe(items, use_container_width=True, hide_index=True)
    else:
        st.write("None — nothing awaits a reviewer")

st.subheader("Handoff")
st.write("Releases go through N0: each package is re-evaluated and then released under an ApprovalEvent "
         "from the approver.")
if st.button("Hand off to N2 (RFQ seeds), N6 and N7", type="primary", key="handoff"):
    r = client.post(f"/v1/n1/reports/{choice}/handoff", headers=_h(tokens["approver"]),
                    json={"targets": ["N2", "N6", "N7"], "derived_source_id": _secret("N1_DERIVED_SOURCE_ID")
                          or "SRC-DEMO-SYNTHETIC", "project_id": _secret("N1_PROJECT_ID") or "PRJ-DEMO"})
    if r.status_code == 200:
        for p in r.json()["packages"]:
            (st.success if p["status"] == "RELEASED" else st.warning)(f"{p['target']}: {p['status']} — {p['reason']}")
    else:
        detail = r.json().get("detail", {})
        st.error(f"Handoff refused: {detail.get('code')} — {detail.get('message')}")

with st.expander("Exclusions"):
    st.dataframe([{"evidence": x["evidence_id"], "codes": ", ".join(x["codes"]), "detail": "; ".join(x["detail"])}
                  for x in rep["exclusions"]], use_container_width=True, hide_index=True)

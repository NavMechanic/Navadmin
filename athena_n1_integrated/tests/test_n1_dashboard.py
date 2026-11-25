"""The Streamlit panel renders Verified Real SAM vs Hypothetical spend, the shortlist, flags and
pending attestations, and its one-click handoff goes through N0 (refused for synthetic demo data)."""
from __future__ import annotations

from streamlit.testing.v1 import AppTest

from conftest import ROOT


def test_dashboard_demo_renders_and_handoff_is_refused_for_synthetic_data(monkeypatch):
    monkeypatch.setenv("N1_DASHBOARD_DEMO", "1")
    monkeypatch.delenv("N1_API_URL", raising=False)
    at = AppTest.from_file(str(ROOT / "dashboard" / "n1_dashboard.py"), default_timeout=120)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    metrics = {m.label: m.value for m in at.metric}
    assert metrics["Verified Real SAM"] == "0.00 USD"
    assert metrics["Hypothetical spend"] == "1,394,400.00"
    assert {s.value for s in at.subheader} >= {"Buyer shortlist", "Risk flags", "Pending evidence attestations",
                                               "Handoff"}
    at.button(key="handoff").click().run()
    assert any("SYNTHETIC_ONLY_REPORT" in e.value for e in at.error)

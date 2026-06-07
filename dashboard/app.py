"""Aviator Digital Twin — Streamlit Dashboard

Launch locally:
    streamlit run dashboard/app.py                     # simulation mode
    streamlit run dashboard/app.py -- --with-rag       # simulation + RAG

Deploy to Streamlit Community Cloud:
    Repo: NavMechanic/Navadmin  |  Branch: main  |  File: dashboard/app.py
    Add ANTHROPIC_API_KEY in the app's Secrets settings.
"""

from __future__ import annotations

import os
import sys
import time

import streamlit as st

# ---------------------------------------------------------------------------
# Inject Streamlit Cloud secrets into environment variables
# ---------------------------------------------------------------------------
def _load_secrets() -> None:
    """Pull secrets from st.secrets (Streamlit Cloud) into os.environ."""
    try:
        for key in ("ANTHROPIC_API_KEY", "QDRANT_URL", "QDRANT_API_KEY"):
            if key in st.secrets and key not in os.environ:
                os.environ[key] = st.secrets[key]
    except Exception:
        pass  # st.secrets not available in local dev — use env vars directly

_load_secrets()

# ---------------------------------------------------------------------------
# Page config (must be first Streamlit call)
# ---------------------------------------------------------------------------
st.set_page_config(
    page_title="Aviator Digital Twin",
    page_icon="🏭",
    layout="wide",
    initial_sidebar_state="expanded",
    menu_items={
        "Get Help": "https://github.com/NavMechanic/Navadmin",
        "Report a bug": "https://github.com/NavMechanic/Navadmin/issues",
        "About": "Aviator Manufacturing Digital Twin — RAG-powered process simulation.",
    },
)

# ---------------------------------------------------------------------------
# Detect deployment mode
# ---------------------------------------------------------------------------
_WITH_RAG = "--with-rag" in sys.argv or bool(os.environ.get("ANTHROPIC_API_KEY"))
_HAS_API_KEY = bool(os.environ.get("ANTHROPIC_API_KEY"))


# ---------------------------------------------------------------------------
# Resource initialisation (cached across reruns)
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner="Initialising digital twin…")
def _build_twin(with_rag: bool):
    from rag.digital_twin.twin import DigitalTwin
    from rag.digital_twin.broker.mqtt_client import MQTTBroker, BrokerConfig

    rag_pipeline = None
    if with_rag and _HAS_API_KEY:
        try:
            from rag.pipeline import RAGPipeline
            rag_pipeline = RAGPipeline()
        except Exception as exc:
            st.warning(f"RAG pipeline unavailable: {exc}")

    twin = DigitalTwin(rag_pipeline=rag_pipeline)
    broker = MQTTBroker(BrokerConfig(simulate=True, sim_interval_s=1.5))
    return twin, broker


twin, broker = _build_twin(_WITH_RAG)

if "broker_started" not in st.session_state:
    broker.start()
    st.session_state["broker_started"] = True

if "snapshots" not in st.session_state:
    st.session_state["snapshots"] = []

if "last_snapshot" not in st.session_state:
    st.session_state["last_snapshot"] = None


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.title("🏭 Aviator Digital Twin")
    st.caption("Phase 2 · Process Simulation Platform")

    # API key status badge
    if _HAS_API_KEY:
        st.success("RAG: Online ✓", icon="🤖")
    else:
        st.info("RAG: Simulation mode\n\nAdd ANTHROPIC_API_KEY to enable AI-powered RCA.", icon="ℹ️")

    st.divider()

    st.subheader("Simulation Controls")
    auto_refresh = st.toggle("Live PLC streaming", value=True)
    refresh_interval = st.slider("Refresh interval (s)", 1, 10, 3)

    st.divider()
    st.subheader("Manual Inputs — Node A")
    torque_nm = st.slider("Torque (Nm)", 0.15, 0.85, 0.45, 0.01)
    depth_mm = st.slider("Spindle depth (mm)", 2.5, 4.0, 3.2, 0.05)

    st.subheader("Manual Inputs — Node B")
    mass_tmq = st.slider("TMQ-100 mass (mg)", 290.0, 420.0, 352.0, 1.0)
    humidity = st.slider("Humidity (%RH)", 20.0, 90.0, 45.0, 1.0)

    st.subheader("Manual Inputs — Node C")
    press_force = st.slider("Inlay press force (N)", 980.0, 1820.0, 1400.0, 10.0)
    frame_offset = st.slider("Frame offset (mm)", -0.2, 0.2, 0.0, 0.01)

    run_manual = st.button("▶ Run Manual Prediction", type="primary")
    run_batch = st.button("⚡ Run Batch Simulation (n=50)")

    st.divider()
    st.caption(f"Total cycles: {len(st.session_state['snapshots'])}")
    yield_rate = twin.yield_rate()
    st.metric("Predicted Yield", f"{yield_rate * 100:.1f}%")
    st.caption("github.com/NavMechanic/Navadmin")


# ---------------------------------------------------------------------------
# Main layout imports
# ---------------------------------------------------------------------------
from dashboard.components.node_cards import render_fastening_card, render_dispensing_card, render_press_card
from dashboard.components.prediction_charts import (
    render_risk_gauge,
    render_risk_trend,
    render_node_histogram,
    render_failure_mode_bar,
)
from dashboard.components.rag_panel import render_rag_panel
from dashboard.components.fishbone_panel import render_fishbone_dashboard
from rag.digital_twin.nodes.fastening_node import FasteningReading
from rag.digital_twin.nodes.dispensing_node import DispensingReading
from rag.digital_twin.nodes.press_node import PressReading

st.title("Aviator Digital Twin — Live Assembly Simulation")
st.caption("4M Process Variables → Predicted Functional Test Outcomes  |  Nodes A · B · C")

# ---------------------------------------------------------------------------
# Batch simulation
# ---------------------------------------------------------------------------
if run_batch:
    with st.spinner("Simulating 50 assembly cycles…"):
        batch = twin.run_batch_simulation(n=50)
        st.session_state["snapshots"].extend(batch)
        st.session_state["last_snapshot"] = batch[-1]
    st.success(f"Batch complete. Predicted yield: {twin.yield_rate()*100:.1f}%")

# ---------------------------------------------------------------------------
# Manual prediction
# ---------------------------------------------------------------------------
if run_manual:
    fa_r = FasteningReading(torque_nm=torque_nm, depth_mm=depth_mm, cycle_time_s=2.1)
    di_r = DispensingReading(mass_tmq_mg=mass_tmq, mass_lm_mg=45.0, nozzle_pressure_bar=3.0, humidity_pct=humidity)
    pr_r = PressReading(inlay_force_n=press_force, display_force_n=1210.0, dwell_time_s=15.0,
                        frame_offset_mm=frame_offset, fixture_id="BE-45")
    snap = twin.predict(fa_r, di_r, pr_r, query_rag=_WITH_RAG and _HAS_API_KEY)
    st.session_state["snapshots"].append(snap)
    st.session_state["last_snapshot"] = snap

# ---------------------------------------------------------------------------
# Live streaming — poll MQTT broker
# ---------------------------------------------------------------------------
if auto_refresh and broker:
    messages = broker.drain(timeout=0.05)
    if messages:
        msg_map = {m.node: m for m in messages}
        if len(msg_map) >= 3:
            fa_r = FasteningReading(
                torque_nm=msg_map["A_FASTENING"].torque_nm or 0.45,
                depth_mm=msg_map["A_FASTENING"].depth_mm or 3.2,
                cycle_time_s=msg_map["A_FASTENING"].cycle_time_s or 2.1,
            )
            di_r = DispensingReading(
                mass_tmq_mg=msg_map["B_DISPENSING"].mass_tmq_mg or 352.0,
                mass_lm_mg=msg_map["B_DISPENSING"].mass_lm_mg or 45.0,
                nozzle_pressure_bar=msg_map["B_DISPENSING"].nozzle_pressure_bar or 3.0,
                humidity_pct=msg_map["B_DISPENSING"].humidity_pct or 45.0,
            )
            pr_r = PressReading(
                inlay_force_n=msg_map["C_PRESS"].inlay_force_n or 1400.0,
                display_force_n=msg_map["C_PRESS"].display_force_n or 1210.0,
                dwell_time_s=msg_map["C_PRESS"].dwell_time_s or 15.0,
                frame_offset_mm=msg_map["C_PRESS"].frame_offset_mm or 0.0,
                fixture_id="BE-45",
            )
            snap = twin.predict(fa_r, di_r, pr_r, query_rag=False)
            st.session_state["snapshots"].append(snap)
            st.session_state["last_snapshot"] = snap

# ---------------------------------------------------------------------------
# Display last snapshot
# ---------------------------------------------------------------------------
last = st.session_state.get("last_snapshot")
snapshots = st.session_state.get("snapshots", [])

if last:
    col_gauge, col_info = st.columns([1, 2])
    with col_gauge:
        render_risk_gauge(last.risk_score, last.overall_risk)
    with col_info:
        st.metric("Snapshot ID", last.snapshot_id)
        st.metric("Timestamp", last.timestamp.strftime("%H:%M:%S UTC"))
        if last.flagged_tests:
            st.error(f"⚠ Flagged: {', '.join(last.flagged_tests)}")
        else:
            st.success("All tests predicted PASS")

    st.divider()

    col_a, col_b = st.columns(2)
    with col_a:
        render_fastening_card(last.fastening)
    with col_b:
        render_dispensing_card(last.dispensing)

    render_press_card(last.press)

    st.divider()

    if len(snapshots) > 1:
        render_risk_trend(snapshots)
        render_node_histogram(snapshots)

    fm = twin.top_failure_modes()
    if fm:
        render_failure_mode_bar(fm)

    st.divider()

    render_rag_panel(twin, snapshot=last)

    st.divider()

    render_fishbone_dashboard(
        triggered_fmea=last.triggered_fmea if hasattr(last, "triggered_fmea") else None,
        high_rpn_fmea=last.high_rpn_fmea if hasattr(last, "high_rpn_fmea") else None,
    )

else:
    # Landing page shown before any simulation runs
    st.info("No predictions yet — use the sidebar controls or enable Live PLC streaming.")

    col1, col2, col3 = st.columns(3)
    with col1:
        st.markdown("### 🔄 Simulate")
        st.markdown(
            "Click **⚡ Run Batch Simulation** to instantly run 50 "
            "virtual assembly cycles and see predicted yield & failure modes."
        )
    with col2:
        st.markdown("### 🎛️ Manual Control")
        st.markdown(
            "Adjust torque, press force, humidity, and alignment in the sidebar, "
            "then click **▶ Run Manual Prediction** to predict outcomes for that exact set of process parameters."
        )
    with col3:
        st.markdown("### 📡 Live Streaming")
        st.markdown(
            "Toggle **Live PLC streaming** in the sidebar to watch synthetic "
            "PLC telemetry arrive in real time, auto-updating the dashboard every few seconds."
        )

    st.markdown("---")
    st.markdown(
        "**Nodes simulated:** A — Fastening (RF contact resistance)  ·  "
        "B — Dispensing (TIM thermal stack)  ·  C — Press (gasket gap clearance)"
    )

# ---------------------------------------------------------------------------
# Auto-refresh
# ---------------------------------------------------------------------------
if auto_refresh:
    time.sleep(0.1)
    st.rerun()

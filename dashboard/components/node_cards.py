"""Node status card components for the Streamlit dashboard."""

import streamlit as st
from rag.digital_twin.nodes.fastening_node import FasteningPrediction
from rag.digital_twin.nodes.dispensing_node import DispensingPrediction
from rag.digital_twin.nodes.press_node import PressPrediction


_OUTCOME_COLOR = {"PASS": "🟢", "RISK": "🟡", "FAIL": "🔴"}
_OUTCOME_CSS = {"PASS": "normal", "RISK": "off", "FAIL": "inverse"}


def _badge(outcome: str) -> str:
    return f"{_OUTCOME_COLOR.get(outcome, '⚪')} **{outcome}**"


def render_fastening_card(pred: FasteningPrediction) -> None:
    with st.container(border=True):
        st.subheader(f"Node A — Fastening  {_OUTCOME_COLOR.get(pred.test_outcome, '⚪')}")
        st.caption("Station 22 · SA38F14673 Top Bracket · M1.6 screws")

        col1, col2, col3 = st.columns(3)
        col1.metric("Axial Force", f"{pred.f_axial_n:.1f} N")
        col2.metric(
            "Contact Resistance",
            f"{pred.contact_resistance_mohm:.3f} mΩ",
            delta=f"{pred.contact_resistance_mohm - 0.8:.3f} vs limit",
            delta_color="inverse",
        )
        col3.metric(
            "RSSI Δ",
            f"{pred.predicted_rssi_delta_db:.2f} dB",
            delta_color="inverse",
        )

        st.markdown(
            f"**Predicted test:** `{pred.test_code}`  |  "
            f"Outcome: {_badge(pred.test_outcome)}  |  "
            f"Confidence: {pred.confidence * 100:.0f}%"
        )


def render_dispensing_card(pred: DispensingPrediction) -> None:
    with st.container(border=True):
        st.subheader(f"Node B — Thermal Dispensing  {_OUTCOME_COLOR.get(pred.test_outcome, '⚪')}")
        st.caption("SM8845 SoC · TMQ-100 grease + liquid metal · PINN thermal model")

        col1, col2, col3 = st.columns(3)
        col1.metric(
            "θ_JA (corrected)",
            f"{pred.theta_JA_corrected_kw:.4f} K/W",
        )
        col2.metric(
            "T_junction",
            f"{pred.t_junction_c:.1f} °C",
            delta=f"{pred.t_junction_c - 85:.1f} vs 85°C limit",
            delta_color="inverse",
        )
        col3.metric(
            "Thermal Margin",
            f"{pred.thermal_margin_c:.1f} °C",
            delta_color="normal" if pred.thermal_margin_c > 0 else "inverse",
        )

        st.markdown(
            f"PINN correction: `{pred.pinn_correction:+.4f}`  |  "
            f"Throttle risk: {'Yes' if pred.thermal_throttle_risk else 'No'}  |  "
            f"**{_badge(pred.test_outcome)}**"
        )


def render_press_card(pred: PressPrediction) -> None:
    with st.container(border=True):
        st.subheader(f"Node C — Mechanical Press  {_OUTCOME_COLOR.get(pred.combined_outcome, '⚪')}")
        st.caption("BE-45 Inlay (1400 N) · PA-49 Display (1210 N) · 12-layer PCB")

        col1, col2, col3 = st.columns(3)
        col1.metric(
            "PCB Deflection",
            f"{pred.deflection_mm:.4f} mm",
        )
        col2.metric(
            "Gap Clearance",
            f"{pred.gap_clearance_mm:.4f} mm",
            delta=f"limit 0.05–0.18 mm",
            delta_color="normal" if 0.05 <= pred.gap_clearance_mm <= 0.18 else "inverse",
        )
        col3.metric(
            "Air Pocket P",
            f"{pred.air_pocket_probability:.2%}",
            delta_color="inverse",
        )

        fp_badge = _badge(pred.fingerprint_outcome)
        ip_badge = _badge(pred.ip68_outcome)
        st.markdown(
            f"Fingerprint: {fp_badge}  |  IP68: {ip_badge}  |  "
            f"Bending stress: {pred.bending_stress_mpa:.1f} MPa"
        )

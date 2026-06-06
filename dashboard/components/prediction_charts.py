"""Plotly charts for digital twin predictions and trend analysis."""

from __future__ import annotations

import numpy as np
import streamlit as st

try:
    import plotly.graph_objects as go
    import plotly.express as px
    from plotly.subplots import make_subplots
    _PLOTLY = True
except ImportError:
    _PLOTLY = False

from rag.digital_twin.twin import TwinSnapshot


_RISK_COLORS = {"PASS": "#00cc66", "RISK": "#ffaa00", "FAIL": "#ff3333"}


def render_risk_gauge(risk_score: float, overall: str) -> None:
    if not _PLOTLY:
        st.metric("Overall Risk Score", f"{risk_score:.2%}", delta=overall)
        return

    fig = go.Figure(go.Indicator(
        mode="gauge+number",
        value=risk_score * 100,
        title={"text": f"Overall Risk  ·  {overall}", "font": {"size": 16}},
        gauge={
            "axis": {"range": [0, 100], "ticksuffix": "%"},
            "bar": {"color": _RISK_COLORS.get(overall, "#888888")},
            "steps": [
                {"range": [0, 40], "color": "#e8f5e9"},
                {"range": [40, 70], "color": "#fff8e1"},
                {"range": [70, 100], "color": "#ffebee"},
            ],
            "threshold": {
                "line": {"color": "red", "width": 3},
                "thickness": 0.8,
                "value": 70,
            },
        },
        number={"suffix": "%", "font": {"size": 28}},
    ))
    fig.update_layout(height=250, margin=dict(t=40, b=10, l=20, r=20))
    st.plotly_chart(fig, use_container_width=True)


def render_risk_trend(snapshots: list[TwinSnapshot]) -> None:
    if not snapshots:
        st.info("No history yet — run the simulation to populate the trend chart.")
        return

    scores = [s.risk_score for s in snapshots]
    labels = [s.overall_risk for s in snapshots]
    indices = list(range(1, len(scores) + 1))
    colors = [_RISK_COLORS.get(l, "#888") for l in labels]

    if not _PLOTLY:
        st.line_chart(scores)
        return

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=indices, y=scores,
        mode="lines+markers",
        marker=dict(color=colors, size=8),
        line=dict(color="#607d8b", width=1.5),
        name="Risk Score",
    ))
    fig.add_hline(y=0.40, line_dash="dash", line_color="#ffaa00", annotation_text="RISK threshold")
    fig.add_hline(y=0.70, line_dash="dash", line_color="#ff3333", annotation_text="FAIL threshold")
    fig.update_layout(
        title="Risk Score Trend",
        xaxis_title="Cycle",
        yaxis_title="Risk Score",
        yaxis_range=[0, 1],
        height=280,
        margin=dict(t=40, b=30, l=50, r=20),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_node_histogram(snapshots: list[TwinSnapshot]) -> None:
    if not snapshots or not _PLOTLY:
        return

    fa_scores = [s.fastening.contact_resistance_mohm for s in snapshots]
    tj_scores = [s.dispensing.t_junction_c for s in snapshots]
    gap_scores = [s.press.gap_clearance_mm for s in snapshots]

    fig = make_subplots(
        rows=1, cols=3,
        subplot_titles=[
            "Contact Resistance (mΩ)",
            "T_junction (°C)",
            "Gap Clearance (mm)",
        ],
    )

    fig.add_trace(go.Histogram(x=fa_scores, nbinsx=20, marker_color="#42a5f5", name="R_c"), row=1, col=1)
    fig.add_vline(x=0.8, line_dash="dash", line_color="orange", row=1, col=1)

    fig.add_trace(go.Histogram(x=tj_scores, nbinsx=20, marker_color="#ef5350", name="T_j"), row=1, col=2)
    fig.add_vline(x=85, line_dash="dash", line_color="orange", row=1, col=2)

    fig.add_trace(go.Histogram(x=gap_scores, nbinsx=20, marker_color="#66bb6a", name="Gap"), row=1, col=3)
    fig.add_vline(x=0.05, line_dash="dash", line_color="orange", row=1, col=3)
    fig.add_vline(x=0.18, line_dash="dash", line_color="orange", row=1, col=3)

    fig.update_layout(
        title="Process Variable Distributions (simulated batch)",
        showlegend=False,
        height=320,
        margin=dict(t=60, b=30),
    )
    st.plotly_chart(fig, use_container_width=True)


def render_failure_mode_bar(failure_counts: dict[str, int]) -> None:
    if not failure_counts or not _PLOTLY:
        return

    codes = list(failure_counts.keys())
    counts = list(failure_counts.values())

    fig = px.bar(
        x=counts, y=codes,
        orientation="h",
        color=counts,
        color_continuous_scale="RdYlGn_r",
        labels={"x": "Predicted Failures", "y": "Test Code"},
        title="Predicted Failure Mode Pareto",
    )
    fig.update_layout(height=300, margin=dict(t=50, b=30, l=10, r=20), coloraxis_showscale=False)
    st.plotly_chart(fig, use_container_width=True)

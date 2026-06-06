"""IPC-A-610G inspection panel for the Streamlit dashboard."""

from __future__ import annotations

import streamlit as st
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rag.ipc.ipc_a610g import IPCInspectionResult

_CONDITION_ICON = {
    "Target": "🟢",
    "Acceptable": "🔵",
    "Process Indicator": "🟡",
    "Defect": "🔴",
}

_CONDITION_COLOR = {
    "Target": "#00cc66",
    "Acceptable": "#2196F3",
    "Process Indicator": "#ffaa00",
    "Defect": "#ff3333",
}


def render_ipc_panel(results: list) -> None:
    """Render the IPC-A-610G inspection results panel."""
    if not results:
        st.info("No IPC inspection results. Run a prediction to see IPC-A-610G assessment.")
        return

    st.subheader("IPC-A-610G Class 2 Inspection — Aviator Assembly")
    st.caption("IPC-A-610G §5 (Solder/TIM) · §8 (PCB Physical) · §9 (Terminals) · §14 (Laminate)")

    # Summary metrics
    defects = [r for r in results if r.condition.value == "Defect"]
    pis = [r for r in results if r.condition.value == "Process Indicator"]
    acceptable = [r for r in results if r.condition.value == "Acceptable"]
    targets = [r for r in results if r.condition.value == "Target"]

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("🟢 Target", len(targets))
    col2.metric("🔵 Acceptable", len(acceptable))
    col3.metric("🟡 Process Indicators", len(pis))
    col4.metric("🔴 Defects", len(defects))

    st.markdown("---")

    # Group by assembly stage
    stages = ["S22_FASTENING", "S31_DISPENSING", "S42_PRESS"]
    stage_labels = {
        "S22_FASTENING": "Station 22 — Fastening (§9.3)",
        "S31_DISPENSING": "Station 31 — Thermal Dispensing (§5.2)",
        "S42_PRESS": "Station 42 — Press & Seating (§8.3 / §14.5)",
    }

    for stage in stages:
        stage_results = [r for r in results if r.assembly_stage == stage]
        if not stage_results:
            continue

        worst = max(
            [{"Target": 0, "Acceptable": 1, "Process Indicator": 2, "Defect": 3}.get(r.condition.value, 0)
             for r in stage_results]
        )
        stage_icon = ["🟢", "🔵", "🟡", "🔴"][worst]

        with st.expander(f"{stage_icon} {stage_labels.get(stage, stage)}", expanded=(worst >= 2)):
            for result in stage_results:
                icon = _CONDITION_ICON.get(result.condition.value, "⚪")
                color = _CONDITION_COLOR.get(result.condition.value, "#888")
                val_str = f"{result.measured_value:.4f}" if abs(result.measured_value) < 100 else f"{result.measured_value:.1f}"
                st.markdown(
                    f"{icon} **§{result.criteria_section}** {result.criteria_title}  \n"
                    f"&nbsp;&nbsp;&nbsp;Measured: `{val_str} {result.units}` — "
                    f"<span style='color:{color};font-weight:bold'>{result.condition.value}</span>",
                    unsafe_allow_html=True,
                )
                if result.condition.value in ("Process Indicator", "Defect"):
                    st.caption(f"    ↳ {result.note}")

    if defects:
        st.error(f"**{len(defects)} IPC-A-610G Defect(s) detected** — disposition required per Class 2 acceptance criteria.")
    elif pis:
        st.warning(f"**{len(pis)} Process Indicator(s)** — within acceptance but warrants process review.")
    else:
        st.success("All checks meet IPC-A-610G Class 2 acceptance criteria.")


def render_ipc_variability_heatmap(history_results: list[list]) -> None:
    """Render a variability heatmap across IPC criteria over prediction history."""
    if not history_results:
        return

    try:
        import plotly.graph_objects as go
    except ImportError:
        return

    _SCORE = {"Target": 0, "Acceptable": 1, "Process Indicator": 2, "Defect": 3}

    # Build matrix: criteria × cycle
    if not history_results[0]:
        return

    criteria_labels = [f"§{r.criteria_section}" for r in history_results[0]]
    cycles = list(range(1, len(history_results) + 1))
    z = [[_SCORE.get(r.condition.value, 0) for r in cycle_results] for cycle_results in history_results]

    fig = go.Figure(go.Heatmap(
        z=list(zip(*z)),  # transpose: criteria × cycles
        x=cycles,
        y=criteria_labels,
        colorscale=[[0, "#00cc66"], [0.33, "#2196F3"], [0.66, "#ffaa00"], [1.0, "#ff3333"]],
        zmin=0, zmax=3,
        colorbar=dict(
            tickvals=[0, 1, 2, 3],
            ticktext=["Target", "Acceptable", "PI", "Defect"],
            thickness=12,
        ),
        hoverongaps=False,
    ))
    fig.update_layout(
        title="IPC-A-610G Process Variability Map (all cycles)",
        xaxis_title="Cycle",
        yaxis_title="IPC Section",
        height=380,
        margin=dict(t=50, b=40, l=80, r=40),
    )
    st.plotly_chart(fig, use_container_width=True)

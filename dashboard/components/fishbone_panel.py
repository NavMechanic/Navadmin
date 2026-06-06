"""Fishbone (Ishikawa) diagram panel for FMEA root cause visualization."""

from __future__ import annotations

import streamlit as st
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rag.fmea.fmea import FMEAItem
    from rag.fmea.fishbone import FishboneDiagram


def render_fishbone_diagram(diagram: FishboneDiagram) -> None:
    """Render a single fishbone diagram using Mermaid."""
    st.markdown(
        f"""
        ```mermaid
        {diagram.mermaid_code}
        ```
        """,
        unsafe_allow_html=True,
    )


def render_fishbone_panel_for_fmea_items(items: list[FMEAItem], max_items: int = 5) -> None:
    """Render fishbone diagrams for top FMEA items by RPN.

    Parameters
    ----------
    items : list[FMEAItem]
        List of FMEA items (should be sorted by RPN descending).
    max_items : int
        Maximum number of diagrams to render (default 5).
    """
    from rag.fmea.fishbone import FishboneGenerator

    if not items:
        st.info("No FMEA items to visualize.")
        return

    st.subheader("Root Cause Analysis — Fishbone Diagrams")
    st.caption("Ishikawa diagrams for top failure modes (sorted by RPN)")

    for i, item in enumerate(items[:max_items]):
        diagram = FishboneGenerator.from_fmea_item(item)

        with st.expander(
            f"#{i+1} [{item.item_id}] {item.failure_mode} (RPN={item.rpn})",
            expanded=(i == 0),
        ):
            st.markdown(f"**Effects:** {item.effects}")
            st.markdown(f"**Responsibility:** {item.responsibility}")

            # Render fishbone
            st.markdown(
                f"""
                ```mermaid
                {diagram.mermaid_code}
                ```
                """,
                unsafe_allow_html=False,
            )

            st.markdown(f"**Recommended Actions:** {item.recommended_actions}")
            st.caption(f"Status: {item.status}")


def render_fishbone_dashboard(
    triggered_fmea: list = None,
    high_rpn_fmea: list = None,
) -> None:
    """Render full fishbone RCA dashboard section.

    Parameters
    ----------
    triggered_fmea : list, optional
        FMEA items triggered by current digital twin prediction.
    high_rpn_fmea : list, optional
        High-RPN FMEA items from assessment.
    """
    st.header("🐟 Root Cause Analysis Dashboard")

    # Tabs for different views
    tab1, tab2 = st.tabs(["Triggered Failures", "High-Risk Modes"])

    with tab1:
        if triggered_fmea:
            render_fishbone_panel_for_fmea_items(triggered_fmea, max_items=3)
        else:
            st.info("No failures triggered in this prediction cycle.")

    with tab2:
        if high_rpn_fmea:
            render_fishbone_panel_for_fmea_items(high_rpn_fmea, max_items=5)
        else:
            st.info("No high-RPN FMEA items.")

"""
Athena 3-D Digital Twin Dashboard.

Real-time visualization of factory operations, field telemetry, and RaaS/YaSS metrics.
Integrates AWS IoT TwinMaker for 3-D asset representation.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import List, Optional

try:
    import streamlit as st
    from streamlit_plotly_events import plotly_events
    import plotly.graph_objects as go
    import plotly.express as px
except ImportError:
    st = None


class DashboardMetrics:
    """Aggregated dashboard metrics snapshot."""

    def __init__(self) -> None:
        self.timestamp = datetime.utcnow()

        # Factory metrics
        self.smt_line_utilization = 0.85  # 85%
        self.smt_throughput_units_per_hour = 120
        self.smt_current_yield = 0.964  # 96.4%
        self.aoi_defects_detected_last_hour = 14

        # Supply chain
        self.parts_in_stock = 15240
        self.parts_critical_shortage = 2
        self.upcoming_stock_outs = []

        # RaaS portfolio
        self.raas_active_units = 1247
        self.raas_mrr_usd = 187_000
        self.raas_arr_usd = 2_244_000
        self.raas_sla_breaches = 3

        # YaSS
        self.yaas_total_earned_usd = 87_500
        self.avg_yield_improvement = 0.032  # 3.2% above baseline

        # Field reliability
        self.field_failures_last_7_days = 12
        self.proactive_replacements_shipped = 4
        self.predicted_failures_next_week = 7

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp.isoformat(),
            "factory": {
                "smt_line_utilization": f"{self.smt_line_utilization * 100:.1f}%",
                "throughput_uph": self.smt_throughput_units_per_hour,
                "yield": f"{self.smt_current_yield * 100:.1f}%",
                "aoi_defects_last_hour": self.aoi_defects_detected_last_hour,
            },
            "supply_chain": {
                "parts_in_stock": self.parts_in_stock,
                "critical_shortages": self.parts_critical_shortage,
            },
            "raas": {
                "active_units": self.raas_active_units,
                "mrr_usd": self.raas_mrr_usd,
                "arr_usd": self.raas_arr_usd,
                "sla_breaches": self.raas_sla_breaches,
            },
            "yaas": {
                "total_earned_usd": self.yaas_total_earned_usd,
                "avg_yield_improvement": f"{self.avg_yield_improvement * 100:.1f}%",
            },
            "field_reliability": {
                "failures_7d": self.field_failures_last_7_days,
                "proactive_replacements": self.proactive_replacements_shipped,
                "predicted_failures": self.predicted_failures_next_week,
            },
        }


def render_dashboard() -> None:
    """Main Streamlit dashboard."""
    if not st:
        print("Streamlit not installed. Run: pip install streamlit plotly-events")
        return

    st.set_page_config(
        page_title="Project Athena — Hardware as AI Service",
        page_icon="🚀",
        layout="wide",
    )

    st.title("🚀 Project Athena — Digital Twin Dashboard")
    st.markdown(
        """
        **Real-time factory operations, RaaS portfolio, and field reliability monitoring.**
        """
    )

    metrics = DashboardMetrics()

    # ------------------------------------------------------------------
    # Top KPIs
    # ------------------------------------------------------------------

    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric(
            label="RaaS MRR",
            value=f"${metrics.raas_mrr_usd:,}",
            delta=f"+${15_000:,}",
        )

    with col2:
        st.metric(
            label="YaSS Earned",
            value=f"${metrics.yaas_total_earned_usd:,}",
            delta=f"{metrics.avg_yield_improvement * 100:+.1f}% yield",
        )

    with col3:
        st.metric(
            label="SMT Yield",
            value=f"{metrics.smt_current_yield * 100:.1f}%",
            delta=f"+{(metrics.smt_current_yield - 0.92) * 100:.1f}% vs baseline",
        )

    with col4:
        st.metric(
            label="Field Reliability",
            value=f"{metrics.raas_active_units:,} units",
            delta=f"-{metrics.field_failures_last_7_days} failures/7d",
        )

    st.divider()

    # ------------------------------------------------------------------
    # Factory Operations
    # ------------------------------------------------------------------

    st.subheader("📊 Factory Operations (Last 24 Hours)")

    factory_col1, factory_col2 = st.columns(2)

    with factory_col1:
        # SMT throughput chart
        hours = list(range(24))
        throughput = [
            120 + (i % 3) * 5 + (15 if 9 <= i <= 17 else 0) for i in hours
        ]
        fig_throughput = go.Figure()
        fig_throughput.add_trace(
            go.Scatter(x=hours, y=throughput, mode="lines+markers", name="Units/hour")
        )
        fig_throughput.update_layout(
            title="SMT Line Throughput",
            xaxis_title="Hour of Day",
            yaxis_title="Units/hour",
            height=400,
        )
        st.plotly_chart(fig_throughput, use_container_width=True)

    with factory_col2:
        # Yield trend
        yield_history = [0.920, 0.925, 0.935, 0.945, 0.952, 0.958, 0.964]
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        fig_yield = go.Figure()
        fig_yield.add_trace(
            go.Scatter(
                x=days,
                y=yield_history,
                mode="lines+markers",
                name="Yield",
                line=dict(color="green"),
            )
        )
        fig_yield.add_hline(y=0.92, line_dash="dash", annotation_text="Baseline 92%")
        fig_yield.update_layout(
            title="Weekly Yield Trend",
            xaxis_title="Day",
            yaxis_title="Yield %",
            height=400,
            yaxis=dict(range=[0.90, 0.97]),
        )
        st.plotly_chart(fig_yield, use_container_width=True)

    # AOI Defect summary
    aoi_col1, aoi_col2, aoi_col3 = st.columns(3)

    with aoi_col1:
        st.info(f"🔍 **AOI Defects (Last Hour): {metrics.aoi_defects_detected_last_hour}**")

    with aoi_col2:
        st.warning(f"⚠️ **Critical Shortages: {metrics.parts_critical_shortage}**")

    with aoi_col3:
        st.success(f"✓ **SMT Utilization: {metrics.smt_line_utilization * 100:.0f}%**")

    st.divider()

    # ------------------------------------------------------------------
    # RaaS Portfolio
    # ------------------------------------------------------------------

    st.subheader("💰 RaaS Portfolio (Reliability as a Service)")

    raas_col1, raas_col2, raas_col3 = st.columns(3)

    with raas_col1:
        st.metric(label="Active Contracts", value=metrics.raas_active_units)

    with raas_col2:
        st.metric(label="Monthly Revenue", value=f"${metrics.raas_mrr_usd:,}")

    with raas_col3:
        st.metric(label="Annual Revenue", value=f"${metrics.raas_arr_usd:,}")

    # RaaS tier breakdown
    tiers = ["Enterprise", "Premium", "Standard"]
    tier_counts = [450, 350, 447]
    tier_colors = ["#2ecc71", "#3498db", "#95a5a6"]

    fig_tiers = go.Figure(
        data=[go.Pie(labels=tiers, values=tier_counts, marker=dict(colors=tier_colors))]
    )
    fig_tiers.update_layout(title="RaaS Tier Distribution", height=400)
    st.plotly_chart(fig_tiers, use_container_width=True)

    # SLA compliance
    st.write("**SLA Compliance (Last 30 Days)**")
    sla_data = {
        "Enterprise (99.9%)": 99.92,
        "Premium (99.5%)": 99.48,
        "Standard (99.0%)": 99.05,
    }
    for tier, compliance in sla_data.items():
        color = "🟢" if compliance >= float(tier.split("(")[1].split("%")[0]) else "🔴"
        st.write(f"{color} {tier}: **{compliance:.2f}%**")

    st.divider()

    # ------------------------------------------------------------------
    # YaSS & Field Reliability
    # ------------------------------------------------------------------

    st.subheader("📈 YaSS & Field Reliability")

    yaas_col1, yaas_col2 = st.columns(2)

    with yaas_col1:
        st.metric(
            label="Yield Improvement vs Baseline",
            value=f"+{metrics.avg_yield_improvement * 100:.2f}%",
            delta=f"${metrics.yaas_total_earned_usd:,} profit share",
        )

    with yaas_col2:
        st.metric(
            label="Predicted Failures (Next 7 Days)",
            value=metrics.predicted_failures_next_week,
            delta=f"{metrics.proactive_replacements_shipped} proactive RMAs",
        )

    # Field failure trend
    days_back = list(range(-7, 1))
    failures_per_day = [2, 1, 3, 1, 2, 1, 2, 0]  # Last 7 days

    fig_failures = go.Figure()
    fig_failures.add_trace(
        go.Bar(
            x=[f"-{abs(d)}d" if d < 0 else "Today" for d in days_back],
            y=failures_per_day,
            marker_color="rgba(255, 99, 71, 0.7)",
            name="Failures",
        )
    )
    fig_failures.update_layout(
        title="Field Failures (Last 7 Days)",
        xaxis_title="Day",
        yaxis_title="Count",
        height=400,
    )
    st.plotly_chart(fig_failures, use_container_width=True)

    st.divider()

    # ------------------------------------------------------------------
    # 3-D Digital Twin Preview
    # ------------------------------------------------------------------

    st.subheader("🌐 3-D Digital Twin (AWS IoT TwinMaker)")

    st.info(
        "**Note:** Full 3-D visualization requires AWS TwinMaker WebGL viewer. "
        "This shows real-time asset metrics that feed the viewer."
    )

    twin_assets = [
        {"id": "SMT-Line-1", "status": "RUNNING", "temp_c": 28, "utilization": "85%"},
        {"id": "SMT-Line-2", "status": "RUNNING", "temp_c": 31, "utilization": "78%"},
        {"id": "FATP-Station-A", "status": "RUNNING", "temp_c": 22, "utilization": "92%"},
        {"id": "Warehouse-1", "status": "OK", "temp_c": 21, "utilization": "68%"},
    ]

    asset_col1, asset_col2 = st.columns(2)

    with asset_col1:
        st.write("**Active Factory Assets**")
        for asset in twin_assets:
            status_icon = "🟢" if asset["status"] == "RUNNING" else "🟡"
            st.write(
                f"{status_icon} **{asset['id']}**  |  "
                f"🌡️ {asset['temp_c']}°C  |  "
                f"⚙️ {asset['utilization']}"
            )

    with asset_col2:
        st.write("**Top Monitored Units (Field)**")
        field_units = [
            {"serial": "SN-0001234", "uptime": "99.97%", "next_maint": "45 days"},
            {"serial": "SN-0001235", "uptime": "99.92%", "next_maint": "30 days"},
            {"serial": "SN-0001236", "uptime": "98.50%", "risk": "⚠️ Replace in 7d"},
        ]
        for unit in field_units:
            risk_tag = f"  {unit.get('risk', '')}" if "risk" in unit else ""
            st.write(
                f"📱 **{unit['serial']}**  →  "
                f"Uptime: {unit['uptime']}{risk_tag}"
            )

    st.divider()

    # ------------------------------------------------------------------
    # Alerts & Actions
    # ------------------------------------------------------------------

    st.subheader("🔔 Alerts & Recommended Actions")

    alerts = [
        {
            "severity": "🔴 CRITICAL",
            "message": "SN-0001236 predicted thermal runaway in 7 days",
            "action": "Initiate proactive RMA",
        },
        {
            "severity": "🟠 WARNING",
            "message": "STM32F405RGT6 stock alert: 45 units remaining",
            "action": "Activate alternate: STM32F405VGT6",
        },
        {
            "severity": "🟡 INFO",
            "message": "SMT-Line-1 temperature rising (28→31°C in 2h)",
            "action": "Schedule preventive maintenance",
        },
    ]

    for alert in alerts:
        with st.container(border=True):
            col_sev, col_msg, col_act = st.columns([1, 2, 1])
            with col_sev:
                st.write(alert["severity"])
            with col_msg:
                st.write(alert["message"])
            with col_act:
                if st.button(alert["action"], key=alert["message"]):
                    st.success(f"✓ Action triggered: {alert['action']}")

    # ------------------------------------------------------------------
    # Data export
    # ------------------------------------------------------------------

    st.divider()
    st.subheader("📥 Data Export")

    export_data = metrics.to_dict()
    st.download_button(
        label="Download Dashboard Snapshot (JSON)",
        data=json.dumps(export_data, indent=2),
        file_name=f"athena_dashboard_{datetime.utcnow().isoformat()}.json",
        mime="application/json",
    )


if __name__ == "__main__":
    render_dashboard()

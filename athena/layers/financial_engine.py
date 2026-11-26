"""
Layer 6 — RaaS/YaSS Financial Engine.

Outcome-based monetization: charge for reliability (RaaS) and yield (YaSS).
Replaces transactional hardware sales with subscription-based performance guarantees.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import List, Optional

from athena.config import cfg


class RaaSContract:
    """Reliability as a Service — monthly subscription for uptime guarantee."""

    def __init__(
        self,
        unit_serial: str,
        base_monthly_usd: float,
        uptime_sla: float,  # e.g., 99.9%
        coverage_duration_months: int = 12,
    ) -> None:
        self.contract_id = f"RAAS-{uuid.uuid4().hex[:8].upper()}"
        self.unit_serial = unit_serial
        self.base_monthly_usd = base_monthly_usd
        self.uptime_sla = uptime_sla
        self.coverage_duration_months = coverage_duration_months
        self.start_date = datetime.utcnow()
        self.end_date = self.start_date + timedelta(days=30 * coverage_duration_months)

        self.total_value_usd = base_monthly_usd * coverage_duration_months

    def to_dict(self) -> dict:
        return {
            "contract_id": self.contract_id,
            "unit_serial": self.unit_serial,
            "base_monthly_usd": self.base_monthly_usd,
            "uptime_sla": f"{self.uptime_sla * 100:.1f}%",
            "coverage_duration_months": self.coverage_duration_months,
            "total_value_usd": self.total_value_usd,
            "start_date": self.start_date.isoformat(),
            "end_date": self.end_date.isoformat(),
        }


class YaaSRevenueShare:
    """Yield as a Service — profit-share based on defect reduction vs. historical baseline."""

    def __init__(
        self,
        production_run_id: str,
        unit_count: int,
        actual_yield: float,  # e.g., 0.95 = 95%
        historical_baseline_yield: float = 0.92,  # e.g., historical 92%
        unit_material_cost_usd: float = 25.0,
    ) -> None:
        self.revenue_share_id = f"YAAS-{uuid.uuid4().hex[:8].upper()}"
        self.production_run_id = production_run_id
        self.unit_count = unit_count
        self.actual_yield = actual_yield
        self.historical_baseline_yield = historical_baseline_yield

        # Calculate saved units
        self.baseline_defects = unit_count * (1 - historical_baseline_yield)
        self.actual_defects = unit_count * (1 - actual_yield)
        self.units_saved = self.baseline_defects - self.actual_defects

        # Revenue share: 30% of material cost per saved unit
        self.margin_per_unit_saved = unit_material_cost_usd * 0.30
        self.total_profit_share_usd = self.units_saved * self.margin_per_unit_saved
        self.calculation_timestamp = datetime.utcnow()

    def to_dict(self) -> dict:
        return {
            "revenue_share_id": self.revenue_share_id,
            "production_run_id": self.production_run_id,
            "unit_count": self.unit_count,
            "actual_yield": f"{self.actual_yield * 100:.1f}%",
            "historical_baseline": f"{self.historical_baseline_yield * 100:.1f}%",
            "units_saved": int(self.units_saved),
            "profit_share_usd": round(self.total_profit_share_usd, 2),
            "timestamp": self.calculation_timestamp.isoformat(),
        }


class FieldReliabilityMonitor:
    """Real-time monitoring of deployed hardware for RaaS SLA compliance."""

    def __init__(self, unit_serial: str) -> None:
        self.unit_serial = unit_serial
        self.uptime_hours = 0.0
        self.total_operational_hours = 0.0
        self.failure_events: List[dict] = []
        self.predicted_failure_risk = 0.0  # 0.0–1.0

    def record_failure(self, failure_type: str, timestamp: datetime) -> None:
        """Log a field failure for SLA tracking."""
        self.failure_events.append({
            "type": failure_type,
            "timestamp": timestamp.isoformat(),
        })
        # Trigger proactive replacement if critical
        if failure_type == "thermal_degradation":
            self.predicted_failure_risk = 0.85

    def calculate_uptime_sla(self, monitoring_window_hours: int = 720) -> float:
        """Calculate % uptime over last 30 days."""
        if not monitoring_window_hours:
            return 1.0
        downtime_hours = len(self.failure_events) * 2  # Assume 2-hour MTTR per event
        return max(0.0, (monitoring_window_hours - downtime_hours) / monitoring_window_hours)

    def predict_next_failure(self) -> Optional[str]:
        """Use historical pattern to predict next failure mode."""
        if self.predicted_failure_risk > 0.80:
            return "thermal_runaway"  # Proactive replacement recommended
        if len(self.failure_events) > 5:
            return "electrolytic_cap_aging"
        return None


class FinancialEngine:
    """
    Outcome-based monetization: RaaS (reliability) + YaSS (yield).
    Replaces transactional hardware sales with subscription guarantees.
    """

    def __init__(self) -> None:
        self._raas_contracts: dict[str, RaaSContract] = {}
        self._yaas_shares: dict[str, YaaSRevenueShare] = {}
        self._field_monitors: dict[str, FieldReliabilityMonitor] = {}

    # ------------------------------------------------------------------
    # RaaS — Reliability as a Service
    # ------------------------------------------------------------------

    def create_raas_contract(
        self,
        unit_serial: str,
        quality_score: float,  # 0.0–1.0 from inspection
        base_monthly_usd: float = 150.0,
    ) -> RaaSContract:
        """
        Create a reliability guarantee contract for a manufactured unit.
        Price scaled by quality score.
        """
        # Tier-based SLA
        if quality_score >= 0.95:
            uptime_sla = 0.999  # 99.9%
            monthly_price = base_monthly_usd * 1.5
            tier = "enterprise"
        elif quality_score >= 0.90:
            uptime_sla = 0.995  # 99.5%
            monthly_price = base_monthly_usd * 1.2
            tier = "premium"
        else:
            uptime_sla = 0.99  # 99.0%
            monthly_price = base_monthly_usd
            tier = "standard"

        contract = RaaSContract(
            unit_serial=unit_serial,
            base_monthly_usd=monthly_price,
            uptime_sla=uptime_sla,
            coverage_duration_months=12,
        )

        self._raas_contracts[unit_serial] = contract
        self._field_monitors[unit_serial] = FieldReliabilityMonitor(unit_serial)

        return contract

    def get_raas_mrr(self) -> float:
        """Calculate Monthly Recurring Revenue (MRR) from all active RaaS contracts."""
        return sum(c.base_monthly_usd for c in self._raas_contracts.values())

    def get_raas_arr(self) -> float:
        """Calculate Annual Recurring Revenue (ARR) from all active RaaS contracts."""
        return self.get_raas_mrr() * 12

    # ------------------------------------------------------------------
    # YaSS — Yield as a Service
    # ------------------------------------------------------------------

    def create_yaas_share(
        self,
        production_run_id: str,
        unit_count: int,
        actual_yield: float,
        historical_baseline_yield: float = 0.92,
        unit_material_cost_usd: float = 25.0,
    ) -> YaaSRevenueShare:
        """
        Calculate profit-share from yield improvements.
        If actual_yield > baseline, receive 30% of material cost per saved unit.
        """
        share = YaaSRevenueShare(
            production_run_id=production_run_id,
            unit_count=unit_count,
            actual_yield=actual_yield,
            historical_baseline_yield=historical_baseline_yield,
            unit_material_cost_usd=unit_material_cost_usd,
        )

        self._yaas_shares[production_run_id] = share
        return share

    def get_yaas_total(self) -> float:
        """Calculate total YaSS profit share earned."""
        return sum(s.total_profit_share_usd for s in self._yaas_shares.values())

    # ------------------------------------------------------------------
    # Field monitoring & SLA compliance
    # ------------------------------------------------------------------

    def record_field_failure(
        self,
        unit_serial: str,
        failure_type: str,
        timestamp: Optional[datetime] = None,
    ) -> bool:
        """Record a field failure for SLA tracking."""
        if unit_serial not in self._field_monitors:
            return False

        monitor = self._field_monitors[unit_serial]
        monitor.record_failure(failure_type, timestamp or datetime.utcnow())

        # Proactive replacement if risk high
        if monitor.predicted_failure_risk > 0.80:
            return self._trigger_proactive_replacement(unit_serial)

        return True

    def calculate_customer_sla_credit(
        self,
        unit_serial: str,
        monitoring_window_hours: int = 720,
    ) -> float:
        """
        Calculate SLA credit owed to customer if uptime < contract SLA.
        E.g., 2-9s/month downtime = 5% credit, etc.
        """
        if unit_serial not in self._field_monitors:
            return 0.0

        monitor = self._field_monitors[unit_serial]
        contract = self._raas_contracts.get(unit_serial)
        if not contract:
            return 0.0

        actual_uptime = monitor.calculate_uptime_sla(monitoring_window_hours)

        # SLA credit schedule
        if actual_uptime >= contract.uptime_sla:
            return 0.0  # No credit
        elif actual_uptime >= contract.uptime_sla - 0.001:
            return contract.base_monthly_usd * 0.05  # 5%
        elif actual_uptime >= contract.uptime_sla - 0.005:
            return contract.base_monthly_usd * 0.10  # 10%
        else:
            return contract.base_monthly_usd * 0.25  # 25%

    # ------------------------------------------------------------------
    # Summary reporting
    # ------------------------------------------------------------------

    def generate_revenue_report(self) -> dict:
        """Generate comprehensive revenue summary."""
        return {
            "timestamp": datetime.utcnow().isoformat(),
            "raas": {
                "active_contracts": len(self._raas_contracts),
                "mrr_usd": round(self.get_raas_mrr(), 2),
                "arr_usd": round(self.get_raas_arr(), 2),
            },
            "yaas": {
                "total_profit_share_usd": round(self.get_yaas_total(), 2),
                "number_of_runs": len(self._yaas_shares),
                "average_yield_improvement": self._avg_yield_improvement(),
            },
            "field_monitoring": {
                "units_monitored": len(self._field_monitors),
                "sla_credits_owed_usd": round(self._total_sla_credits_owed(), 2),
                "proactive_replacements_pending": self._count_proactive_replacements(),
            },
            "total_annualized_revenue_usd": round(
                self.get_raas_arr() + self.get_yaas_total(), 2
            ),
        }

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _trigger_proactive_replacement(self, unit_serial: str) -> bool:
        """Initiate proactive RMA for unit at risk of failure."""
        # In production, would trigger warehouse/logistics system
        print(f"  ⚠ PROACTIVE RMA INITIATED: {unit_serial}")
        return True

    def _avg_yield_improvement(self) -> float:
        """Calculate average yield improvement across all YaSS runs."""
        if not self._yaas_shares:
            return 0.0
        total_improvement = sum(
            s.actual_yield - s.historical_baseline_yield
            for s in self._yaas_shares.values()
        )
        return total_improvement / len(self._yaas_shares)

    def _total_sla_credits_owed(self) -> float:
        """Calculate total SLA credits owed to customers."""
        total = 0.0
        for unit_serial in self._field_monitors:
            total += self.calculate_customer_sla_credit(unit_serial)
        return total

    def _count_proactive_replacements(self) -> int:
        """Count units flagged for proactive RMA."""
        return sum(
            1 for m in self._field_monitors.values()
            if m.predicted_failure_risk > 0.80
        )

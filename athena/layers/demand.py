"""Layer 1 — Trigger & Demand Forecasting via Pecan AI."""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta
from typing import Optional

import httpx

from athena.config import cfg
from athena.models import DealSignal, DemandForecast


class PecanDemandLayer:
    """Polls Pecan AI for high-probability enterprise deals and builds a demand forecast."""

    def __init__(self) -> None:
        self._client = httpx.Client(
            base_url=cfg.PECAN_BASE_URL,
            headers={"Authorization": f"Bearer {cfg.PECAN_API_KEY}"},
            timeout=cfg.HTTP_TIMEOUT_S,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def get_forecast(self, deal_id: Optional[str] = None) -> DemandForecast:
        """Return a DemandForecast.  Falls back to simulation when dry-run."""
        signal = self._fetch_signal(deal_id)
        if signal.probability < cfg.PECAN_MIN_PROBABILITY:
            raise ValueError(
                f"Deal {signal.deal_id} probability {signal.probability:.0%} "
                f"below threshold {cfg.PECAN_MIN_PROBABILITY:.0%}"
            )
        return self._build_forecast(signal)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _fetch_signal(self, deal_id: Optional[str]) -> DealSignal:
        if cfg.ATHENA_DRY_RUN:
            return self._mock_signal(deal_id)

        path = f"/deals/{deal_id}/forecast" if deal_id else "/deals/top"
        resp = self._client.get(path)
        resp.raise_for_status()
        data = resp.json()

        return DealSignal(
            deal_id=data["deal_id"],
            account_name=data["account_name"],
            probability=data["win_probability"],
            estimated_units=data["estimated_units"],
            close_date=data["close_date"],
            segment=data.get("segment", "enterprise"),
            metadata=data.get("metadata", {}),
        )

    def _build_forecast(self, signal: DealSignal) -> DemandForecast:
        # Apply a 10 % safety uplift and round up to the nearest 10.
        raw = signal.estimated_units * signal.probability
        recommended = math.ceil(raw * 1.10 / 10) * 10

        return DemandForecast(
            signal=signal,
            forecast_units=round(raw),
            confidence_interval_low=max(1, round(raw * 0.85)),
            confidence_interval_high=round(raw * 1.20),
            recommended_build_qty=recommended,
        )

    @staticmethod
    def _mock_signal(deal_id: Optional[str]) -> DealSignal:
        close = (datetime.utcnow() + timedelta(days=45)).strftime("%Y-%m-%d")
        return DealSignal(
            deal_id=deal_id or f"DEAL-{uuid.uuid4().hex[:8].upper()}",
            account_name="Acme Aerospace LLC",
            probability=0.88,
            estimated_units=250,
            close_date=close,
            segment="enterprise",
            metadata={"source": "pecan_mock", "region": "AMER"},
        )

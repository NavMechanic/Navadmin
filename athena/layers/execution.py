"""Layer 4 — Execution via MacroFab (PCBA) and Fictiv (mechanical chassis)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Optional

import httpx

from athena.config import cfg
from athena.models import ExecutionResult, ProductionOrder, RunStatus


class ExecutionLayer:
    """
    Dispatches digital production packets simultaneously to MacroFab
    (PCBA) and Fictiv (mechanical chassis).
    """

    def __init__(self) -> None:
        self._macrofab = httpx.Client(
            base_url=cfg.MACROFAB_BASE_URL,
            headers={"Authorization": f"Token {cfg.MACROFAB_API_KEY}"},
            timeout=cfg.HTTP_TIMEOUT_S,
        )
        self._fictiv_token: Optional[str] = None
        self._fictiv = httpx.Client(
            base_url=cfg.FICTIV_BASE_URL,
            timeout=cfg.HTTP_TIMEOUT_S,
        )

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def dispatch(self, build_qty: int, bom_id: str) -> ExecutionResult:
        if cfg.ATHENA_DRY_RUN:
            return self._mock_execution(build_qty)

        pcba_order = self._submit_macrofab(build_qty, bom_id)
        mech_order = self._submit_fictiv(build_qty)
        total_cost = (pcba_order.estimated_unit_cost or 0) + (mech_order.estimated_unit_cost or 0)
        return ExecutionResult(
            pcba_order=pcba_order,
            mechanical_order=mech_order,
            total_cost_usd=total_cost * build_qty,
            lead_time_days=max(
                self._parse_lead_days(pcba_order.estimated_ship_date),
                self._parse_lead_days(mech_order.estimated_ship_date),
            ),
        )

    # ------------------------------------------------------------------
    # MacroFab PCBA
    # ------------------------------------------------------------------

    def _submit_macrofab(self, build_qty: int, bom_id: str) -> ProductionOrder:
        resp = self._macrofab.post(
            "/orders",
            json={
                "bom_reference": bom_id,
                "quantity": build_qty,
                "service_level": "standard",
                "notes": "Project Athena automated production run",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return ProductionOrder(
            order_id=data["order_id"],
            supplier="macrofab",
            order_type="pcba",
            quantity=build_qty,
            status=RunStatus.PENDING,
            estimated_ship_date=data.get("estimated_ship_date"),
            tracking_url=data.get("tracking_url"),
        )

    # ------------------------------------------------------------------
    # Fictiv mechanical chassis
    # ------------------------------------------------------------------

    def _get_fictiv_token(self) -> str:
        if self._fictiv_token:
            return self._fictiv_token
        resp = httpx.post(
            f"{cfg.FICTIV_BASE_URL}/oauth/token",
            json={
                "grant_type": "client_credentials",
                "client_id": cfg.FICTIV_CLIENT_ID,
                "client_secret": cfg.FICTIV_CLIENT_SECRET,
            },
            timeout=cfg.HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        self._fictiv_token = resp.json()["access_token"]
        return self._fictiv_token

    def _submit_fictiv(self, build_qty: int) -> ProductionOrder:
        token = self._get_fictiv_token()
        resp = self._fictiv.post(
            "/orders",
            json={
                "quantity": build_qty,
                "material": "6061-T6 Aluminum",
                "process": "CNC Machining",
                "finish": "Type II Anodize",
                "part_name": "Aviator Chassis",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        data = resp.json()
        return ProductionOrder(
            order_id=data["order_id"],
            supplier="fictiv",
            order_type="mechanical",
            quantity=build_qty,
            status=RunStatus.PENDING,
            estimated_ship_date=data.get("estimated_ship_date"),
            tracking_url=data.get("tracking_url"),
        )

    # ------------------------------------------------------------------
    # Mock helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mock_execution(build_qty: int) -> ExecutionResult:
        now = datetime.utcnow()
        pcba_ship = (now + timedelta(days=18)).strftime("%Y-%m-%d")
        mech_ship = (now + timedelta(days=14)).strftime("%Y-%m-%d")

        pcba_order = ProductionOrder(
            order_id=f"MF-{uuid.uuid4().hex[:8].upper()}",
            supplier="macrofab",
            order_type="pcba",
            quantity=build_qty,
            status=RunStatus.RUNNING,
            estimated_ship_date=pcba_ship,
            tracking_url="https://macrofab.com/orders/MF-MOCK",
        )
        mech_order = ProductionOrder(
            order_id=f"FV-{uuid.uuid4().hex[:8].upper()}",
            supplier="fictiv",
            order_type="mechanical",
            quantity=build_qty,
            status=RunStatus.RUNNING,
            estimated_ship_date=mech_ship,
            tracking_url="https://fictiv.com/orders/FV-MOCK",
        )

        pcba_unit_cost = 42.50
        mech_unit_cost = 18.75

        return ExecutionResult(
            pcba_order=pcba_order,
            mechanical_order=mech_order,
            total_cost_usd=round((pcba_unit_cost + mech_unit_cost) * build_qty, 2),
            lead_time_days=18,
        )

    @staticmethod
    def _parse_lead_days(ship_date: Optional[str]) -> int:
        if not ship_date:
            return 21
        try:
            delta = datetime.strptime(ship_date, "%Y-%m-%d") - datetime.utcnow()
            return max(0, delta.days)
        except ValueError:
            return 21

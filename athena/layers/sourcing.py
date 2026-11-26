"""Layer 2 — Design & Sourcing Lock via Duro PLM + Nexar API."""

from __future__ import annotations

import random
import uuid
from typing import List, Optional

import httpx

from athena.config import cfg
from athena.models import (
    BOM,
    BOMLine,
    PartAvailability,
    PartStatus,
    SourcingResult,
)

# ---------------------------------------------------------------------------
# Nexar GraphQL query for supply-chain data
# ---------------------------------------------------------------------------

_NEXAR_SUPPLY_QUERY = """
query SupplySearch($mpn: String!) {
  supplySearch(q: $mpn, limit: 5) {
    hits {
      part {
        mpn
        manufacturer { name }
        sellers {
          company { name }
          offers {
            inventoryLevel
            prices { convertedPrice currency }
          }
        }
      }
    }
  }
}
"""


class DuroSourcingLayer:
    """Fetches the master BOM from Duro PLM and checks availability via Nexar."""

    def __init__(self) -> None:
        self._duro = httpx.Client(
            base_url=cfg.DURO_BASE_URL,
            headers={"x-api-key": cfg.DURO_API_KEY},
            timeout=cfg.HTTP_TIMEOUT_S,
        )
        self._nexar_token: Optional[str] = None

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def lock_sourcing(self, build_qty: int, bom_id: Optional[str] = None) -> SourcingResult:
        bom = self._fetch_bom(bom_id)
        availability = self._check_availability(bom, build_qty)
        return self._assess_risk(bom, availability)

    # ------------------------------------------------------------------
    # Duro PLM
    # ------------------------------------------------------------------

    def _fetch_bom(self, bom_id: Optional[str]) -> BOM:
        if cfg.ATHENA_DRY_RUN:
            return self._mock_bom(bom_id)

        path = f"/boms/{bom_id}" if bom_id else f"/boms?revision={cfg.DURO_BOM_REVISION}&limit=1"
        resp = self._duro.get(path)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            data = data[0]

        lines = [
            BOMLine(
                line_id=ln["id"],
                mpn=ln["mpn"],
                manufacturer=ln["manufacturer"],
                description=ln.get("description", ""),
                quantity=ln["quantity"],
                reference_designators=ln.get("refdes", []),
                unit_price_usd=ln.get("unitPrice"),
            )
            for ln in data["lines"]
        ]
        return BOM(
            bom_id=data["id"],
            revision=data["revision"],
            product_name=data["name"],
            lines=lines,
        )

    # ------------------------------------------------------------------
    # Nexar availability check
    # ------------------------------------------------------------------

    def _get_nexar_token(self) -> str:
        if self._nexar_token:
            return self._nexar_token
        resp = httpx.post(
            "https://identity.nexar.com/connect/token",
            data={
                "grant_type": "client_credentials",
                "client_id": cfg.NEXAR_CLIENT_ID,
                "client_secret": cfg.NEXAR_CLIENT_SECRET,
            },
            timeout=cfg.HTTP_TIMEOUT_S,
        )
        resp.raise_for_status()
        self._nexar_token = resp.json()["access_token"]
        return self._nexar_token

    def _check_availability(self, bom: BOM, build_qty: int) -> List[PartAvailability]:
        if cfg.ATHENA_DRY_RUN:
            return self._mock_availability(bom, build_qty)

        token = self._get_nexar_token()
        results: List[PartAvailability] = []
        for line in bom.lines:
            resp = httpx.post(
                cfg.NEXAR_BASE_URL,
                json={"query": _NEXAR_SUPPLY_QUERY, "variables": {"mpn": line.mpn}},
                headers={"Authorization": f"Bearer {token}"},
                timeout=cfg.HTTP_TIMEOUT_S,
            )
            resp.raise_for_status()
            hits = resp.json().get("data", {}).get("supplySearch", {}).get("hits", [])
            needed = line.quantity * build_qty

            stock = sum(
                offer.get("inventoryLevel", 0)
                for hit in hits
                for seller in hit["part"].get("sellers", [])
                for offer in seller.get("offers", [])
            )

            status = PartStatus.AVAILABLE if stock >= needed else PartStatus.SHORT
            alt_mpn = None
            if status == PartStatus.SHORT and len(hits) > 1:
                alt_mpn = hits[1]["part"]["mpn"]
                status = PartStatus.ALTERNATE_MAPPED

            results.append(
                PartAvailability(
                    mpn=line.mpn,
                    status=status,
                    stock_qty=stock,
                    alternate_mpn=alt_mpn,
                )
            )
        return results

    # ------------------------------------------------------------------
    # Risk assessment
    # ------------------------------------------------------------------

    @staticmethod
    def _assess_risk(bom: BOM, avail: List[PartAvailability]) -> SourcingResult:
        shorts = sum(1 for a in avail if a.status in (PartStatus.SHORT, PartStatus.CRITICAL))
        alts = sum(1 for a in avail if a.status == PartStatus.ALTERNATE_MAPPED)
        total = len(avail)
        shortage_pct = shorts / total if total else 0

        if shortage_pct > 0.20:
            risk = "high"
        elif shortage_pct > 0.05:
            risk = "medium"
        else:
            risk = "low"

        return SourcingResult(
            bom=bom,
            availability=avail,
            shortage_count=shorts,
            alternates_mapped=alts,
            sourcing_risk=risk,
        )

    # ------------------------------------------------------------------
    # Mock helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _mock_bom(bom_id: Optional[str]) -> BOM:
        return BOM(
            bom_id=bom_id or f"BOM-{uuid.uuid4().hex[:6].upper()}",
            revision="C",
            product_name="Aviator PCBA Rev-C",
            lines=[
                BOMLine(line_id="L001", mpn="GD25Q64CSIG", manufacturer="GigaDevice",
                        description="64Mb SPI Flash", quantity=1,
                        reference_designators=["U3"], unit_price_usd=0.45),
                BOMLine(line_id="L002", mpn="STM32F405RGT6", manufacturer="STMicroelectronics",
                        description="ARM Cortex-M4 MCU", quantity=1,
                        reference_designators=["U1"], unit_price_usd=8.20),
                BOMLine(line_id="L003", mpn="LM5166DRVT", manufacturer="Texas Instruments",
                        description="DC/DC Buck Converter", quantity=2,
                        reference_designators=["U4", "U5"], unit_price_usd=1.85),
                BOMLine(line_id="L004", mpn="CGA4J2X7R1H104K125AA", manufacturer="TDK",
                        description="100nF 0805 Cap", quantity=48,
                        reference_designators=[], unit_price_usd=0.02),
                BOMLine(line_id="L005", mpn="CC0805KKX5R8BB106", manufacturer="Yageo",
                        description="10uF 0805 Cap", quantity=12,
                        reference_designators=[], unit_price_usd=0.08),
            ],
        )

    @staticmethod
    def _mock_availability(bom: BOM, build_qty: int) -> List[PartAvailability]:
        mock_data = {
            "GD25Q64CSIG": (50000, None),
            "STM32F405RGT6": (180, "STM32F405VGT6"),  # slight shortage → alternate
            "LM5166DRVT": (9000, None),
            "CGA4J2X7R1H104K125AA": (500000, None),
            "CC0805KKX5R8BB106": (200000, None),
        }
        results = []
        for line in bom.lines:
            stock, alt = mock_data.get(line.mpn, (random.randint(0, 500), None))
            needed = line.quantity * build_qty
            if stock >= needed:
                status = PartStatus.AVAILABLE
                alt = None
            elif alt:
                status = PartStatus.ALTERNATE_MAPPED
            else:
                status = PartStatus.SHORT

            results.append(
                PartAvailability(
                    mpn=line.mpn,
                    status=status,
                    stock_qty=stock,
                    lead_time_weeks=2 if status != PartStatus.AVAILABLE else None,
                    alternate_mpn=alt,
                    alternate_source="Digi-Key" if alt else None,
                )
            )
        return results

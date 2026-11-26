#!/usr/bin/env python3
"""Load ``app/data/node2_seed.json`` into the database.

    python3 scripts/seed.py               # create tables if absent, then seed
    python3 scripts/seed.py --reset       # drop everything first

Idempotent on part number, artifact name+revision, FMEA row ref and control ref,
so re-running against a populated database updates rather than duplicates.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import select                                    # noqa: E402
from sqlalchemy.ext.asyncio import AsyncSession                  # noqa: E402

from app.config import get_settings                              # noqa: E402
from app.db import create_all, dispose_engine, drop_all, get_sessionmaker  # noqa: E402
from app.models import (Artifact, BomLine, Certificate, ControlPlanItem,   # noqa: E402
                        DesignParameter, FmeaRow, GateInstance, Part,
                        PartRevision, User, utcnow)


def _d(v) -> date | None:
    return date.fromisoformat(v) if v else None


def _dt(v) -> datetime | None:
    return (datetime.fromisoformat(v).replace(tzinfo=timezone.utc)
            if isinstance(v, str) else None)


async def seed(session: AsyncSession, data: dict) -> dict[str, int]:
    counts: dict[str, int] = {}

    # ---- users -----------------------------------------------------------
    users: dict[str, User] = {}
    for u in data["users"]:
        row = (await session.execute(
            select(User).where(User.username == u["username"]))).scalar_one_or_none()
        if row is None:
            row = User(username=u["username"])
            session.add(row)
        row.full_name = u["full_name"]
        row.email = u.get("email")
        row.roles = u["roles"]
        row.department = u.get("department")
        users[u["username"]] = row
    await session.flush()
    counts["users"] = len(users)

    # ---- parts and revisions ---------------------------------------------
    parts: dict[str, Part] = {}
    for p in data["parts"]:
        row = (await session.execute(
            select(Part).where(Part.part_number == p["part_number"]))).scalar_one_or_none()
        if row is None:
            row = Part(part_number=p["part_number"])
            session.add(row)
        for k in ("name", "description", "part_type", "supply_mode", "supplier",
                  "sourcing_status", "is_sellable", "lifecycle_risk", "safety_critical"):
            setattr(row, k, p.get(k))
        parts[p["part_number"]] = row
    await session.flush()
    counts["parts"] = len(parts)

    revisions: dict[tuple[str, str], PartRevision] = {}
    for r in data["revisions"]:
        part = parts[r["part_number"]]
        row = (await session.execute(
            select(PartRevision).where(PartRevision.part_id == part.id,
                                       PartRevision.rev == r["rev"]))).scalar_one_or_none()
        if row is None:
            row = PartRevision(part_id=part.id, rev=r["rev"])
            session.add(row)
        row.lifecycle_state = r["lifecycle_state"]
        row.unit_cost_usd = (Decimal(str(r["unit_cost_usd"]))
                             if r.get("unit_cost_usd") is not None else None)
        row.change_summary = r.get("change_summary")
        row.released_at = _dt(f"{r['released_on']}T09:00:00") if r.get("released_on") else None
        row.released_by = r.get("released_by")
        row.source_system = r.get("source_system")
        row.source_ref = r.get("source_ref")
        revisions[(r["part_number"], r["rev"])] = row
    await session.flush()
    counts["revisions"] = len(revisions)

    # ---- BOM lines --------------------------------------------------------
    n_lines = 0
    for line in data["bom_lines"]:
        parent = revisions[(line["parent"], line["parent_rev"])]
        child = parts[line["child"]]
        existing = (await session.execute(
            select(BomLine).where(BomLine.parent_revision_id == parent.id,
                                  BomLine.find_number == line["find_number"]))
        ).scalar_one_or_none()
        if existing is None:
            existing = BomLine(parent_revision_id=parent.id,
                               find_number=line["find_number"])
            session.add(existing)
        existing.child_part_id = child.id
        existing.child_revision_id = (revisions[(line["child"], line["child_rev"])].id
                                      if line.get("child_rev") else None)
        existing.quantity = Decimal(str(line["quantity"]))
        existing.notes = line.get("notes")
        n_lines += 1
    await session.flush()
    counts["bom_lines"] = n_lines

    # ---- artifacts ---------------------------------------------------------
    for a in data["artifacts"]:
        row = (await session.execute(
            select(Artifact).where(Artifact.name == a["name"],
                                   Artifact.revision == a["revision"]))).scalar_one_or_none()
        if row is None:
            row = Artifact(name=a["name"], revision=a["revision"])
            session.add(row)
        row.artifact_type = a["artifact_type"]
        row.status = a["status"]
        row.sku = a.get("sku")
        row.file_format = a.get("file_format")
        row.source_system = a.get("source_system")
        row.is_parametric = a.get("is_parametric", False)
        row.gate_code = a.get("gate_code")
        row.ingested_at = utcnow()
        row.released_at = _dt(f"{a['released_on']}T09:00:00") if a.get("released_on") else None
    await session.flush()
    counts["artifacts"] = len(data["artifacts"])

    # ---- certificates ------------------------------------------------------
    for c in data["certificates"]:
        row = (await session.execute(
            select(Certificate).where(Certificate.scheme == c["scheme"],
                                      Certificate.sku == c["sku"]))).scalar_one_or_none()
        if row is None:
            row = Certificate(scheme=c["scheme"], sku=c["sku"])
            session.add(row)
        row.certificate_number = c.get("certificate_number")
        row.status = c["status"]
        row.issued_on = _d(c.get("issued_on"))
        row.expires_on = _d(c.get("expires_on"))
        row.lab = c.get("lab")
        row.scope = c.get("scope")
        row.part_id = parts[c["part_number"]].id if c.get("part_number") in parts else None
    await session.flush()
    counts["certificates"] = len(data["certificates"])

    # ---- FMEA --------------------------------------------------------------
    for f in data["fmea_rows"]:
        row = (await session.execute(
            select(FmeaRow).where(FmeaRow.fmea_type == f["fmea_type"],
                                  FmeaRow.row_ref == f["row_ref"]))).scalar_one_or_none()
        if row is None:
            row = FmeaRow(fmea_type=f["fmea_type"], row_ref=f["row_ref"])
            session.add(row)
        for k in ("sku", "item_function", "failure_mode", "effect", "severity",
                  "occurrence", "detection", "action_priority", "prevention_control",
                  "detection_control", "recommended_action", "responsibility_function",
                  "target_gate", "status"):
            setattr(row, k, f.get(k))
        owner = users.get(f.get("owner_username") or "")
        row.owner_user_id = owner.id if owner else None
        row.linked_part_id = (parts[f["linked_part"]].id
                              if f.get("linked_part") in parts else None)
    await session.flush()
    counts["fmea_rows"] = len(data["fmea_rows"])

    # ---- control plan --------------------------------------------------------
    for c in data["control_items"]:
        row = (await session.execute(
            select(ControlPlanItem)
            .where(ControlPlanItem.control_ref == c["control_ref"]))).scalar_one_or_none()
        if row is None:
            row = ControlPlanItem(control_ref=c["control_ref"])
            session.add(row)
        for k in ("sku", "station", "characteristic", "specification", "method",
                  "classification", "linked_fmea_ref"):
            setattr(row, k, c.get(k))
        row.linked_part_id = (parts[c["linked_part"]].id
                              if c.get("linked_part") in parts else None)
    await session.flush()
    counts["control_items"] = len(data["control_items"])

    # ---- design parameters -----------------------------------------------------
    for p in data["design_parameters"]:
        row = (await session.execute(
            select(DesignParameter)
            .where(DesignParameter.code == p["code"]))).scalar_one_or_none()
        if row is None:
            row = DesignParameter(code=p["code"])
            session.add(row)
        row.sku = p["sku"]
        row.part_id = parts[p["part_number"]].id if p.get("part_number") in parts else None
        row.name = p["name"]
        row.measured_value = (Decimal(str(p["measured_value"]))
                              if p.get("measured_value") is not None else None)
        row.unit = p.get("unit")
        row.min_value = Decimal(str(p["min_value"])) if p.get("min_value") is not None else None
        row.standard_ref = p.get("standard_ref")
        row.method = p.get("method")
        row.verified = p.get("verified", False)
        row.is_placeholder = p.get("is_placeholder", False)
    await session.flush()
    counts["design_parameters"] = len(data["design_parameters"])

    # ---- gates ------------------------------------------------------------------
    for g in data["gates"]:
        row = (await session.execute(
            select(GateInstance).where(GateInstance.sku == g["sku"],
                                       GateInstance.gate_code == g["gate_code"]))
        ).scalar_one_or_none()
        if row is None:
            row = GateInstance(sku=g["sku"], gate_code=g["gate_code"])
            session.add(row)
        row.state = g["state"]
        row.opened_at = row.opened_at or utcnow()
        if g["state"] == "PASSED" and row.closed_at is None:
            row.closed_at = utcnow()
            row.closed_by = "seed"
    counts["gates"] = len(data["gates"])

    await session.commit()
    return counts


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reset", action="store_true", help="drop all tables first")
    ap.add_argument("--seed-file", type=Path, default=None)
    args = ap.parse_args()

    settings = get_settings()
    path = args.seed_file or settings.seed_path
    if not path.exists():
        raise SystemExit(f"Seed file not found: {path}. Run scripts/make_seed.py first.")

    if args.reset:
        await drop_all()
    await create_all()

    data = json.loads(path.read_text())
    factory = get_sessionmaker()
    async with factory() as session:
        counts = await seed(session, data)

    print(f"seeded {settings.database_url.split('@')[-1]}")
    for k, v in counts.items():
        print(f"  {k:<20} {v}")
    await dispose_engine()


if __name__ == "__main__":
    asyncio.run(main())

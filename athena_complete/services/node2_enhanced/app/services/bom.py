"""Multi-level BOM: explosion, where-used and cost roll-up.

Both traversals are recursive CTEs executed in the database rather than N+1
round trips from Python. That is not premature optimisation — ECN impact
analysis calls ``where_used`` for every affected part, and a kit BOM is four
levels deep across four SKUs.

Revision resolution is the subtle part. A BOM line either pins
``child_revision_id`` or floats. A floating line resolves to the child's latest
*released* revision, which is expressed as a grouped sub-select rather than
PostgreSQL's ``DISTINCT ON`` so the same SQL runs on SQLite in the test suite.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import Numeric, String, and_, cast, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from ..enums import LifecycleState
from ..models import BomLine, Part, PartRevision
from ..schemas import BomExplosion, BomNode, WhereUsedEntry

log = logging.getLogger(__name__)

# A consumer-electronics BOM is 3-5 levels. Anything past this is a data error,
# and the cap is what stops a cycle from spinning the database rather than
# returning an answer.
MAX_BOM_DEPTH = 12


class BomError(Exception):
    """Raised for structural BOM problems the caller must fix."""


def _effective_revision_subquery():
    """part_id -> id of the latest RELEASED revision.

    ``MAX(id)`` is the latest because revision rows are only ever appended.
    Deliberately portable: ``DISTINCT ON`` would tie this to PostgreSQL and the
    test suite runs on SQLite.
    """
    return (
        select(PartRevision.part_id.label("part_id"),
               func.max(PartRevision.id).label("rev_id"))
        .where(PartRevision.lifecycle_state == LifecycleState.RELEASED)
        .group_by(PartRevision.part_id)
        .subquery("eff_rev")
    )


async def resolve_revision(session: AsyncSession, part_number: str,
                           rev: str | None = None) -> tuple[Part, PartRevision]:
    """Find a part and one of its revisions.

    With ``rev`` omitted, returns the latest released revision; if nothing is
    released, the latest revision of any state, because a pre-G2 SKU legitimately
    has no released revision yet and the caller still needs to see its structure.
    """
    part = (await session.execute(
        select(Part).where(Part.part_number == part_number))).scalar_one_or_none()
    if part is None:
        raise BomError(f"Unknown part_number {part_number!r}.")

    stmt = select(PartRevision).where(PartRevision.part_id == part.id)
    if rev:
        stmt = stmt.where(PartRevision.rev == rev)
    else:
        stmt = stmt.order_by(
            # released first, then newest
            (PartRevision.lifecycle_state == LifecycleState.RELEASED).desc(),
            PartRevision.id.desc(),
        )
    revision = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if revision is None:
        raise BomError(
            f"{part_number} has no revision {rev!r}." if rev
            else f"{part_number} has no revisions at all."
        )
    return part, revision


async def explode(session: AsyncSession, part_number: str, rev: str | None = None,
                  max_level: int = MAX_BOM_DEPTH) -> BomExplosion:
    """Explode a BOM downward into a nested tree with a cost roll-up."""
    root_part, root_rev = await resolve_revision(session, part_number, rev)
    eff = _effective_revision_subquery()

    child_part = aliased(Part, name="child_part")
    child_rev = aliased(PartRevision, name="child_rev")

    # ---- anchor: the root revision's own lines -------------------------
    resolved_child_rev = func.coalesce(BomLine.child_revision_id, eff.c.rev_id)
    anchor = (
        select(
            BomLine.id.label("line_id"),
            BomLine.child_part_id.label("part_id"),
            resolved_child_rev.label("rev_id"),
            BomLine.child_revision_id.label("pinned_rev_id"),
            BomLine.find_number.label("find_number"),
            BomLine.quantity.label("qty"),
            cast(BomLine.quantity, Numeric(18, 6)).label("ext_qty"),
            literal(1).label("level"),
            # Path starts at the child; the root prefix is added in Python because
            # two adjacent bind parameters either side of `||` are ambiguous to
            # PostgreSQL. The cast is required: a recursive CTE demands the same
            # type in both terms, and varchar(64) || text widens to plain varchar.
            cast(child_part.part_number, String(400)).label("path"),
            BomLine.reference_designator.label("ref_des"),
        )
        .join(child_part, child_part.id == BomLine.child_part_id)
        .outerjoin(eff, eff.c.part_id == BomLine.child_part_id)
        .where(BomLine.parent_revision_id == root_rev.id)
        .cte("bom_tree", recursive=True)
    )

    # ---- recursive step: each resolved child's own lines ---------------
    bl = aliased(BomLine, name="bl")
    cp2 = aliased(Part, name="cp2")
    eff2 = _effective_revision_subquery()
    resolved2 = func.coalesce(bl.child_revision_id, eff2.c.rev_id)
    recursive = (
        select(
            bl.id,
            bl.child_part_id,
            resolved2,
            bl.child_revision_id,
            bl.find_number,
            bl.quantity,
            cast(anchor.c.ext_qty * bl.quantity, Numeric(18, 6)),
            anchor.c.level + 1,
            cast(anchor.c.path + literal(" / ") + cp2.part_number, String(400)),
            bl.reference_designator,
        )
        .join(cp2, cp2.id == bl.child_part_id)
        .outerjoin(eff2, eff2.c.part_id == bl.child_part_id)
        .where(and_(bl.parent_revision_id == anchor.c.rev_id,
                    anchor.c.level < max_level))
    )
    tree_cte = anchor.union_all(recursive)

    rows = (await session.execute(
        select(
            tree_cte.c.line_id, tree_cte.c.part_id, tree_cte.c.rev_id,
            tree_cte.c.pinned_rev_id, tree_cte.c.find_number, tree_cte.c.qty,
            tree_cte.c.ext_qty, tree_cte.c.level, tree_cte.c.path, tree_cte.c.ref_des,
            Part.part_number, Part.name, Part.part_type, Part.supply_mode,
            child_rev.rev, child_rev.lifecycle_state, child_rev.unit_cost_usd,
        )
        .join(Part, Part.id == tree_cte.c.part_id)
        .outerjoin(child_rev, child_rev.id == tree_cte.c.rev_id)
        .order_by(tree_cte.c.level, tree_cte.c.path)
    )).all()

    warnings: list[str] = []
    nodes: list[BomNode] = []
    by_path: dict[str, BomNode] = {}
    total = Decimal("0")
    seen_parts: set[str] = set()

    for r in rows:
        qty = Decimal(str(r.qty))
        ext = Decimal(str(r.ext_qty))
        unit_cost = Decimal(str(r.unit_cost_usd)) if r.unit_cost_usd is not None else None
        ext_cost = (unit_cost * ext).quantize(Decimal("0.0001")) if unit_cost is not None else None
        seen_parts.add(r.part_number)

        if r.rev is None:
            warnings.append(
                f"{r.part_number} (level {r.level}) has no released revision — the BOM line "
                "floats and currently resolves to nothing. Release a revision or pin one."
            )
        elif r.lifecycle_state != LifecycleState.RELEASED and r.pinned_rev_id is not None:
            warnings.append(
                f"{r.part_number} rev {r.rev} is pinned but is {r.lifecycle_state}, not "
                "RELEASED. A gate will not accept it as evidence."
            )

        # Only count cost for leaves and pinned sub-assemblies once: a parent's
        # cost is the sum of its children, so adding both double-counts. Parents
        # here carry no unit cost of their own by construction (see the seed).
        if ext_cost is not None:
            total += ext_cost

        node = BomNode(
            part_number=r.part_number, name=r.name, part_type=r.part_type,
            revision=r.rev, lifecycle_state=r.lifecycle_state, level=r.level,
            find_number=r.find_number, quantity_per_parent=qty, extended_quantity=ext,
            unit_cost_usd=unit_cost, extended_cost_usd=ext_cost,
            supply_mode=r.supply_mode, revision_pinned=r.pinned_rev_id is not None,
            # the stored path is relative to the root; prefix it for display
            path=f"{root_part.part_number} / {r.path}",
        )
        by_path[r.path] = node
        parent_path = r.path.rsplit(" / ", 1)[0]
        if r.level == 1 or parent_path not in by_path:
            nodes.append(node)
        else:
            by_path[parent_path].children.append(node)

    depth = max((r.level for r in rows), default=0)
    if depth >= max_level:
        warnings.append(
            f"BOM traversal hit the depth cap of {max_level} levels. This usually means a "
            "cycle: a part that is, transitively, its own child. Check the deepest path."
        )

    return BomExplosion(
        sku=root_part.part_number, revision=root_rev.rev,
        lifecycle_state=root_rev.lifecycle_state,
        generated_at=datetime.now(timezone.utc),
        max_level=depth, line_count=len(rows), distinct_part_count=len(seen_parts),
        rolled_up_cost_usd=total.quantize(Decimal("0.0001")),
        tree=nodes, warnings=sorted(set(warnings)),
    )


async def where_used(session: AsyncSession, part_number: str,
                     max_level: int = MAX_BOM_DEPTH) -> list[WhereUsedEntry]:
    """Walk upward: every assembly and SKU that consumes this part.

    This is what makes ECN impact analysis correct rather than approximate. A
    cell substitution has to reach the kit, not just the powerbank.
    """
    part = (await session.execute(
        select(Part).where(Part.part_number == part_number))).scalar_one_or_none()
    if part is None:
        raise BomError(f"Unknown part_number {part_number!r}.")

    parent_rev = aliased(PartRevision, name="parent_rev")
    parent_part = aliased(Part, name="parent_part")

    anchor = (
        select(
            parent_part.id.label("part_id"),
            BomLine.quantity.label("qty"),
            literal(1).label("level"),
            cast(parent_part.part_number, String(400)).label("path"),
        )
        .join(parent_rev, parent_rev.id == BomLine.parent_revision_id)
        .join(parent_part, parent_part.id == parent_rev.part_id)
        .where(BomLine.child_part_id == part.id)
        .cte("where_used", recursive=True)
    )

    bl = aliased(BomLine, name="bl_up")
    pr2 = aliased(PartRevision, name="pr2")
    pp2 = aliased(Part, name="pp2")
    recursive = (
        select(
            pp2.id, bl.quantity, anchor.c.level + 1,
            cast(pp2.part_number + literal(" / ") + anchor.c.path, String(400)),
        )
        .join(pr2, pr2.id == bl.parent_revision_id)
        .join(pp2, pp2.id == pr2.part_id)
        .where(and_(bl.child_part_id == anchor.c.part_id,
                    anchor.c.level < max_level))
    )
    cte = anchor.union_all(recursive)

    rows = (await session.execute(
        select(cte.c.part_id, cte.c.qty, cte.c.level, cte.c.path,
               Part.part_number, Part.name, Part.is_sellable)
        .join(Part, Part.id == cte.c.part_id)
        .order_by(cte.c.level, Part.part_number)
    )).all()

    out: list[WhereUsedEntry] = []
    seen: set[tuple[str, str]] = set()
    for r in rows:
        key = (r.part_number, r.path)
        if key in seen:
            continue
        seen.add(key)
        out.append(WhereUsedEntry(
            part_number=r.part_number, name=r.name, level=r.level,
            path=f"{r.path} / {part.part_number}",
            sku=r.part_number if r.is_sellable else None,
            quantity_per_parent=Decimal(str(r.qty)),
        ))
    return out


async def affected_skus(session: AsyncSession, part_number: str) -> list[str]:
    """Every sellable SKU that ultimately contains this part, plus itself."""
    entries = await where_used(session, part_number)
    skus = {e.part_number for e in entries if e.sku}
    self_part = (await session.execute(
        select(Part).where(Part.part_number == part_number))).scalar_one_or_none()
    if self_part is not None and self_part.is_sellable:
        skus.add(self_part.part_number)
    return sorted(skus)


async def would_create_cycle(session: AsyncSession, parent_part_id: int,
                             child_part_id: int) -> bool:
    """True if adding parent -> child would make the graph cyclic.

    Checked before every insert. A cyclic BOM is not a slow query, it is an
    infinite one, and it is trivially easy to create by hand.
    """
    if parent_part_id == child_part_id:
        return True
    parent = (await session.execute(
        select(Part.part_number).where(Part.id == parent_part_id))).scalar_one_or_none()
    if parent is None:
        return False
    # Is the prospective parent already somewhere beneath the prospective child?
    child_pn = (await session.execute(
        select(Part.part_number).where(Part.id == child_part_id))).scalar_one()
    try:
        tree = await explode(session, child_pn)
    except BomError:
        return False

    def walk(nodes: list[BomNode]) -> bool:
        for n in nodes:
            if n.part_number == parent:
                return True
            if walk(n.children):
                return True
        return False

    return walk(tree.tree)


async def add_line(session: AsyncSession, parent_revision: PartRevision,
                   child_part: Part, quantity: Decimal,
                   find_number: int | None = None,
                   child_revision: PartRevision | None = None,
                   reference_designator: str | None = None,
                   notes: str | None = None) -> BomLine:
    """Insert one BOM line with the structural guards applied."""
    if parent_revision.lifecycle_state == LifecycleState.RELEASED:
        raise BomError(
            f"{parent_revision.part.part_number if parent_revision.part else 'parent'} rev "
            f"{parent_revision.rev} is RELEASED. Released structure is immutable — raise an "
            "ECN and create a new revision instead."
        )
    if await would_create_cycle(session, parent_revision.part_id, child_part.id):
        raise BomError(
            f"Adding {child_part.part_number} under revision {parent_revision.id} would "
            "create a cycle: the parent already appears beneath the child."
        )
    if find_number is None:
        current = (await session.execute(
            select(func.max(BomLine.find_number))
            .where(BomLine.parent_revision_id == parent_revision.id))).scalar()
        find_number = (current or 0) + 1

    line = BomLine(
        parent_revision_id=parent_revision.id, child_part_id=child_part.id,
        child_revision_id=child_revision.id if child_revision else None,
        find_number=find_number, quantity=quantity,
        reference_designator=reference_designator, notes=notes,
    )
    session.add(line)
    return line


async def cost_rollup(session: AsyncSession, part_number: str,
                      rev: str | None = None) -> dict[str, Any]:
    """Cost summary for a SKU, split by supply mode."""
    exp = await explode(session, part_number, rev)

    by_mode: dict[str, Decimal] = {}
    leaves = 0

    def walk(nodes: list[BomNode]) -> None:
        nonlocal leaves
        for n in nodes:
            if n.extended_cost_usd is not None:
                by_mode[n.supply_mode or "UNSPECIFIED"] = (
                    by_mode.get(n.supply_mode or "UNSPECIFIED", Decimal("0"))
                    + n.extended_cost_usd)
                leaves += 1
            walk(n.children)

    walk(exp.tree)
    return {
        "sku": exp.sku, "revision": exp.revision,
        "rolled_up_cost_usd": exp.rolled_up_cost_usd,
        "costed_lines": leaves,
        "by_supply_mode": {k: v.quantize(Decimal("0.0001")) for k, v in sorted(by_mode.items())},
        "warnings": exp.warnings,
    }

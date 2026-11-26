"""Rule registry and evaluation context.

A gate rule is a small async function that answers one question about one SKU
and returns a structured verdict. Rules are registered by decorator so adding a
gate criterion is a self-contained change: no dispatch table to update, no
``if gate == ...`` chain to extend.

The context exists so that twelve rules do not run twelve BOM explosions. Every
accessor caches, and the cache lives for exactly one evaluation.
"""
from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import Settings, get_settings
from ..enums import (ArtifactStatus, ArtifactType, CertificateStatus, ECNState, FmeaType,
                     GateCode, GateState, ImpactState, ReviewState, RuleSeverity,
                     RuleStatus)
from ..models import (Artifact, Certificate, ControlPlanItem, DesignParameter, ECN,
                      ECNAffectedItem, ECNImpact, FmeaRow, GateInstance, Part)
from ..services import bom as bom_svc

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RuleResult:
    """One rule's verdict."""
    rule_id: str
    status: RuleStatus
    detail: str
    remediation: str | None = None
    object_refs: list[str] = field(default_factory=list)
    # Artifact names this rule confirmed. Merged into ``passed_artifacts``.
    evidence: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.status is RuleStatus.FAILED


RuleFunc = Callable[["RuleContext"], Awaitable[RuleResult]]


@dataclass(slots=True)
class GateRule:
    rule_id: str
    gate: GateCode
    title: str
    func: RuleFunc
    severity: RuleSeverity = RuleSeverity.BLOCKING
    applies_to: str = "all SKUs"
    standard_ref: str | None = None


_REGISTRY: dict[str, GateRule] = {}
_BY_GATE: dict[GateCode, list[GateRule]] = {}


def rule(rule_id: str, *, gate: GateCode, title: str,
         severity: RuleSeverity = RuleSeverity.BLOCKING,
         applies_to: str = "all SKUs", standard_ref: str | None = None):
    """Register a gate rule."""

    def _wrap(fn: RuleFunc) -> RuleFunc:
        if rule_id in _REGISTRY:
            raise RuntimeError(f"Duplicate rule_id {rule_id!r}.")
        gr = GateRule(rule_id=rule_id, gate=gate, title=title, func=fn,
                      severity=severity, applies_to=applies_to,
                      standard_ref=standard_ref)
        _REGISTRY[rule_id] = gr
        _BY_GATE.setdefault(gate, []).append(gr)
        return fn

    return _wrap


def rules_for(gate: GateCode, inherit: bool = True) -> list[GateRule]:
    """Rules for a gate.

    With ``inherit`` (the default), every blocking rule from every earlier gate
    is re-run. A gate does not un-require what an earlier gate required, and
    without this a G4 pass could sit on a G2 criterion that has since regressed.
    """
    own = list(_BY_GATE.get(gate, []))
    if not inherit:
        return own
    inherited = [
        r for g, rs in _BY_GATE.items() if g.ordinal < gate.ordinal
        for r in rs if r.severity is RuleSeverity.BLOCKING
    ]
    seen: set[str] = set()
    out: list[GateRule] = []
    for r in inherited + own:
        if r.rule_id not in seen:
            seen.add(r.rule_id)
            out.append(r)
    return out


def all_rules() -> list[GateRule]:
    return sorted(_REGISTRY.values(), key=lambda r: (r.gate.ordinal, r.rule_id))


def get_rule(rule_id: str) -> GateRule | None:
    return _REGISTRY.get(rule_id)


# =====================================================================
# Evaluation context
# =====================================================================
class RuleContext:
    """Everything a rule may read, loaded once and cached.

    ``scope_skus`` is the key idea. Evaluating a bundled kit must see the
    adapter's creepage and the powerbank's BMS FMEA rows, because a kit whose
    children are not ready is not ready. For a single SKU the scope is just
    itself.
    """

    def __init__(self, session: AsyncSession, sku: str, part: Part,
                 gate: GateCode, settings: Settings | None = None) -> None:
        self.session = session
        self.sku = sku
        self.part = part
        self.gate = gate
        self.settings = settings or get_settings()
        self._cache: dict[str, Any] = {}

    # ---------------------------------------------------------- scope
    async def scope_skus(self) -> list[str]:
        if "scope" not in self._cache:
            skus = {self.sku}
            try:
                exp = await bom_svc.explode(self.session, self.sku)
            except bom_svc.BomError:
                exp = None
            if exp is not None:
                def walk(nodes):
                    for n in nodes:
                        if n.part_type == "SKU":
                            skus.add(n.part_number)
                        walk(n.children)
                walk(exp.tree)
            self._cache["scope"] = sorted(skus)
        return self._cache["scope"]

    async def child_skus(self) -> list[str]:
        return [s for s in await self.scope_skus() if s != self.sku]

    async def scope_parts(self) -> list[Part]:
        if "scope_parts" not in self._cache:
            skus = await self.scope_skus()
            rows = (await self.session.execute(
                select(Part).where(Part.part_number.in_(skus)))).scalars().all()
            self._cache["scope_parts"] = list(rows)
        return self._cache["scope_parts"]

    async def explosion(self):
        if "explosion" not in self._cache:
            try:
                self._cache["explosion"] = await bom_svc.explode(self.session, self.sku)
            except bom_svc.BomError as exc:
                log.warning("explosion failed for %s: %s", self.sku, exc)
                self._cache["explosion"] = None
        return self._cache["explosion"]

    async def bom_parts(self) -> list[Part]:
        """Every distinct part in the SKU's structure, as ORM objects."""
        if "bom_parts" not in self._cache:
            exp = await self.explosion()
            numbers: set[str] = set()
            if exp is not None:
                def walk(nodes):
                    for n in nodes:
                        numbers.add(n.part_number)
                        walk(n.children)
                walk(exp.tree)
            rows = (await self.session.execute(
                select(Part).where(Part.part_number.in_(numbers or {"__none__"})))
            ).scalars().all()
            self._cache["bom_parts"] = list(rows)
        return self._cache["bom_parts"]

    # ---------------------------------------------------------- evidence
    async def artifacts(self) -> list[Artifact]:
        if "artifacts" not in self._cache:
            skus = await self.scope_skus()
            rows = (await self.session.execute(
                select(Artifact).where(Artifact.sku.in_(skus)))).scalars().all()
            self._cache["artifacts"] = list(rows)
        return self._cache["artifacts"]

    async def released_artifacts(self, *types: ArtifactType,
                                 sku: str | None = None) -> list[Artifact]:
        wanted = {t.value for t in types}
        return [a for a in await self.artifacts()
                if a.artifact_type in wanted
                and a.status == ArtifactStatus.RELEASED
                and (sku is None or a.sku == sku)]

    async def fmea_rows(self, fmea_type: FmeaType | None = None) -> list[FmeaRow]:
        if "fmea" not in self._cache:
            skus = await self.scope_skus()
            rows = (await self.session.execute(
                select(FmeaRow).where(FmeaRow.sku.in_(skus)))).scalars().all()
            self._cache["fmea"] = list(rows)
        rows = self._cache["fmea"]
        return [r for r in rows if fmea_type is None or r.fmea_type == fmea_type]

    async def control_plan(self) -> list[ControlPlanItem]:
        if "control" not in self._cache:
            skus = await self.scope_skus()
            rows = (await self.session.execute(
                select(ControlPlanItem).where(ControlPlanItem.sku.in_(skus)))
            ).scalars().all()
            self._cache["control"] = list(rows)
        return self._cache["control"]

    async def parameters(self) -> list[DesignParameter]:
        if "params" not in self._cache:
            skus = await self.scope_skus()
            rows = (await self.session.execute(
                select(DesignParameter).where(DesignParameter.sku.in_(skus)))
            ).scalars().all()
            self._cache["params"] = list(rows)
        return self._cache["params"]

    async def certificates(self) -> list[Certificate]:
        if "certs" not in self._cache:
            skus = await self.scope_skus()
            rows = (await self.session.execute(
                select(Certificate).where(Certificate.sku.in_(skus)))).scalars().all()
            self._cache["certs"] = list(rows)
        return self._cache["certs"]

    async def open_ecns(self) -> list[ECN]:
        """ECNs touching this scope that are not yet resolved."""
        if "ecns" not in self._cache:
            skus = await self.scope_skus()
            part_ids = [p.id for p in await self.bom_parts()]
            stmt = (
                select(ECN).distinct()
                .outerjoin(ECNAffectedItem, ECNAffectedItem.ecn_id == ECN.id)
                .where(ECN.state.in_([ECNState.DRAFT, ECNState.UNDER_REVIEW,
                                      ECNState.APPROVED]))
                .where((ECN.primary_sku.in_(skus)) |
                       (ECNAffectedItem.part_id.in_(part_ids or [-1])))
            )
            self._cache["ecns"] = list((await self.session.execute(stmt)).scalars().all())
        return self._cache["ecns"]

    async def pending_impacts(self) -> list[ECNImpact]:
        if "impacts" not in self._cache:
            skus = await self.scope_skus()
            rows = (await self.session.execute(
                select(ECNImpact).where(
                    ECNImpact.sku.in_(skus),
                    ECNImpact.state.in_([ImpactState.PENDING_RE_REVIEW,
                                         ImpactState.IN_PROGRESS]),
                ))).scalars().all()
            self._cache["impacts"] = list(rows)
        return self._cache["impacts"]

    async def gate_states(self) -> dict[tuple[str, str], str]:
        if "gates" not in self._cache:
            skus = await self.scope_skus()
            rows = (await self.session.execute(
                select(GateInstance).where(GateInstance.sku.in_(skus)))).scalars().all()
            self._cache["gates"] = {(g.sku, g.gate_code): g.state for g in rows}
        return self._cache["gates"]

    # ---------------------------------------------------------- helpers
    async def electronic_skus(self) -> list[str]:
        """SKUs carrying a PCBA of their own, and therefore needing a schematic.

        Traversal stops at a nested SKU: a bundled kit does not need its own
        schematic because its children already have theirs.
        """
        if "electronic" not in self._cache:
            self._cache["electronic"] = await self._skus_containing(
                lambda name: any(k in name for k in ("pcba", "pcb", "soc", "module", "ic")))
        return self._cache["electronic"]

    async def _skus_containing(self, predicate) -> list[str]:
        """SKUs whose *own* structure contains a matching part.

        Descent stops at a nested SKU, so a characteristic belonging to a packed
        unit is attributed to that unit and not also to every kit containing it.
        """
        out: set[str] = set()
        for sku in await self.scope_skus():
            try:
                exp = await bom_svc.explode(self.session, sku)
            except bom_svc.BomError:
                continue

            def walk(nodes) -> bool:
                for n in nodes:
                    if n.part_type == "SKU":
                        continue          # that SKU answers for its own content
                    if predicate((n.name or "").lower()) or predicate(
                            (n.part_number or "").lower()):
                        return True
                    if walk(n.children):
                        return True
                return False

            if exp and walk(exp.tree):
                out.add(sku)
        return sorted(out)

    async def adapter_skus(self) -> list[str]:
        """SKUs subject to mains-isolation rules.

        Identified by a part carrying a mains-facing characteristic rather than
        by a name match, so a second adapter variant is covered automatically.
        """
        params = await self.parameters()
        return sorted({p.sku for p in params if p.code.startswith("CREEPAGE")})

    async def battery_skus(self) -> list[str]:
        """SKUs containing a lithium cell — the UN 38.3 transport population."""
        if "battery" not in self._cache:
            # Descent stops at a nested SKU: the kit does not import cells, the
            # powerbank does, and the transport summary belongs to the shipment
            # that actually moves them.
            self._cache["battery"] = await self._skus_containing(
                lambda name: "cell" in name or "battery" in name)
        return self._cache["battery"]

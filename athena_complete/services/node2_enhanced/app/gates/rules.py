"""The gate rule set.

Every rule answers one question, states the failure in terms an engineer can
act on, and names the objects at fault. A blocking reason that says
"requirements not met" is useless; a blocking reason that says which measured
value, against which clause, on which part, is a work instruction.

G2 carries the rules the specification names. Later gates are lighter but real,
and every gate inherits the blocking rules of the gates before it.
"""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from ..enums import (ActionPriority, ArtifactType, CertificateStatus, CertificationScheme,
                     ECNState, FmeaType, GateCode, GateState, LifecycleState, ReviewState,
                     RuleSeverity, RuleStatus)
from .registry import RuleContext, RuleResult, rule

def num(value: Decimal | float | None) -> str:
    """Format without ``Numeric``'s trailing zeros.

    ``Decimal('2.4000')`` reading back as "2.4000mm" in a blocking reason is the
    kind of detail that makes an engineer distrust the whole message.
    """
    if value is None:
        return "-"
    d = Decimal(str(value)).normalize()
    return f"{d:f}" if d != d.to_integral_value() else str(int(d))


def mm(value: Decimal | float | None) -> str:
    """Format a linear dimension, always with at least one decimal place.

    A safety limit written as "3mm" invites the reader to wonder whether it was
    rounded. "3.0mm" says the tenth is significant, which for an isolation
    distance it is.
    """
    if value is None:
        return "-"
    d = Decimal(str(value)).normalize()
    return f"{d:.1f}" if d == d.to_integral_value() else f"{d:f}"


OK = RuleStatus.PASSED
BAD = RuleStatus.FAILED
NA = RuleStatus.NOT_APPLICABLE
WARN = RuleStatus.WARNING


# =====================================================================
# G1 — Feasibility
# =====================================================================
@rule("RULE_BOM_STRUCTURE_EXISTS", gate=GateCode.G1_FEASIBILITY,
      title="A costed BOM structure exists for the SKU",
      applies_to="all SKUs")
async def bom_structure_exists(ctx: RuleContext) -> RuleResult:
    exp = await ctx.explosion()
    if exp is None or not exp.tree:
        return RuleResult(
            "RULE_BOM_STRUCTURE_EXISTS", BAD,
            f"{ctx.sku} has no BOM structure. Nothing downstream — cost, sourcing, "
            "FMEA scope or gate evidence — can be derived from an empty structure.",
            remediation="POST /api/v1/bom/{sku}/revisions/{rev}/lines to build the structure.",
        )
    if exp.rolled_up_cost_usd <= 0:
        return RuleResult(
            "RULE_BOM_STRUCTURE_EXISTS", BAD,
            f"{ctx.sku} has {exp.line_count} BOM lines but a rolled-up cost of zero. "
            "Unit costs are missing on every revision, so the cost ceiling agreed at "
            "G1 cannot be tested.",
            remediation="Set unit_cost_usd on the child part revisions.",
        )
    return RuleResult(
        "RULE_BOM_STRUCTURE_EXISTS", OK,
        f"{exp.line_count} lines over {exp.max_level} levels, "
        f"{exp.distinct_part_count} distinct parts, rolled up to "
        f"${exp.rolled_up_cost_usd} per unit.",
    )


# =====================================================================
# G2 — Design / EVT  (the specification's gate)
# =====================================================================
@rule("RULE_CAD_MASTER_MODEL", gate=GateCode.G2_DESIGN_EVT,
      title="Parametric CAD master model released for every SKU in scope",
      applies_to="all SKUs", standard_ref="ISO 10303-242 (STEP AP242)")
async def cad_master_model(ctx: RuleContext) -> RuleResult:
    """A released, *parametric* master. A dumb STEP export is not a master model:
    the tool maker cannot drive a change from it, so accepting one would let the
    gate pass on evidence that cannot support the next stage."""
    missing: list[str] = []
    evidence: list[str] = []
    for sku in await ctx.scope_skus():
        arts = await ctx.released_artifacts(ArtifactType.CAD_MASTER_MODEL, sku=sku)
        parametric = [a for a in arts if a.is_parametric]
        if not parametric:
            non_param = [a.name for a in arts]
            missing.append(
                f"{sku}: no released parametric CAD master"
                + (f" (found non-parametric {', '.join(non_param)})" if non_param else "")
            )
        else:
            evidence.extend(a.name for a in parametric)
    if missing:
        return RuleResult(
            "RULE_CAD_MASTER_MODEL", BAD,
            "Parametric CAD master model missing or not released — " + "; ".join(missing),
            remediation="Release the master from the CAD vault; the webhook ingests it "
                        "as CAD_MASTER_MODEL with is_parametric=true.",
            object_refs=[m.split(":")[0] for m in missing],
        )
    return RuleResult("RULE_CAD_MASTER_MODEL", OK,
                      f"Released parametric master model present for all "
                      f"{len(await ctx.scope_skus())} SKUs in scope.",
                      evidence=evidence)


@rule("RULE_SCHEMATIC_RELEASED", gate=GateCode.G2_DESIGN_EVT,
      title="Schematics released for every electronic SKU in scope",
      applies_to="SKUs carrying a PCBA")
async def schematic_released(ctx: RuleContext) -> RuleResult:
    missing: list[str] = []
    evidence: list[str] = []
    for sku in await ctx.electronic_skus():
        arts = await ctx.released_artifacts(ArtifactType.SCHEMATIC, sku=sku)
        if not arts:
            drafts = [a.name for a in await ctx.artifacts()
                      if a.artifact_type == ArtifactType.SCHEMATIC and a.sku == sku]
            missing.append(f"{sku}"
                           + (f" (in {drafts[0]} but not released)" if drafts else ""))
        else:
            evidence.extend(a.name for a in arts)
    if missing:
        return RuleResult(
            "RULE_SCHEMATIC_RELEASED", BAD,
            "Released schematics missing for: " + ", ".join(missing)
            + ". EVT boards cannot be fabricated from an unreleased schematic, and any "
              "board built from one is not traceable to a controlled design.",
            remediation="Release the schematic in the EDA vault.",
            object_refs=[m.split(" ")[0] for m in missing],
        )
    return RuleResult("RULE_SCHEMATIC_RELEASED", OK,
                      f"Schematics released for {len(evidence)} design(s).",
                      evidence=evidence)


@rule("RULE_DFMEA_DRAFTED", gate=GateCode.G2_DESIGN_EVT,
      title="DFMEA drafted for every SKU in scope",
      applies_to="all SKUs", standard_ref="AIAG-VDA FMEA Handbook, 1st ed.")
async def dfmea_drafted(ctx: RuleContext) -> RuleResult:
    rows = await ctx.fmea_rows(FmeaType.DFMEA)
    by_sku: dict[str, int] = {}
    for r in rows:
        by_sku[r.sku] = by_sku.get(r.sku, 0) + 1
    missing = [s for s in await ctx.scope_skus()
               if by_sku.get(s, 0) == 0 and s != ctx.sku]
    # The kit itself may legitimately have no DFMEA of its own if every child does.
    if not rows:
        missing = await ctx.scope_skus()
    if missing:
        return RuleResult(
            "RULE_DFMEA_DRAFTED", BAD,
            "No DFMEA rows exist for: " + ", ".join(missing)
            + ". A design entering EVT with no recorded failure-mode analysis has no "
              "basis for its detection controls or its test plan.",
            remediation="Run the DFMEA workshop and load the rows before G2 exit.",
            object_refs=missing,
        )
    return RuleResult("RULE_DFMEA_DRAFTED", OK,
                      f"{len(rows)} DFMEA rows across "
                      f"{len(by_sku)} SKUs ({', '.join(f'{k}:{v}' for k, v in sorted(by_sku.items()))}).")


@rule("RULE_UNASSIGNED_FMEA", gate=GateCode.G2_DESIGN_EVT,
      title="Zero High Action Priority FMEA items without a named owner",
      applies_to="all SKUs", standard_ref="AIAG-VDA FMEA Handbook §6 (Optimisation)")
async def unassigned_fmea(ctx: RuleContext) -> RuleResult:
    """AIAG-VDA requires every High AP item to carry a *responsible person* and a
    target date. A department name is not a person: 'Design Eng.' cannot be held
    to a date, and an action with no individual owner is the single most common
    way a High AP item reaches production unclosed."""
    rows = await ctx.fmea_rows()
    high = [r for r in rows if r.action_priority == ActionPriority.HIGH]
    if not high:
        return RuleResult("RULE_UNASSIGNED_FMEA", OK,
                          "No High Action Priority items in scope.")

    unassigned = [r for r in high if r.owner_user_id is None]
    if unassigned and ctx.settings.fmea_high_ap_requires_named_owner:
        bits = []
        for r in unassigned[:6]:
            fn = f", carries function '{r.responsibility_function}'" if r.responsibility_function else ""
            bits.append(f"High AP item '{r.item_function}' ({r.row_ref}, {r.sku}) "
                        f"missing owner assignment{fn}")
        more = f" (+{len(unassigned) - 6} more)" if len(unassigned) > 6 else ""
        return RuleResult(
            "RULE_UNASSIGNED_FMEA", BAD,
            "; ".join(bits) + more,
            remediation="POST /api/v1/fmea/{row_ref}/owner with a named user and a target "
                        "date. A responsibility function is not an owner.",
            object_refs=[r.row_ref for r in unassigned],
        )

    no_date = [r for r in high if r.target_date is None and r.target_gate is None]
    if no_date:
        return RuleResult(
            "RULE_UNASSIGNED_FMEA", BAD,
            f"{len(no_date)} High AP items are owned but carry neither a target date nor "
            f"a target gate: {', '.join(r.row_ref for r in no_date[:8])}.",
            remediation="Set target_date or target_gate on each.",
            object_refs=[r.row_ref for r in no_date],
        )
    return RuleResult("RULE_UNASSIGNED_FMEA", OK,
                      f"All {len(high)} High AP items carry a named owner and a target.")


@rule("RULE_CREEPAGE_CHECK", gate=GateCode.G2_DESIGN_EVT,
      title="Creepage and clearance verified against the isolation minimum",
      applies_to="mains-connected SKUs (adapter)",
      standard_ref="IEC 62368-1 §5.4.2 / Table 10-13")
async def creepage_check(ctx: RuleContext) -> RuleResult:
    """Numeric, not documentary. The gate reads the measured value and compares
    it to the minimum, and refuses a value that is still a placeholder — the
    programme's own design package flags these as provisional."""
    adapters = await ctx.adapter_skus()
    if not adapters:
        return RuleResult("RULE_CREEPAGE_CHECK", NA,
                          "No mains-connected SKU in scope; isolation distances do not apply.")

    minimum = Decimal(str(ctx.settings.creepage_min_mm_adapter))
    params = [p for p in await ctx.parameters() if p.code.startswith("CREEPAGE")]
    failures: list[str] = []
    evidence: list[str] = []

    for p in params:
        where = f"{p.sku} {p.name}"
        if p.measured_value is None:
            failures.append(f"{where} has no measured value recorded")
            continue
        measured = Decimal(str(p.measured_value))
        if p.is_placeholder:
            failures.append(
                f"{where} carries {mm(measured)}mm but the value is flagged as a placeholder, "
                "not a measurement — the design package records creepage, clearance and "
                "Hi-Pot values as provisional pending the safety assessment"
            )
            continue
        required = Decimal(str(p.min_value)) if p.min_value is not None else minimum
        if measured < required:
            failures.append(
                f"{p.name} creepage distance is {mm(measured)}mm, minimum required is "
                f"{mm(required)}mm under {p.standard_ref or 'IEC 62368-1'}"
            )
        elif not p.verified:
            failures.append(
                f"{where} measures {mm(measured)}mm against {mm(required)}mm but has not been "
                "verified — no signed evidence is attached"
            )
        else:
            evidence.append(f"{p.code}={mm(measured)}{p.unit or 'mm'}")

    if not params:
        failures.append(
            f"No creepage or clearance parameter is recorded for {', '.join(adapters)}. "
            "The isolation distances are the adapter's primary safety characteristic and "
            "cannot be assumed"
        )

    if failures:
        # The specification's example wording for this rule is the single-fault
        # case, so a lone failure is reported on its own rather than as a list.
        detail = failures[0] if len(failures) == 1 else "; ".join(failures)
        return RuleResult(
            "RULE_CREEPAGE_CHECK", BAD, detail,
            remediation=f"Measure on the released layout and record with "
                        f"a {ctx.settings.creepage_guard_band_pct:.0f}% guard band "
                        f"(DFMEA D-02 recommended action), then set verified=true.",
            object_refs=[p.code for p in params] or adapters,
        )
    return RuleResult("RULE_CREEPAGE_CHECK", OK,
                      f"Isolation distances verified for {', '.join(adapters)}: "
                      + ", ".join(evidence),
                      evidence=evidence)


@rule("RULE_PRELIMINARY_BOM_RELEASED", gate=GateCode.G2_DESIGN_EVT,
      title="Preliminary BOM exported and released",
      applies_to="all SKUs")
async def preliminary_bom(ctx: RuleContext) -> RuleResult:
    arts = await ctx.released_artifacts(ArtifactType.BOM_EXPORT)
    if not arts:
        return RuleResult(
            "RULE_PRELIMINARY_BOM_RELEASED", BAD,
            f"No released BOM export for {ctx.sku}. Sourcing cannot place EVT material "
            "against an unreleased structure.",
            remediation="Export the BOM from the vault; the webhook ingests it as "
                        "BOM_EXPORT.",
        )
    return RuleResult("RULE_PRELIMINARY_BOM_RELEASED", OK,
                      f"{len(arts)} released BOM export(s).",
                      evidence=[a.name for a in arts])


@rule("RULE_CHILD_GATE_PRECEDENCE", gate=GateCode.G2_DESIGN_EVT,
      title="Constituent SKUs have formally signed off this gate",
      severity=RuleSeverity.ADVISORY,
      applies_to="bundled kits and multi-SKU assemblies")
async def child_gate_precedence(ctx: RuleContext) -> RuleResult:
    """Advisory at the design gates, blocking at SOP.

    A kit evaluation already applies every rule to every child's own evidence,
    because the child SKUs are in scope — so a child that is not ready produces
    its own blocking reasons here and reporting it twice is noise. What this rule
    adds is the *formal* sign-off ordering, which matters at SOP and is advice
    beforehand. ``RULE_CHILD_GATES_CLOSED_AT_SOP`` enforces it.
    """
    if not ctx.settings.enforce_child_gate_precedence:
        return RuleResult("RULE_CHILD_GATE_PRECEDENCE", NA,
                          "Child gate precedence is disabled by configuration "
                          "(ATHENA_ENFORCE_CHILD_GATE_PRECEDENCE=false).")
    children = await ctx.child_skus()
    if not children:
        return RuleResult("RULE_CHILD_GATE_PRECEDENCE", NA,
                          f"{ctx.sku} has no child SKUs.")
    states = await ctx.gate_states()
    behind = [
        f"{c} is {states.get((c, ctx.gate.value), GateState.NOT_STARTED.value)}"
        for c in children
        if states.get((c, ctx.gate.value)) not in (GateState.PASSED, GateState.WAIVED)
    ]
    if behind:
        return RuleResult(
            "RULE_CHILD_GATE_PRECEDENCE", WARN,
            f"{ctx.sku} cannot pass {ctx.gate.value} while its constituent SKUs have not: "
            + ", ".join(behind)
            + ". A kit inherits every child's risk; passing it first would record a "
              "readiness the line cannot deliver.",
            remediation="Evaluate and pass the child SKUs first.",
            object_refs=children,
        )
    return RuleResult("RULE_CHILD_GATE_PRECEDENCE", OK,
                      f"All {len(children)} child SKUs have passed {ctx.gate.value}.")


@rule("RULE_NO_PENDING_RE_REVIEW", gate=GateCode.G2_DESIGN_EVT,
      title="No risk or control record left in Pending Re-Review",
      applies_to="all SKUs")
async def no_pending_re_review(ctx: RuleContext) -> RuleResult:
    """An ECN flags records for re-review. Passing a gate with those still open
    means the gate was evaluated against analysis that predates the change."""
    fmea = [r for r in await ctx.fmea_rows() if r.review_state != ReviewState.CURRENT]
    controls = [c for c in await ctx.control_plan() if c.review_state != ReviewState.CURRENT]
    impacts = await ctx.pending_impacts()
    if not (fmea or controls or impacts):
        return RuleResult("RULE_NO_PENDING_RE_REVIEW", OK,
                          "No records are awaiting re-review.")
    refs = [r.row_ref for r in fmea] + [c.control_ref for c in controls]
    ecns = sorted({str(i.ecn_id) for i in impacts})
    return RuleResult(
        "RULE_NO_PENDING_RE_REVIEW", BAD,
        f"{len(fmea)} FMEA rows and {len(controls)} control-plan items are Pending "
        f"Re-Review after an engineering change"
        + (f" (ECN id {', '.join(ecns)})" if ecns else "")
        + f": {', '.join(refs[:10])}"
        + (f" +{len(refs) - 10} more" if len(refs) > 10 else "")
        + ". The gate would otherwise be judged on pre-change analysis.",
        remediation="Complete the re-review and POST /api/v1/ecn/{id}/impacts/{impact_id}.",
        object_refs=refs,
    )


@rule("RULE_NO_UNAPPROVED_ECN", gate=GateCode.G2_DESIGN_EVT,
      title="No unapproved engineering change is open against the scope",
      applies_to="all SKUs")
async def no_unapproved_ecn(ctx: RuleContext) -> RuleResult:
    open_ecns = [e for e in await ctx.open_ecns()
                 if e.state in (ECNState.DRAFT, ECNState.UNDER_REVIEW)]
    if not open_ecns:
        return RuleResult("RULE_NO_UNAPPROVED_ECN", OK,
                          "No draft or in-review ECN against this scope.")
    return RuleResult(
        "RULE_NO_UNAPPROVED_ECN", BAD,
        f"{len(open_ecns)} engineering change(s) are open and undecided against this "
        f"scope: " + ", ".join(f"{e.ecn_number} ({e.state}) — {e.title}"
                               for e in open_ecns[:5])
        + ". The design being gated is not the design that will be built.",
        remediation="Take the change board decision, or withdraw the ECN.",
        object_refs=[e.ecn_number for e in open_ecns],
    )


@rule("RULE_UN38_3_BEFORE_CELL_SHIPMENT", gate=GateCode.G2_DESIGN_EVT,
      title="UN 38.3 test summary valid before lithium cells are transported",
      applies_to="SKUs containing a lithium cell",
      standard_ref="UN Manual of Tests and Criteria, Part III §38.3")
async def un38_3_before_shipment(ctx: RuleContext) -> RuleResult:
    """UN 38.3 is usually filed as a G4 compliance item. That is too late.

    The cells are SKD imports: they must be air-freighted to reach the EVT
    build, and no carrier will accept a lithium cell without a test summary. The
    constraint therefore binds at G2 exit, when the EVT build is authorised —
    not at the DVT compliance pack.
    """
    skus = await ctx.battery_skus()
    if not skus:
        return RuleResult("RULE_UN38_3_BEFORE_CELL_SHIPMENT", NA,
                          "No lithium cell in scope.")
    certs = {c.sku: c for c in await ctx.certificates()
             if c.scheme == CertificationScheme.UN38_3}
    bad: list[str] = []
    evidence: list[str] = []
    for sku in skus:
        c = certs.get(sku)
        if c is None:
            bad.append(f"{sku}: no UN 38.3 test summary on file")
        elif c.status != CertificateStatus.VALID:
            bad.append(f"{sku}: UN 38.3 is {c.status}")
        elif c.expires_on and c.expires_on < date.today():
            bad.append(f"{sku}: UN 38.3 expired {c.expires_on.isoformat()}")
        else:
            evidence.append(f"UN38.3 {c.certificate_number or ''} ({sku})".strip())
    if bad:
        return RuleResult(
            "RULE_UN38_3_BEFORE_CELL_SHIPMENT", BAD,
            "; ".join(bad)
            + ". The cells are SKD imports and cannot be air-freighted for the EVT build "
              "without a valid test summary, so this binds at G2 exit rather than at the "
              "DVT compliance pack.",
            remediation="Obtain the summary from the cell supplier and register it in the "
                        "Compliance Vault.",
            object_refs=skus,
        )
    return RuleResult("RULE_UN38_3_BEFORE_CELL_SHIPMENT", OK,
                      f"Valid UN 38.3 for {', '.join(skus)}.", evidence=evidence)


# ---- advisory rules: reported, do not block --------------------------
@rule("RULE_SAFETY_PARAMETER_PLACEHOLDERS", gate=GateCode.G2_DESIGN_EVT,
      title="Safety-relevant parameters are measurements, not placeholders",
      severity=RuleSeverity.ADVISORY, applies_to="all SKUs")
async def safety_placeholders(ctx: RuleContext) -> RuleResult:
    placeholders = [p for p in await ctx.parameters()
                    if p.is_placeholder and not p.code.startswith("CREEPAGE")]
    if not placeholders:
        return RuleResult("RULE_SAFETY_PARAMETER_PLACEHOLDERS", OK,
                          "No placeholder values outside the isolation set.")
    return RuleResult(
        "RULE_SAFETY_PARAMETER_PLACEHOLDERS", WARN,
        f"{len(placeholders)} safety-relevant parameters are still provisional: "
        + ", ".join(f"{p.code} ({num(p.measured_value)}{p.unit or ''})" for p in placeholders)
        + ". These will block at G3, when the design freeze releases them to tooling.",
        remediation="Close them out of the safety assessment before the tooling freeze.",
        object_refs=[p.code for p in placeholders],
    )


@rule("RULE_NRND_COMPONENT_DESIGNED_IN", gate=GateCode.G2_DESIGN_EVT,
      title="No NRND or end-of-life component designed into the structure",
      severity=RuleSeverity.ADVISORY, applies_to="all SKUs")
async def nrnd_designed_in(ctx: RuleContext) -> RuleResult:
    risky = [p for p in await ctx.bom_parts()
             if (p.lifecycle_risk or "").upper() in ("NRND", "EOL")]
    if not risky:
        return RuleResult("RULE_NRND_COMPONENT_DESIGNED_IN", OK,
                          "No NRND or EOL components in the structure.")
    return RuleResult(
        "RULE_NRND_COMPONENT_DESIGNED_IN", WARN,
        f"{len(risky)} component(s) carry a supply-continuity flag: "
        + ", ".join(f"{p.part_number} ({p.lifecycle_risk})" for p in risky)
        + ". Designing in a part the vendor has stopped recommending pushes a "
          "substitution ECN into the ramp, when it is most expensive.",
        remediation="Qualify an alternate now, or accept the risk in writing at the gate.",
        object_refs=[p.part_number for p in risky],
    )


# =====================================================================
# G3 — EVT exit
# =====================================================================
@rule("RULE_GERBER_RELEASED", gate=GateCode.G3_EVT_EXIT,
      title="Gerber / fabrication data released",
      applies_to="SKUs carrying a PCBA")
async def gerber_released(ctx: RuleContext) -> RuleResult:
    arts = await ctx.released_artifacts(ArtifactType.GERBER)
    if not arts:
        return RuleResult(
            "RULE_GERBER_RELEASED", BAD,
            "No released fabrication data. DVT boards cannot be ordered.",
            remediation="Release Gerbers (RS-274X or IPC-2581) from the EDA vault.",
        )
    return RuleResult("RULE_GERBER_RELEASED", OK,
                      f"{len(arts)} released fabrication package(s).",
                      evidence=[a.name for a in arts])


@rule("RULE_EVT_TEST_REPORT", gate=GateCode.G3_EVT_EXIT,
      title="EVT test report released for every SKU in scope",
      applies_to="all SKUs")
async def evt_test_report(ctx: RuleContext) -> RuleResult:
    missing = []
    evidence = []
    for sku in await ctx.scope_skus():
        arts = [a for a in await ctx.released_artifacts(ArtifactType.TEST_REPORT, sku=sku)
                if "EVT" in (a.name or "").upper() or a.gate_code == GateCode.G3_EVT_EXIT]
        (evidence.extend(a.name for a in arts) if arts else missing.append(sku))
    if missing:
        return RuleResult("RULE_EVT_TEST_REPORT", BAD,
                          "No released EVT test report for: " + ", ".join(missing),
                          object_refs=missing)
    return RuleResult("RULE_EVT_TEST_REPORT", OK,
                      f"EVT reports released for {len(evidence)} SKU(s).",
                      evidence=evidence)


@rule("RULE_NO_PLACEHOLDER_AT_FREEZE", gate=GateCode.G3_EVT_EXIT,
      title="No provisional safety value survives the design freeze",
      applies_to="all SKUs", standard_ref="IEC 62368-1")
async def no_placeholder_at_freeze(ctx: RuleContext) -> RuleResult:
    """Advisory at G2, blocking here. G3 releases the design to tooling, and a
    provisional isolation or Hi-Pot value cut into steel is a recall, not a
    rework."""
    placeholders = [p for p in await ctx.parameters() if p.is_placeholder]
    if not placeholders:
        return RuleResult("RULE_NO_PLACEHOLDER_AT_FREEZE", OK,
                          "All parameters carry measured values.")
    return RuleResult(
        "RULE_NO_PLACEHOLDER_AT_FREEZE", BAD,
        f"{len(placeholders)} provisional parameter(s) would be frozen into tooling: "
        + ", ".join(p.code for p in placeholders)
        + ". A provisional isolation or dielectric value cut into steel is a recall, "
          "not a rework.",
        remediation="Complete the safety assessment and record measured values.",
        object_refs=[p.code for p in placeholders],
    )


# =====================================================================
# G4 — DVT exit
# =====================================================================
@rule("RULE_MARKET_ACCESS_CERTIFICATES", gate=GateCode.G4_DVT_EXIT,
      title="BIS CRS and WPC ETA valid for every SKU placed on the Indian market",
      applies_to="all SKUs", standard_ref="BIS CRS (MeitY order) / WPC ETA")
async def market_access_certificates(ctx: RuleContext) -> RuleResult:
    required = {CertificationScheme.BIS_CRS, CertificationScheme.WPC_ETA}
    horizon = date.today() + timedelta(days=ctx.settings.certificate_min_validity_days)
    certs = await ctx.certificates()
    problems: list[str] = []
    evidence: list[str] = []
    for sku in await ctx.scope_skus():
        for scheme in sorted(required):
            c = next((x for x in certs if x.sku == sku and x.scheme == scheme), None)
            if c is None:
                problems.append(f"{sku}: no {scheme} record")
            elif c.status != CertificateStatus.VALID:
                problems.append(f"{sku}: {scheme} is {c.status}")
            elif c.expires_on and c.expires_on < horizon:
                problems.append(
                    f"{sku}: {scheme} expires {c.expires_on.isoformat()}, inside the "
                    f"{ctx.settings.certificate_min_validity_days}-day window — it would "
                    "lapse during the ramp"
                )
            else:
                evidence.append(f"{scheme} {c.certificate_number or ''} ({sku})".strip())
    if problems:
        return RuleResult("RULE_MARKET_ACCESS_CERTIFICATES", BAD,
                          "; ".join(problems),
                          remediation="Verify against the Compliance Vault: "
                                      "POST /api/v1/compliance/refresh?sku=...",
                          object_refs=await ctx.scope_skus())
    return RuleResult("RULE_MARKET_ACCESS_CERTIFICATES", OK,
                      f"{len(evidence)} market-access certificates valid.",
                      evidence=evidence)


@rule("RULE_CONTROL_PLAN_COMPLETE", gate=GateCode.G4_DVT_EXIT,
      title="Every Critical characteristic has a control-plan entry",
      applies_to="all SKUs")
async def control_plan_complete(ctx: RuleContext) -> RuleResult:
    items = await ctx.control_plan()
    if not items:
        return RuleResult("RULE_CONTROL_PLAN_COMPLETE", BAD,
                          f"No control plan exists for {ctx.sku}.",
                          remediation="Derive the control plan from the PFMEA.")
    critical = [i for i in items if (i.classification or "").lower() == "critical"]
    orphan = [i.control_ref for i in critical if not i.linked_fmea_ref]
    if orphan:
        return RuleResult(
            "RULE_CONTROL_PLAN_COMPLETE", BAD,
            f"{len(orphan)} Critical control(s) are not traceable to a PFMEA row: "
            + ", ".join(orphan)
            + ". A control with no failure mode behind it cannot be justified or "
              "safely removed.",
            object_refs=orphan,
        )
    return RuleResult("RULE_CONTROL_PLAN_COMPLETE", OK,
                      f"{len(items)} control-plan items, {len(critical)} Critical, "
                      "all traceable to a PFMEA row.")


# =====================================================================
# G5 — PVT exit
# =====================================================================
@rule("RULE_PFMEA_OWNERS_CLOSED", gate=GateCode.G5_PVT_EXIT,
      title="Every High AP process risk is closed before rate production",
      applies_to="all SKUs")
async def pfmea_owners_closed(ctx: RuleContext) -> RuleResult:
    rows = [r for r in await ctx.fmea_rows(FmeaType.PFMEA)
            if r.action_priority == ActionPriority.HIGH]
    open_rows = [r for r in rows if (r.status or "").lower() != "closed"]
    if open_rows:
        return RuleResult(
            "RULE_PFMEA_OWNERS_CLOSED", BAD,
            f"{len(open_rows)} High AP process risks remain open entering rate "
            f"production: " + ", ".join(f"{r.row_ref} ({r.item_function})"
                                        for r in open_rows[:6]),
            object_refs=[r.row_ref for r in open_rows],
        )
    return RuleResult("RULE_PFMEA_OWNERS_CLOSED", OK,
                      f"All {len(rows)} High AP process risks closed.")


@rule("RULE_GOLDEN_SAMPLE_APPROVED", gate=GateCode.G5_PVT_EXIT,
      title="Golden sample and cosmetic limit samples approved",
      applies_to="all SKUs")
async def golden_sample(ctx: RuleContext) -> RuleResult:
    arts = await ctx.released_artifacts(ArtifactType.GOLDEN_SAMPLE)
    if not arts:
        return RuleResult(
            "RULE_GOLDEN_SAMPLE_APPROVED", BAD,
            "No approved golden sample. Cosmetic standards would be argued on the line "
            "during the ramp rather than agreed before it.",
            remediation="Approve golden and limit samples with the line and QA.",
        )
    return RuleResult("RULE_GOLDEN_SAMPLE_APPROVED", OK,
                      f"{len(arts)} approved sample record(s).",
                      evidence=[a.name for a in arts])


# =====================================================================
# G6 — SOP
# =====================================================================
@rule("RULE_ALL_CERTIFICATES_VALID_AT_SOP", gate=GateCode.G6_SOP,
      title="Every registered certificate is valid with margin at start of production",
      applies_to="all SKUs")
async def certificates_valid_at_sop(ctx: RuleContext) -> RuleResult:
    horizon = date.today() + timedelta(days=ctx.settings.certificate_min_validity_days)
    bad = [f"{c.sku}/{c.scheme}: {c.status}"
           f"{f' expires {c.expires_on.isoformat()}' if c.expires_on else ''}"
           for c in await ctx.certificates()
           if c.status != CertificateStatus.VALID
           or (c.expires_on and c.expires_on < horizon)]
    if bad:
        return RuleResult("RULE_ALL_CERTIFICATES_VALID_AT_SOP", BAD,
                          "; ".join(bad), object_refs=await ctx.scope_skus())
    return RuleResult("RULE_ALL_CERTIFICATES_VALID_AT_SOP", OK,
                      "All certificates valid beyond the ramp window.")


@rule("RULE_CHILD_GATES_CLOSED_AT_SOP", gate=GateCode.G6_SOP,
      title="Every constituent SKU has passed every gate before the kit ships",
      applies_to="bundled kits")
async def child_gates_closed_at_sop(ctx: RuleContext) -> RuleResult:
    """Blocking, unlike its advisory counterpart at G2.

    A kit's first-pass yield is the product of its children's, not their average,
    and its availability in any week is set by the worst line. Shipping a kit
    whose children have not formally cleared their gates records a readiness the
    line cannot deliver.
    """
    children = await ctx.child_skus()
    if not children:
        return RuleResult("RULE_CHILD_GATES_CLOSED_AT_SOP", NA,
                          f"{ctx.sku} has no child SKUs.")
    states = await ctx.gate_states()
    behind: list[str] = []
    for child in children:
        for g in GateCode:
            if g.ordinal > ctx.gate.ordinal:
                continue
            st = states.get((child, g.value), GateState.NOT_STARTED.value)
            if st not in (GateState.PASSED, GateState.WAIVED):
                behind.append(f"{child} {g.value} is {st}")
    if behind:
        return RuleResult(
            "RULE_CHILD_GATES_CLOSED_AT_SOP", BAD,
            f"{ctx.sku} cannot start production while its constituent SKUs have open "
            "gates: " + ", ".join(behind[:8])
            + (f" (+{len(behind) - 8} more)" if len(behind) > 8 else ""),
            remediation="Close the child SKU gates first.",
            object_refs=children,
        )
    return RuleResult("RULE_CHILD_GATES_CLOSED_AT_SOP", OK,
                      f"All {len(children)} constituent SKUs have cleared every gate.")

"""Deterministic N1 engine — no LLM, no network, no database. Pure functions over N0-admitted evidence.

N1.2 feature engineering   entity resolution, service-category tagging, spend-type classification
                           (VERIFIED_BUDGET | INDICATIVE | HYPOTHETICAL), FX normalization with an explicit
                           rate date, evidence freshness scoring
N1.3 opportunity scoring   transparent 0–100 *heuristic prioritization* (not a win probability)
N1.4 guardrails            unsupported claims, duplicate spend, superseded corrigenda, stale evidence,
                           malformed values, missing mandatory attachments, revoked rights
N1.5 cost-weighted ranking hard-policy gates first, then net value of permissible actions; never executes
N1.6 report assembly       SAM buckets, shortlist, exclusions, risk flags, citations, Markdown

All money is ``Decimal`` quantized to 0.01 with ROUND_HALF_EVEN.
"""
from __future__ import annotations

import difflib
import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Iterable, Optional

from ..models import MarketObservation, TenderDeadline
from ..rules import canonical_json, sha256_hex
from .models import (SAM_DISCLAIMER, SCORE_DISCLAIMER, ActionOptimizationRequest, BlockedAction, BuyerAccount,
                     Citation, EvidenceAttestation, EvidenceFeature, Exclusion, FxConversion, FxRate, MarketReport,
                     MlReadiness, OpportunityAssessment, RankedAction, ReportRequest, RiskFlag, SamSummary,
                     ScoreComponent)

ENGINE_VERSION = "n1-engine/1.0.0"
MONEY = Decimal("0.01")
ZERO = Decimal("0.00")
DEFAULT_SLA_DAYS = {"TENDER_NOTICE": 180, "CONTRACT_AWARD": 730, "TRADE_STATISTIC": 730, "BUDGET_DOCUMENT": 365,
                    "BUYER_INTERVIEW": 180, "CRM_RECORD": 365}
FX_STALE_DAYS = 31
MAX_PLAUSIBLE_AMOUNT = Decimal("1000000000000")
ML_REQUIRED_LABELED_OUTCOMES = 200
PRECEDENCE = {"CONTRACT_AWARD": 5, "BUDGET_DOCUMENT": 4, "TENDER_NOTICE": 3, "CRM_RECORD": 2, "BUYER_INTERVIEW": 2,
              "TRADE_STATISTIC": 1}
OFFICIAL_CONNECTORS = {"SAM_GOV", "TED_EU", "INDIA_CPPP"}

# exclusions: HARD ones disqualify the evidence entirely; SAM_ONLY ones keep it for scoring only
HARD_EXCLUSIONS = {"FUTURE_DATED", "STALE_EVIDENCE", "SUPERSEDED_CORRIGENDUM", "MISSING_MANDATORY_ATTACHMENT",
                   "ATTESTATION_REJECTED", "UNSUPPORTED_CLAIM", "RIGHTS_REVOKED"}
SAM_ONLY_EXCLUSIONS = {"NO_AMOUNT", "FX_MISSING", "OUT_OF_CATEGORY", "DUPLICATE_SPEND", "MALFORMED_AMOUNT",
                       "IMPLAUSIBLE_AMOUNT"}


class InvariantViolation(RuntimeError):
    """Raised if synthetic or unattested spend would ever reach verified_real_sam."""


def money(v: Decimal) -> Decimal:
    return Decimal(v).quantize(MONEY, rounding=ROUND_HALF_EVEN)


def _utc_date(dt: datetime) -> date:
    return dt.date()


# ==============================================================================================
# N1.2 — entity resolution
# ==============================================================================================
_LEGAL_SUFFIXES = {"inc", "incorporated", "ltd", "limited", "llc", "plc", "gmbh", "ag", "sa", "sas", "srl", "bv",
                   "nv", "pvt", "private", "co", "corp", "corporation", "company", "pte", "oy", "ab", "spa"}


def normalize_name(name: str) -> str:
    s = name.lower().replace("&", " and ")
    s = re.sub(r"\bu\.?\s?s\.?\b", "us", s)
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    tokens = [t for t in s.split() if t]
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    if tokens and tokens[0] == "the":
        tokens = tokens[1:]
    return " ".join(tokens)


def name_similarity(a: str, b: str) -> float:
    ta, tb = " ".join(sorted(set(a.split()))), " ".join(sorted(set(b.split())))
    return difflib.SequenceMatcher(None, ta, tb).ratio()


def _ids(identifiers: dict[str, str]) -> set[tuple[str, str]]:
    return {(k.strip().lower(), v.strip().upper()) for k, v in identifiers.items() if v and v.strip()}


def account_id_for(tenant_id: str, country: Optional[str], normalized: str) -> str:
    return "ACC-" + sha256_hex(f"{tenant_id}|{country or '--'}|{normalized}".encode())[:16]


def match_account(name: str, identifiers: dict[str, str], country: Optional[str],
                  accounts: Iterable[BuyerAccount], fuzzy_threshold: float = 0.92
                  ) -> tuple[Optional[BuyerAccount], str]:
    """Identifier match, then exact normalized name, then fuzzy name — always within one country."""
    norm, ids = normalize_name(name), _ids(identifiers)
    candidates = [a for a in accounts if not country or not a.country or a.country == country]
    if ids:
        for a in candidates:
            if ids & _ids(a.identifiers):
                return a, "IDENTIFIER"
    for a in candidates:
        if norm and (a.normalized_name == norm or norm in {normalize_name(x) for x in a.aliases}):
            return a, "EXACT_NAME"
    best, best_score = None, 0.0
    for a in candidates:
        if a.country and country and a.country == country:
            score = name_similarity(norm, a.normalized_name)
            if score > best_score:
                best, best_score = a, score
    if best is not None and best_score >= fuzzy_threshold:
        return best, "FUZZY_NAME"
    return None, "NEW"


# ==============================================================================================
# N1.2 — service-category tagging
# ==============================================================================================
TAXONOMY: list[tuple[str, dict[str, tuple[str, ...]]]] = [
    ("power_electronics", {"cpv": ("3171", "3112", "3115", "3168"), "naics": ("335311", "335999", "334416"),
                           "hs": ("8504",), "keywords": ("power supply", "power supplies", "converter", "inverter",
                                                         "rectifier", "battery charger", "power electronics",
                                                         "ups system")}),
    ("pcb_assembly", {"cpv": ("31711", "3171111"), "naics": ("334412", "334418"), "hs": ("8534",),
                      "keywords": ("printed circuit", "pcb", "smt assembly", "electronic assembly")}),
    ("embedded_systems", {"cpv": ("3271", "4826", "3242"), "naics": ("334419", "541512"), "hs": ("8471", "8542"),
                          "keywords": ("embedded", "firmware", "controller board", "microcontroller")}),
    ("test_measurement", {"cpv": ("3850", "3851", "3852"), "naics": ("334515",), "hs": ("9030",),
                          "keywords": ("test equipment", "oscilloscope", "measurement instrument", "calibration")}),
    ("industrial_automation", {"cpv": ("4250", "4296"), "naics": ("333995", "335314"), "hs": ("8537",),
                               "keywords": ("plc", "automation", "scada", "motor drive")}),
]


def keyword_categories(text: str) -> list[str]:
    t = f" {text} ".lower()
    return [cat for cat, rule in TAXONOMY if any(re.search(rf"\b{re.escape(k)}\b", t) for k in rule["keywords"])]


def tag_category(obs: MarketObservation) -> tuple[Optional[str], str]:
    if obs.service_category:
        return obs.service_category, "GIVEN"
    for cat, rule in TAXONOMY:
        for scheme in ("cpv", "naics", "hs"):
            for code in obs.classification_codes.get(scheme, []):
                if any(str(code).startswith(p) for p in rule[scheme]):
                    return cat, "CODE"
    text = f" {(obs.title or '')} {(obs.description or '')} ".lower()
    for cat, rule in TAXONOMY:
        if any(re.search(rf"\b{re.escape(k)}\b", text) for k in rule["keywords"]):
            return cat, "KEYWORD"
    return None, "NONE"


# ==============================================================================================
# N1.2 — spend-type classification
# ==============================================================================================
def classify_spend(obs: MarketObservation) -> tuple[str, str]:
    if obs.synthetic:
        return "HYPOTHETICAL", "synthetic record"
    if obs.amount is None:
        return "HYPOTHETICAL", "no stated amount"
    t = obs.observation_type
    if t == "TRADE_STATISTIC":
        return "HYPOTHETICAL", "sector trade flow is a TAM proxy, not buyer spend"
    if t == "CONTRACT_AWARD":
        return "VERIFIED_BUDGET", "awarded contract value"
    if t in ("BUDGET_DOCUMENT", "CRM_RECORD", "BUYER_INTERVIEW"):
        status = obs.budget_status
        if status == "APPROVED":
            return "VERIFIED_BUDGET", f"{t.lower()} states an approved budget"
        if status == "PLANNED":
            return "INDICATIVE", f"{t.lower()} states a planned budget"
        return "HYPOTHETICAL", f"{t.lower()} budget is exploratory or unstated"
    if t == "TENDER_NOTICE":
        return "INDICATIVE", "tender estimated value (not a commitment)"
    return "INDICATIVE", "unclassified amount"


# ==============================================================================================
# N1.2 — currency normalization (explicit FX date) and freshness
# ==============================================================================================
def fx_lookup(rates: list[FxRate], currency: str, on: date) -> Optional[FxRate]:
    usable = [r for r in rates if r.currency == currency and r.as_of <= on]
    return max(usable, key=lambda r: r.as_of) if usable else None


def convert(obs: MarketObservation, base: str, rates: list[FxRate], on: date) -> Optional[FxConversion]:
    if obs.amount is None:
        return None
    if obs.amount.currency == base:
        rate = FxRate(currency=base, rate_to_base=Decimal(1), as_of=on, source="identity")
    else:
        rate = fx_lookup(rates, obs.amount.currency, on)
        if rate is None:
            return None
    return FxConversion(original=obs.amount, base_currency=base, rate=rate.rate_to_base, fx_as_of=rate.as_of,
                        fx_source=rate.source, amount_base=money(obs.amount.amount * rate.rate_to_base),
                        fx_stale=(on - rate.as_of).days > FX_STALE_DAYS)


def freshness(obs: MarketObservation, as_of: datetime, overrides: dict[str, int]) -> tuple[int, int, float, Optional[str]]:
    sla = int(overrides.get(obs.observation_type, DEFAULT_SLA_DAYS[obs.observation_type]))
    age = (as_of - obs.published_at).days
    stale = None
    if age > sla:
        stale = f"published {age} days ago; SLA for {obs.observation_type} is {sla} days"
    elif obs.observation_type == "TENDER_NOTICE" and obs.deadline_at is not None and obs.deadline_at < as_of:
        stale = f"tender deadline {obs.deadline_at.date()} has passed"
    score = 0.0 if stale else round(max(0.0, 1 - max(age, 0) / sla), 3)
    return age, sla, score, stale


# ==============================================================================================
# feature construction + N1.4 guardrails
# ==============================================================================================
@dataclass
class EvidenceInput:
    obs: MarketObservation
    revision: int
    sha256: str
    account_id: Optional[str]
    n0_artifact: str
    attestation: Optional[EvidenceAttestation] = None
    blocked: Optional[str] = None          # e.g. "RIGHTS_REVOKED: source rights revoked after intake"


@dataclass
class _Work:
    inp: EvidenceInput
    category: Optional[str]
    category_source: str
    spend_type: str
    spend_reason: str
    fx: Optional[FxConversion]
    age: int
    sla: int
    score: float
    excl: list[str] = field(default_factory=list)
    detail: list[str] = field(default_factory=list)

    def add(self, code: str, detail: str) -> None:
        if code not in self.excl:
            self.excl.append(code)
            self.detail.append(f"{code}: {detail}")

    @property
    def hard(self) -> bool:
        return any(c in HARD_EXCLUSIONS for c in self.excl)


def _attested(inp: EvidenceInput) -> bool:
    a = inp.attestation
    return bool(a and a.status == "ATTESTED" and a.evidence_sha256 == inp.sha256 and not inp.obs.synthetic)


def build_features(inputs: list[EvidenceInput], request: ReportRequest, as_of: datetime) -> list[EvidenceFeature]:
    on = _utc_date(as_of)
    serviceable = set(request.serviceable_categories)
    work: list[_Work] = []
    for inp in inputs:
        o = inp.obs
        cat, cat_src = tag_category(o)
        spend, reason = classify_spend(o)
        fx = convert(o, request.base_currency, request.fx_rates, on)
        age, sla, score, stale = freshness(o, as_of, request.freshness_sla_days)
        w = _Work(inp, cat, cat_src, spend, reason, fx, age, sla, score)
        if inp.blocked:
            w.add("RIGHTS_REVOKED", inp.blocked)
        if o.published_at - as_of > timedelta(days=1):
            w.add("FUTURE_DATED", f"published_at {o.published_at.isoformat()} is after the report date")
        if stale:
            w.add("STALE_EVIDENCE", stale)
        missing = [a.name for a in o.attachments if a.required and not a.present]
        if missing:
            w.add("MISSING_MANDATORY_ATTACHMENT", ", ".join(missing))
        if inp.attestation and inp.attestation.status == "REJECTED" and inp.attestation.evidence_sha256 == inp.sha256:
            w.add("ATTESTATION_REJECTED", f"rejected by {inp.attestation.attested_by}: {inp.attestation.notes}")
        if o.amount is not None and o.source_connector == "MANUAL" and not o.source_url and not o.evidence_snippets:
            w.add("UNSUPPORTED_CLAIM", "manual amount with no source URL and no evidence snippet")
        if o.amount is None:
            w.add("NO_AMOUNT", "no stated amount")
        else:
            if o.amount.amount <= 0:
                w.add("MALFORMED_AMOUNT", f"{o.amount.amount} {o.amount.currency} is not a positive amount")
            elif o.amount.amount > MAX_PLAUSIBLE_AMOUNT:
                w.add("IMPLAUSIBLE_AMOUNT", f"{o.amount.amount} {o.amount.currency} exceeds the plausibility cap")
            if fx is None:
                w.add("FX_MISSING", f"no {o.amount.currency}→{request.base_currency} rate dated on or before {on}")
        if cat not in serviceable:
            w.add("OUT_OF_CATEGORY", f"category {cat or 'untagged'} is not serviceable "
                                     f"({', '.join(sorted(serviceable))})")
        work.append(w)

    _supersede_corrigenda(work)
    _dedupe_spend(work)

    features = []
    for w in work:
        o, attested = w.inp.obs, _attested(w.inp)
        if w.excl:
            counts = "EXCLUDED"
        elif o.synthetic or w.spend_type == "HYPOTHETICAL":
            counts = "HYPOTHETICAL"
        elif w.spend_type == "VERIFIED_BUDGET" and attested:
            counts = "VERIFIED"
        else:
            counts = "INDICATIVE"                   # includes VERIFIED_BUDGET awaiting attestation
        features.append(EvidenceFeature(
            evidence_id=o.evidence_id, evidence_revision=w.inp.revision, evidence_sha256=w.inp.sha256,
            account_id=w.inp.account_id, observation_type=o.observation_type, source_connector=o.source_connector,
            source_url=o.source_url, n0_artifact=w.inp.n0_artifact, service_category=w.category,
            category_source=w.category_source, spend_type=w.spend_type, spend_reason=w.spend_reason, fx=w.fx,
            age_days=w.age, sla_days=w.sla, freshness_score=w.score, attested=attested,
            attestation_id=w.inp.attestation.attestation_id if attested else None, synthetic=o.synthetic,
            exclusions=w.excl, exclusion_detail=w.detail, counts_as=counts))
    return features


def _supersede_corrigenda(work: list[_Work]) -> None:
    """Only the latest corrigendum of a tender counts (same connector + source_record_id)."""
    groups: dict[tuple[str, str], list[_Work]] = {}
    for w in work:
        o = w.inp.obs
        if o.source_record_id:
            groups.setdefault((o.source_connector, o.source_record_id.strip().upper()), []).append(w)
    for members in groups.values():
        if len(members) < 2:
            continue
        latest = max(members, key=lambda w: (w.inp.obs.corrigendum_seq, w.inp.obs.published_at,
                                             w.inp.obs.evidence_id))
        for w in members:
            if w is not latest:
                w.add("SUPERSEDED_CORRIGENDUM", f"superseded by {latest.inp.obs.evidence_id} "
                                                f"(corrigendum {latest.inp.obs.corrigendum_seq})")


def _dedupe_spend(work: list[_Work]) -> None:
    """Union-find over spend-bearing evidence: explicit links, same record id across connectors,
    or same account + amount within 0.5 % + published within 45 days. Keep one per group."""
    cand = [w for w in work if w.fx is not None and not w.hard
            and not {"MALFORMED_AMOUNT", "IMPLAUSIBLE_AMOUNT"} & set(w.excl)]
    parent = {id(w): id(w) for w in cand}
    by_id = {w.inp.obs.evidence_id: w for w in cand}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(id(a)), find(id(b))
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for w in cand:
        for rel in w.inp.obs.related_evidence_ids:
            if rel in by_id:
                union(w, by_id[rel])
    records: dict[str, _Work] = {}
    for w in cand:
        rid = (w.inp.obs.source_record_id or "").strip().upper()
        if rid:
            if rid in records and records[rid].inp.obs.source_connector != w.inp.obs.source_connector:
                union(w, records[rid])
            records.setdefault(rid, w)
    for i, a in enumerate(cand):
        for b in cand[i + 1:]:
            if a.inp.account_id and a.inp.account_id == b.inp.account_id:
                x, y = a.fx.amount_base, b.fx.amount_base
                close = abs(x - y) <= Decimal("0.005") * max(x, y, Decimal(1))
                near = abs((a.inp.obs.published_at - b.inp.obs.published_at).days) <= 45
                if close and near:
                    union(a, b)
    groups: dict[int, list[_Work]] = {}
    for w in cand:
        groups.setdefault(find(id(w)), []).append(w)
    for members in groups.values():
        if len(members) < 2:
            continue
        keep = max(members, key=lambda w: (not w.inp.obs.synthetic, _attested(w.inp),
                                           PRECEDENCE[w.inp.obs.observation_type], w.inp.obs.published_at,
                                           w.inp.obs.evidence_id))
        for w in members:
            if w is not keep:
                w.add("DUPLICATE_SPEND", f"same spend as {keep.inp.obs.evidence_id}")


# ==============================================================================================
# N1.3 — heuristic opportunity scoring
# ==============================================================================================
_ACCESS = {"ACTIVE": (25, "active relationship"), "WARM": (15, "warm relationship"), "COLD": (5, "cold contact"),
           "NONE": (0, "no relationship")}
_STRENGTH = {"VERIFIED": 30.0, "INDICATIVE": 12.0, "HYPOTHETICAL": 3.0}
PENALTY = 5.0
MAX_PENALTY = 30.0


def _open_tenders(feats: list[EvidenceFeature], obs: dict[str, MarketObservation], as_of: datetime):
    return [f for f in feats if f.observation_type == "TENDER_NOTICE" and not set(f.exclusions) & HARD_EXCLUSIONS
            and obs[f.evidence_id].deadline_at and obs[f.evidence_id].deadline_at > as_of]


def quiet_period_accounts(features: list[EvidenceFeature], obs: dict[str, MarketObservation],
                          accounts: dict[str, BuyerAccount], as_of: datetime) -> set[str]:
    """Public buyers with an open tender: contact only through the tender's official channels."""
    out = set()
    for f in _open_tenders(features, obs, as_of):
        acc = accounts.get(f.account_id or "")
        if acc and (acc.buyer_type == "PUBLIC" or f.source_connector in OFFICIAL_CONNECTORS):
            out.add(acc.account_id)
    return out


def score_account(acc: BuyerAccount, feats: list[EvidenceFeature], obs: dict[str, MarketObservation],
                  serviceable: set[str], as_of: datetime) -> Optional[OpportunityAssessment]:
    usable = [f for f in feats if not set(f.exclusions) & HARD_EXCLUSIONS]
    in_cat = [f for f in usable if f.service_category in serviceable]
    if not in_cat:
        return None
    comps, pens, flags, missing = [], [], [], []
    pts, why = _ACCESS[acc.relationship_stage]
    if acc.do_not_contact:
        pts, why = 0, "do-not-contact"
    comps.append(ScoreComponent(name="buyer_access", points=pts, max_points=25, reason=why))
    fit = round(30 * len(in_cat) / len(usable), 1)
    comps.append(ScoreComponent(name="requirements_fit", points=fit, max_points=30,
                                reason=f"{len(in_cat)} of {len(usable)} usable evidence items are in serviceable "
                                       "categories"))
    best, best_id = 0.0, None
    for f in in_cat:
        base = _STRENGTH.get(f.counts_as, 0.0)
        if f.counts_as == "INDICATIVE" and f.spend_type == "VERIFIED_BUDGET":
            base = 18.0                                     # verified budget awaiting attestation
        if f.counts_as == "EXCLUDED":                       # SAM-only exclusion (e.g. no amount)
            base = 2.0 if f.synthetic else 6.0
        s = round(base * max(f.freshness_score, 0.25), 1)
        if s > best:
            best, best_id = s, f.evidence_id
    comps.append(ScoreComponent(name="procurement_evidence_strength", points=best, max_points=30,
                                reason=f"strongest evidence {best_id} (strength × freshness)"))
    open_t = [f for f in _open_tenders(in_cat, obs, as_of)]
    deadlines = sorted(((obs[f.evidence_id].deadline_at, f) for f in open_t), key=lambda x: x[0])
    if deadlines:
        days = (deadlines[0][0] - as_of).days
        timing = (15, f"open tender closes in {days} days") if days <= 60 else (10, f"open tender closes in {days} days")
        if days <= 7:
            flags.append("DEADLINE_IMMINENT")
    elif any(f.observation_type == "CONTRACT_AWARD" and f.age_days <= 365 for f in in_cat):
        timing = (6, "award in the last 12 months (renewal cycle)")
    else:
        timing = (0, "no open tender or recent award")
    comps.append(ScoreComponent(name="timing", points=timing[0], max_points=15, reason=timing[1]))

    def penalize(code: str, reason: str, info: str) -> None:
        pens.append(ScoreComponent(name=code, points=-PENALTY, max_points=0, reason=reason))
        missing.append(info)

    if not acc.identifiers:
        penalize("MISSING_BUYER_IDENTIFIER", "no registry identifier (UEI / VAT / GSTIN)",
                 f"buyer identifier for {acc.legal_name}")
    if not any(f.fx is not None for f in in_cat):
        penalize("NO_SPEND_AMOUNT", "no in-category evidence states an amount", f"spend amount for {acc.legal_name}")
    pending = [f.evidence_id for f in in_cat if f.spend_type == "VERIFIED_BUDGET" and not f.attested
               and not f.synthetic]
    if pending:
        penalize("PENDING_ATTESTATION", f"{len(pending)} verified-budget item(s) not attested",
                 f"reviewer attestation for {', '.join(pending)}")
        flags.append("PENDING_ATTESTATION")
    no_deadline = [f.evidence_id for f in in_cat if f.observation_type == "TENDER_NOTICE"
                   and obs[f.evidence_id].deadline_at is None]
    if no_deadline:
        penalize("TENDER_WITHOUT_DEADLINE", "tender notice without a submission deadline",
                 f"submission deadline for {', '.join(no_deadline)}")
    no_url = [f.evidence_id for f in in_cat if not f.source_url]
    if no_url:
        penalize("MISSING_SOURCE_URL", "evidence without a citable source URL", f"source URL for {', '.join(no_url)}")
    attach = [f.evidence_id for f in feats if "MISSING_MANDATORY_ATTACHMENT" in f.exclusions]
    if attach:
        penalize("MISSING_MANDATORY_ATTACHMENT", "tender documents incomplete",
                 f"mandatory attachments for {', '.join(attach)}")
        flags.append("MISSING_MANDATORY_ATTACHMENT")
    total_pen = max(-MAX_PENALTY, sum(p.points for p in pens))
    score = max(0.0, min(100.0, round(sum(c.points for c in comps) + total_pen, 1)))

    if acc.do_not_contact:
        flags.append("DO_NOT_CONTACT")
    if open_t and (acc.buyer_type == "PUBLIC" or any(f.source_connector in OFFICIAL_CONNECTORS for f in open_t)):
        flags.append("PROCUREMENT_QUIET_PERIOD")
    if acc.synthetic_only or all(f.synthetic for f in in_cat):
        flags.append("SYNTHETIC_ONLY")
    if any("STALE_EVIDENCE" in f.exclusions for f in feats):
        flags.append("STALE_EVIDENCE")
    if any("DUPLICATE_SPEND" in f.exclusions for f in feats):
        flags.append("DUPLICATE_SPEND_EXCLUDED")
    if any(f.fx and f.fx.fx_stale for f in in_cat):
        flags.append("FX_STALE")
    if not acc.named_buyer_validated:
        flags.append("NAMED_BUYER_UNVALIDATED")
    if any(obs[f.evidence_id].classification_codes.get("set_aside") for f in in_cat):
        flags.append("SET_ASIDE_ELIGIBILITY")

    def spend(kind: str) -> Decimal:
        return money(sum((f.fx.amount_base for f in in_cat if f.counts_as == kind and f.fx), ZERO))

    seed, blockers = _rfq_seed(in_cat, obs)
    return OpportunityAssessment(
        opportunity_id=f"OPP-{acc.account_id}", account_id=acc.account_id, legal_name=acc.legal_name,
        country=acc.country, service_categories=sorted({f.service_category for f in in_cat}),
        heuristic_priority_score=score, components=comps, penalties=pens, risk_flags=sorted(set(flags)),
        missing_information=missing, verified_spend_base=spend("VERIFIED"), indicative_spend_base=spend("INDICATIVE"),
        hypothetical_spend_base=spend("HYPOTHETICAL"), evidence_ids=sorted(f.evidence_id for f in in_cat),
        tender_deadlines=[TenderDeadline(evidence_id=f.evidence_id, deadline_at=d, source_url=f.source_url)
                          for d, f in deadlines],
        synthetic_only="SYNTHETIC_ONLY" in flags, rfq_seed_evidence_id=seed, rfq_seed_blockers=blockers)


def _rfq_seed(in_cat: list[EvidenceFeature], obs: dict[str, MarketObservation]) -> tuple[Optional[str], list[str]]:
    real = [f for f in in_cat if not f.synthetic and f.observation_type != "TRADE_STATISTIC"]
    if not real:
        return None, ["no real (non-synthetic) buyer evidence"]
    ranked = sorted(real, key=lambda f: (f.attested, bool(obs[f.evidence_id].requirements),
                                         bool(obs[f.evidence_id].quantity), PRECEDENCE[f.observation_type],
                                         f.freshness_score, f.evidence_id), reverse=True)
    best = ranked[0]
    o = obs[best.evidence_id]
    blockers = []
    if not best.attested:
        blockers.append(f"{best.evidence_id} is not reviewer-attested")
    if not o.requirements:
        blockers.append(f"{best.evidence_id} has no structured requirement clauses")
    if not o.quantity or o.quantity <= 0:
        blockers.append(f"{best.evidence_id} has no quantity")
    return (best.evidence_id if not blockers else None), blockers


# ==============================================================================================
# N1.5 — cost-weighted, non-executing action ranking
# ==============================================================================================
OUTREACH = {"EMAIL_OUTREACH", "REQUEST_BUYER_INTERVIEW"}
CONTACT = OUTREACH | {"ATTEND_PREBID_MEETING", "SUBMIT_CLARIFICATION_QUESTION"}
ALWAYS_BLOCKED = {
    "SUBMIT_BID": ("BID_SUBMISSION_IS_N6", "bids go through N6's governed commercial workflow; N1 never bids"),
    "PORTAL_SCRAPE": ("SCRAPING_PROHIBITED", "portals are ingested only through authorized APIs or drop zones"),
    "AUTO_OUTREACH_CAMPAIGN": ("AUTONOMOUS_OUTREACH_PROHIBITED", "N1 never sends outreach on its own"),
}


def optimize_actions(req: ActionOptimizationRequest, accounts: dict[str, BuyerAccount], quiet: set[str]
                     ) -> tuple[list[RankedAction], list[BlockedAction], Decimal]:
    blocked, allowed = [], []
    for a in req.actions:
        codes, reasons = [], []
        if a.action_type in ALWAYS_BLOCKED:
            c, r = ALWAYS_BLOCKED[a.action_type]
            codes.append(c)
            reasons.append(r)
        if a.currency != req.currency:
            codes.append("CURRENCY_MISMATCH")
            reasons.append(f"costs in {a.currency}, optimization in {req.currency}; no implicit FX")
        acc = accounts.get(a.target_account_id) if a.target_account_id else None
        if a.target_account_id and acc is None:
            codes.append("UNKNOWN_TARGET")
            reasons.append(f"{a.target_account_id} is not an account in this tenant")
        if a.action_type in CONTACT and not a.target_account_id:
            codes.append("TARGET_REQUIRED")
            reasons.append("contact actions need a target account")
        if acc is not None and a.action_type in CONTACT:
            if acc.do_not_contact:
                codes.append("DO_NOT_CONTACT")
                reasons.append(f"{acc.legal_name} is marked do-not-contact")
            if acc.synthetic_only:
                codes.append("SYNTHETIC_TARGET")
                reasons.append(f"{acc.legal_name} exists only in synthetic data")
        if acc is not None and a.action_type in OUTREACH and acc.account_id in quiet:
            codes.append("PROCUREMENT_QUIET_PERIOD")
            reasons.append(f"{acc.legal_name} has an open public tender; use its official clarification channel")
        if codes:
            blocked.append(BlockedAction(action_id=a.action_id, action_type=a.action_type, policy_codes=codes,
                                         reason="; ".join(reasons)))
        else:
            allowed.append(a)
    scored = []
    for a in allowed:
        delay = money(a.delay_cost_per_day * a.expected_delay_days)
        net = money(a.expected_loss_avoided - a.execution_cost - delay)
        scored.append((a, delay, net))
    scored.sort(key=lambda x: (-x[2], x[0].execution_cost, x[0].action_id))
    ranked, spent = [], ZERO
    for i, (a, delay, net) in enumerate(scored, 1):
        positive = net > 0
        within = req.budget_cap is None or spent + a.execution_cost <= req.budget_cap
        recommended = positive and within
        if recommended:
            spent += a.execution_cost
        why = (f"expected loss avoided {money(a.expected_loss_avoided)} − cost {money(a.execution_cost)} − "
               f"delay {delay} = {net} {req.currency}")
        if not positive:
            why += "; not worth doing at these costs"
        elif not within:
            why += "; exceeds the remaining budget cap"
        ranked.append(RankedAction(rank=i, action_id=a.action_id, action_type=a.action_type,
                                   target_account_id=a.target_account_id, execution_cost=money(a.execution_cost),
                                   delay_cost=delay, expected_loss_avoided=money(a.expected_loss_avoided),
                                   net_value=net, recommended=recommended, within_budget=within, rationale=why))
    return ranked, blocked, money(spent)


# ==============================================================================================
# N1.6 — report assembly
# ==============================================================================================
def build_report(*, report_id: str, tenant_id: str, request: ReportRequest, as_of: datetime, generated_at: datetime,
                 generated_by: str, scope_key: str, revision: int, supersedes: Optional[str],
                 inputs: list[EvidenceInput], accounts: dict[str, BuyerAccount], labeled_outcomes: int
                 ) -> MarketReport:
    features = build_features(inputs, request, as_of)
    obs = {i.obs.evidence_id: i.obs for i in inputs}
    serviceable = set(request.serviceable_categories)

    def total(kind: str) -> Decimal:
        return money(sum((f.fx.amount_base for f in features if f.counts_as == kind and f.fx), ZERO))

    verified = [f for f in features if f.counts_as == "VERIFIED"]
    if any(f.synthetic or not f.attested or f.spend_type != "VERIFIED_BUDGET" for f in verified):
        raise InvariantViolation("synthetic, unattested or non-verified spend reached verified_real_sam")
    by_cat: dict[str, Decimal] = {}
    for f in verified:
        by_cat[f.service_category] = money(by_cat.get(f.service_category, ZERO) + f.fx.amount_base)
    excluded_spend = money(sum((f.fx.amount_base for f in features if f.counts_as == "EXCLUDED" and f.fx
                                and f.fx.amount_base > 0), ZERO))
    sam = SamSummary(base_currency=request.base_currency, verified_real_sam=total("VERIFIED"),
                     indicative_spend=total("INDICATIVE"), hypothetical_addressable_spend=total("HYPOTHETICAL"),
                     excluded_spend=excluded_spend, verified_by_category=dict(sorted(by_cat.items())),
                     verified_evidence_ids=sorted(f.evidence_id for f in verified),
                     indicative_evidence_ids=sorted(f.evidence_id for f in features if f.counts_as == "INDICATIVE"),
                     hypothetical_evidence_ids=sorted(f.evidence_id for f in features
                                                      if f.counts_as == "HYPOTHETICAL"),
                     synthetic_evidence_count=sum(1 for f in features if f.synthetic))
    if all(f.synthetic for f in features) and sam.verified_real_sam != 0:
        raise InvariantViolation("an all-synthetic evidence set produced non-zero verified_real_sam")

    by_acc: dict[str, list[EvidenceFeature]] = {}
    for f in features:
        if f.account_id:
            by_acc.setdefault(f.account_id, []).append(f)
    wanted = set(request.account_ids) if request.account_ids else None
    opps = []
    for acc_id, feats in by_acc.items():
        if wanted is not None and acc_id not in wanted:
            continue
        acc = accounts.get(acc_id)
        if acc is None:
            continue
        o = score_account(acc, feats, obs, serviceable, as_of)
        if o is not None:
            opps.append(o)
    opps.sort(key=lambda o: (-o.heuristic_priority_score, -o.verified_spend_base, o.account_id))
    opps = opps[:request.top_n]

    flags = [RiskFlag(code=c, account_id=o.account_id, detail=f"{o.legal_name}: {c.replace('_', ' ').lower()}")
             for o in opps for c in o.risk_flags]
    for f in features:
        if f.fx and f.fx.fx_stale:
            flags.append(RiskFlag(code="FX_STALE", evidence_id=f.evidence_id,
                                  detail=f"FX rate dated {f.fx.fx_as_of} is older than {FX_STALE_DAYS} days"))
        for code in ("MALFORMED_AMOUNT", "IMPLAUSIBLE_AMOUNT", "DUPLICATE_SPEND", "STALE_EVIDENCE",
                     "SUPERSEDED_CORRIGENDUM", "RIGHTS_REVOKED", "UNSUPPORTED_CLAIM"):
            if code in f.exclusions:
                flags.append(RiskFlag(code=code, evidence_id=f.evidence_id, account_id=f.account_id,
                                      detail=next(d for d in f.exclusion_detail if d.startswith(code))))
    pending = sorted(f.evidence_id for f in features if f.spend_type == "VERIFIED_BUDGET" and not f.attested
                     and not f.synthetic and not set(f.exclusions) & HARD_EXCLUSIONS)
    missing = sorted({m for o in opps for m in o.missing_information})
    citations = [Citation(evidence_id=f.evidence_id, evidence_revision=f.evidence_revision, source_url=f.source_url,
                          source_connector=f.source_connector, observation_type=f.observation_type,
                          published_at=obs[f.evidence_id].published_at, n0_artifact=f.n0_artifact)
                 for f in sorted(features, key=lambda f: f.evidence_id)]
    readiness = MlReadiness(labeled_outcomes=labeled_outcomes, required=ML_REQUIRED_LABELED_OUTCOMES,
                            ready=labeled_outcomes >= ML_REQUIRED_LABELED_OUTCOMES,
                            note="a win-probability model is deferred until enough real labeled won/lost "
                                 "outcomes exist; scores stay heuristic")
    report = MarketReport(
        report_id=report_id, tenant_id=tenant_id, scope_name=request.scope_name, scope_key=scope_key,
        revision=revision, supersedes_report_id=supersedes, as_of=as_of, generated_at=generated_at,
        generated_by=generated_by, engine_version=ENGINE_VERSION, base_currency=request.base_currency,
        fx_table=request.fx_rates, serviceable_categories=sorted(serviceable), sam=sam, opportunities=opps,
        exclusions=[Exclusion(evidence_id=f.evidence_id, codes=f.exclusions, detail=f.exclusion_detail)
                    for f in features if f.exclusions],
        risk_flags=flags, pending_attestations=pending, missing_information=missing, citations=citations,
        evidence_features=sorted(features, key=lambda f: f.evidence_id), ml_readiness=readiness,
        disclaimers=[SAM_DISCLAIMER, SCORE_DISCLAIMER,
                     "PASS at N0 and inclusion here are not engineering qualification or a commitment to bid."])
    return seal(report)


def report_digest(report: MarketReport) -> str:
    body = report.model_dump(mode="json")
    body.pop("report_sha256", None)
    return sha256_hex(canonical_json(body))


def seal(report: MarketReport) -> MarketReport:
    return report.model_copy(update={"report_sha256": report_digest(report)})


def scope_key_for(tenant_id: str, request: ReportRequest) -> str:
    key = {"tenant": tenant_id, "scope": request.scope_name, "categories": sorted(request.serviceable_categories),
           "base": request.base_currency, "accounts": sorted(request.account_ids or [])}
    return "SCP-" + hashlib.sha256(canonical_json(key)).hexdigest()[:16]


def render_markdown(report: MarketReport) -> str:
    cite = {c.evidence_id: c for c in report.citations}

    def ref(eid: str) -> str:
        c = cite.get(eid)
        if c and c.source_url and c.source_url.startswith("http"):
            return f"[{eid}]({c.source_url})"
        return f"`{eid}`" + (f" ({c.n0_artifact})" if c else "")

    s = report.sam
    lines = [f"# Market report {report.report_id} (rev {report.revision})", "",
             f"Scope **{report.scope_name}** · as of {report.as_of.date()} · base currency {s.base_currency} · "
             f"engine `{report.engine_version}` · sha256 `{(report.report_sha256 or '')[:16]}…`", "",
             "## Serviceable addressable market", "",
             "| Bucket | Amount | Evidence |", "|---|---:|---|",
             f"| **Verified real SAM** | {s.verified_real_sam:,} | {', '.join(ref(e) for e in s.verified_evidence_ids) or '—'} |",
             f"| Indicative (unverified or unattested) | {s.indicative_spend:,} | "
             f"{', '.join(ref(e) for e in s.indicative_evidence_ids) or '—'} |",
             f"| Hypothetical (synthetic / TAM proxy) | {s.hypothetical_addressable_spend:,} | "
             f"{', '.join(ref(e) for e in s.hypothetical_evidence_ids) or '—'} |",
             f"| Excluded by guardrails | {s.excluded_spend:,} | see exclusions |", "",
             f"> {s.disclaimer}", "", "## Buyer shortlist", "",
             "| # | Buyer | Heuristic priority (0–100) | Verified | Flags | Evidence |", "|---|---|---:|---:|---|---|"]
    for i, o in enumerate(report.opportunities, 1):
        lines.append(f"| {i} | {o.legal_name} | {o.heuristic_priority_score} | {o.verified_spend_base:,} | "
                     f"{', '.join(o.risk_flags) or '—'} | {', '.join(ref(e) for e in o.evidence_ids)} |")
    lines += ["", f"> {SCORE_DISCLAIMER}", ""]
    if report.pending_attestations:
        lines += ["## Pending evidence attestations", ""] + [f"- {ref(e)}" for e in report.pending_attestations] + [""]
    if report.missing_information:
        lines += ["## Missing information", ""] + [f"- {m}" for m in report.missing_information] + [""]
    if report.exclusions:
        lines += ["## Exclusions (N1.4 guardrails)", ""]
        lines += [f"- {ref(x.evidence_id)}: {'; '.join(x.detail)}" for x in report.exclusions] + [""]
    lines += ["## Citations", ""]
    lines += [f"- {ref(c.evidence_id)} — {c.observation_type} via {c.source_connector}, published "
              f"{c.published_at.date()}, admitted by N0 as `{c.n0_artifact}`" for c in report.citations]
    return "\n".join(lines) + "\n"

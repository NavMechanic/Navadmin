"""N6 Commercial — bid portal for single-node, bundle and full-chain bids (gate P: "End-to-end bid with no auto-award").

There is deliberately no code path that awards a bid automatically: `award` requires an
explicit confirmation from a founder/programme manager who did not submit the bid.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..core import audit
from ..core.errors import Conflict, Forbidden, NotFound, ValidationFailed
from ..core.security import Principal
from ..db import utcnow
from ..models import Bid

SCOPES = {"single_node", "bundle", "full_chain"}
NODES = {"N1", "N2", "N3", "N4", "N5", "N6", "N7", "N8"}
AWARD_ROLES = ("founder", "programme_manager")


def submit_bid(db: Session, principal: Principal, data: dict) -> Bid:
    scope, nodes = data.get("scope"), sorted(set(data.get("nodes", [])))
    if scope not in SCOPES:
        raise ValidationFailed(f"scope must be one of {sorted(SCOPES)}")
    if not nodes or not set(nodes) <= NODES:
        raise ValidationFailed(f"nodes must be a non-empty subset of {sorted(NODES)}")
    if scope == "single_node" and len(nodes) != 1:
        raise ValidationFailed("single_node bids cover exactly one node")
    if scope == "full_chain" and set(nodes) != NODES:
        raise ValidationFailed("full_chain bids cover N1-N8")
    try:
        amount = Decimal(str(data["amount"])).quantize(Decimal("0.01"))
    except (KeyError, InvalidOperation):
        raise ValidationFailed("amount must be a decimal number")
    if amount <= 0:
        raise ValidationFailed("amount must be positive")
    bid = Bid(tenant_id=principal.tenant_id, rfq_ref=data["rfq_ref"], bidder=data["bidder"], scope=scope, nodes=nodes,
              amount=str(amount), currency=data.get("currency", "USD"), lead_time_days=int(data["lead_time_days"]),
              submitted_by=principal.username)
    db.add(bid)
    db.flush()
    audit.record(db, principal.tenant_id, principal.username, "bid.submitted", "bid", bid.id,
                 {"rfq_ref": bid.rfq_ref, "scope": scope, "amount": bid.amount})
    return bid


def get_bid(db: Session, tenant_id: str, bid_id: int) -> Bid:
    bid = db.get(Bid, bid_id)
    if bid is None or bid.tenant_id != tenant_id:
        raise NotFound(f"bid {bid_id} not found")
    return bid


def _transition(db, principal, bid_id, allowed_from, to, action):
    bid = get_bid(db, principal.tenant_id, bid_id)
    if bid.status not in allowed_from:
        raise Conflict(f"bid {bid_id} is {bid.status}; cannot {action}")
    bid.status, bid.decided_by, bid.decided_at = to, principal.username, utcnow()
    audit.record(db, principal.tenant_id, principal.username, f"bid.{action}", "bid", bid.id)
    return bid


def shortlist(db: Session, principal: Principal, bid_id: int) -> Bid:
    return _transition(db, principal, bid_id, {"submitted"}, "shortlisted", "shortlist")


def reject(db: Session, principal: Principal, bid_id: int) -> Bid:
    return _transition(db, principal, bid_id, {"submitted", "shortlisted"}, "rejected", "reject")


def award(db: Session, principal: Principal, bid_id: int, confirm: bool) -> Bid:
    if confirm is not True:
        raise ValidationFailed("awarding requires explicit confirm=true; bids are never auto-awarded")
    if not principal.has_any(*AWARD_ROLES):
        raise Forbidden("awarding requires founder or programme_manager")
    bid = get_bid(db, principal.tenant_id, bid_id)
    if bid.submitted_by == principal.username:
        raise Forbidden("the bid's submitter cannot award it")
    already = db.scalars(select(Bid).where(Bid.tenant_id == principal.tenant_id, Bid.rfq_ref == bid.rfq_ref,
                                           Bid.status == "awarded")).first()
    if already:
        raise Conflict(f"RFQ {bid.rfq_ref} already awarded to bid {already.id}")
    return _transition(db, principal, bid_id, {"shortlisted"}, "awarded", "award")


def bid_view(bid: Bid) -> dict:
    return {"id": bid.id, "rfq_ref": bid.rfq_ref, "bidder": bid.bidder, "scope": bid.scope, "nodes": bid.nodes,
            "amount": bid.amount, "currency": bid.currency, "lead_time_days": bid.lead_time_days, "status": bid.status,
            "submitted_by": bid.submitted_by, "decided_by": bid.decided_by}

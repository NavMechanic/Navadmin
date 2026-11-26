"""ATHENA N5 — Procurement, Supplier Intelligence & BOM Optimization Engine.

N5 ingests N3-approved engineering BOMs (exact MPNs, quantities, design revision), qualifies
manufacturers, distributors and EMS / ODM partners, validates live and signed quotations against
deterministic guardrails, ranks the *eligible* offers by calculated extended material cost and
emits N0-checked draft handoffs to N6 (commercial) and N7 (finance).
"""
from ..models import EXCLUDED_COST_CATEGORIES, INDICATIVE_MATERIAL_LABEL

N5_VERSION = "1.0.0"

APPROVAL_SCOPE = (
    "N5 approval means the supplier selection and the indicative material quote were checked against the "
    "approved N3 BOM revision, current supplier qualifications, quotation validity and the deterministic "
    "guardrails. It is a draft input for N6 bidding and N7 costing: not a purchase order, a supplier contract, "
    "a complete product cost or an engineering approval of any alternate part.")

__all__ = ["APPROVAL_SCOPE", "EXCLUDED_COST_CATEGORIES", "INDICATIVE_MATERIAL_LABEL", "N5_VERSION"]

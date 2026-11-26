# ATHENA N5 — AI and Training Strategy

**N5 runs no model today.** Every eligibility decision, quantity, price-break choice, cost, ranking
and approval is computed by the deterministic engine (`app/n5/engine.py`) and recomputed by N0 at
release. No LLM does any arithmetic. Any future model may assist a person; it never decides, prices,
substitutes or approves.

## 1. What is never used for training

- **Distributor API payloads** from DigiKey, Mouser and Nexar, and anything derived from them.
  - Their terms prohibit bulk download, aggregation and database building, and N5 does not train on
    them.
  - The records carry `training_allowed: false`. The N0 sources for these connectors must not
    permit `MODEL_TRAINING`.
- **Supplier quotations and EMS commercial terms**, unless the supplier's agreement grants that use
  and the N0 source explicitly permits `MODEL_TRAINING`.
- **Personal data** in supplier contacts. N5 records hold company data only, and N0's PII screening
  applies to everything submitted.

## 2. Candidate assistive models (all gated, none implemented)

| Candidate | Purpose | Allowed influence | Gate before use |
|---|---|---|---|
| Quote-document extractor (SLM, behind the N0 extraction boundary) | read supplier PDF quotations into `athena-quote/1` drafts | produces a **draft** that a procurement analyst confirms field by field; N5's structural checks and guardrails still apply | rights-cleared, labelled first-party quotations; field-level precision ≥ 0.99 on a frozen hold-out; N0 extraction-boundary review |
| Lead-time / supply-risk estimator | warn about parts likely to go long-lead or end-of-life | may add an INFO risk flag to N6; never changes eligibility, ranking or the target | the tenant's own PO / receipt history (first-party); calibration on hold-out quarters; documented features with no distributor payloads |
| ECO equivalence assistant | summarise parametric differences between an approved and a proposed alternate | drafts the `parametric_comparison` text of an ECO proposal from datasheets the tenant is licensed to use | human engineer review; the ECO still goes to N3, and N5 never substitutes |
| Supplier-document classifier | sort uploaded channel proofs, certificates and allocation letters | suggests a role for a file; the supplier-quality reviewer decides | labelled first-party documents; precision / recall per class on a frozen hold-out |

## 3. Data governance for any future training set

1. **Rights.** Every training record traces to an N0 source that permits `MODEL_TRAINING`, recorded
   in its lineage.
2. **Ground truth.** Labels come from human decisions:
   - accepted / rejected quotes, analyst-confirmed extractions, qualified suppliers;
   - never another model's output and never an unreviewed record.
3. **Separation.** Train, calibration and test sets are split by supplier and time, with the test
   set frozen.
4. **No leakage of the decision rule.** The guardrails stay code. Models never learn to approve or
   reject offers.
5. **Monitoring.** Track override rates and drift per tenant, and suspend a model automatically when
   its overrides exceed threshold.

## 4. LLM use that is explicitly out of bounds

- calculating any quantity, price, FX conversion or total;
- choosing or ranking a supplier, or waiving a guardrail;
- approving a supplier, a report or a handoff;
- substituting a part or editing a BOM.

# ATHENA N4 — Training, Model Gating and Vendor Qualification Plan

N4 is **deterministic-first**. Every PASS / REWORK / BLOCK decision, every verdict and every
release is computed by `app/n4/engine.py` and re-checked by N0. Models assist people. They never
decide, rate or approve.

## 1. Model status

| Capability | Status | What would unlock the next stage |
|---|---|---|
| Deterministic policy engine | **ACTIVE** | — |
| LLM + RAG suggestions (test-case drafts, PFMEA failure-mode drafts, RCA hypotheses) | **OPT_IN**, off by default (`N4_LLM_PROVIDER=anthropic`) | Already available. Each tenant enables it explicitly. |
| SLM extractor (lab-report / datasheet field extraction) | **PLANNED** | A licensed, labelled extraction set. Field-level precision ≥ 0.99 on held-out lab reports. N0 extraction-boundary review. |
| Vision models for release decisions (YOLOv8 AOI) | **DEFERRED_GATED** (triage only today) | See §3 |
| Time-series ML (burn-in drift, ATE trend anomalies) | **DEFERRED_GATED** | See §3 |
| Reliability models (Weibull / Bayesian life) | **DEFERRED_GATED**. Only research-only MTBF illustrations exist. | Field-return data with genealogy from N8, statistician review, a stated physics-of-failure basis. |

## 2. LLM + RAG guardrails (implemented in `app/n4/ai.py`)

**Corpus.** Documents come only from sources that are APPROVED and permit `LLM_EXTRACTION`:

- SOPs, standards excerpts the tenant is licensed for, and verified CAPA records;
- a `VERIFIED_CAPA` document needs a CAPA verified effective.

Text passes presidio redaction (or the N0 fallback redactor) before storage.

**Retrieval.** Lexical BM25 over chunked text (`rag_top_k`, default 6). Nothing leaves the tenant except
the prompt to the configured provider.

**Output contract.** Output is constrained JSON (`SUGGESTION_SCHEMA`). Every suggestion must cite a
retrieved chunk with a **verbatim quote**, and unsupported suggestions are dropped (`NO_GROUNDING`).

**Forbidden content.** Any S/O/D rating, RPN, AP or "approve the release" wording is stripped from the
output. The suggestion record says so.

**Adoption.** Suggestions are stored as `N4Suggestion` with status `DRAFT`. A human adopts one only by
submitting their own PFMEA entry or test case with `adopted_from_suggestion`, and the human remains
`rated_by`.

**Model.** The model id is configured (`N4_LLM_MODEL`). Tests use a fake provider. Before enabling the
LLM in production, run the suggestion evaluation set (backlog N4-B07) and record the result in the
tenant's approval.

## 3. Gates for deferred models

A deferred model may move from *triage* to *decision support* only when **all** of these hold, and
N0's dataset policy gate approves the training set:

1. **Licensed training data.** Images and logs come from sources whose rights permit model training.
   The N0 source record carries that permitted use.
2. **Ground truth from people.** Labels are double-labelled by independent human adjudicators
   (`/v1/n4/aoi/training-labels` exports only `training_eligible` labels). Model outputs, unreviewed
   calls and importer self-labels are never used.
3. **Independent hold-out.** The calibration and test sets are separate from training, cover every
   PCB revision and line, and are frozen. Report per-class recall / precision with confidence
   intervals. Recall on safety-relevant defect classes (bridges, missing insulation) must meet the
   quality lead's target with a 95 % lower bound.
4. **Drift monitoring.** N8 monitors are in place for the model's input distribution and for the
   human-override rate.

Even then, a model output can at most **raise** a finding (for example, flag a unit). It never clears a
human-required step and never changes the deterministic decision.

## 4. AOI labelling workflow

1. The AOI station exports `athena-aoi/1` with images and raw detections, including
   low-confidence ones.
2. N4 triages them (flag ≥ 0.85, ambiguous ≥ 0.40).
3. A `quality_reviewer`, who is not the importer, adjudicates each detection and the unit.
4. A second, independent reviewer adjudicates a sample, plus every disagreement. Labels where both
   agree become `training_eligible`.
5. A periodic export feeds the gated training pipeline outside N4. Retrained weights come back only as
   a new `model.version` + `weights_sha256` in the inference records, so N4 never runs training.

## 5. Vendor qualification

| Vendor type | Onboarding in N4 | Evidence they must deliver |
|---|---|---|
| **Accredited test laboratory** | A `quality_lead` records the lab (`POST /v1/n4/labs`): accreditation body + number, accredited scopes (standards / clauses) and validity. The evidence source must be APPROVED for `VERIFICATION`. | `athena-lab-report/1` manifest + signed PDF. It names the exact PCB revision and the certificate number. Per-result U, k, instrument and calibration due date. |
| **Calibration provider** | Calibration records (`POST /v1/n4/calibrations`) with the certificate PDF, the accreditation reference, and calibrated-at / due dates. They can be revoked. | Certificate per instrument serial, plus the uncertainty statement. |
| **ATE integrator** | The instruments are registered with serial + firmware. SOPs are registered and approved by a `quality_reviewer`, and the author never self-approves. | `athena-scpi-log/1` with raw IDN, query, response and UTC timestamps, plus raw waveform / CSV attachments with SHA-256. |
| **AOI vendor** | The AOI station is registered as an instrument (kind `AOI_STATION`). The model name, version and weights hash are declared. | `athena-aoi/1` with every image and detection, including low-confidence detections. No pre-filtered "pass" outputs. |

**Ongoing controls:**

- Accreditation validity is checked on the report issue date, and calibration validity on the capture
  date.
- An expired or revoked record blocks new evidence immediately. It never silently changes a release
  already made.
- A vendor whose submission is hard-rejected stays blocking for those tests until a corrected,
  accepted submission arrives.

## 6. People: qualification of approvers

- A `quality_lead` grants qualifications (`POST /v1/n4/qualifications`): principal, methods
  (SIM / BENCH_ATE / ACCREDITED_LAB / INSPECTION), valid-until and basis (competence record). They
  cannot grant one to themselves.
- A reviewer must hold a current qualification for **every** method used in the plan
  (`N4_REQUIRE_QUALIFICATION=true`).
- Training expectations for test authorities: GUM / ILAC-G8 decision rules and guard bands, the
  product standard (for example IEC 62368-1), PFMEA facilitation, and the N4 evidence-hash review
  procedure.

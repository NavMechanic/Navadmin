# Navadmin

## ATHENA Data Quality Gateway

Nothing reaches Qdrant, the Digital Twin or a cross-node handoff unless the deterministic DQ
gateway (`rag/dq_gateway/`) admits it. That covers vendor artifacts, AI/LLM outputs, CAD/FEA
exports, supplier quotes, financial models and line telemetry. See
[docs/DQ_GATEWAY.md](docs/DQ_GATEWAY.md) for the pipeline, the DQ01–DQ27 rules, the API
(A01–A21) and the CLI workflow.

```bash
pip install -r requirements.txt
python -m pytest tests/                       # T01–T12 + integration tests
streamlit run dashboard/app.py                # "DQ Control Tower" tab
ATHENA_DQ_TOKENS='{"<token>": {"tenant_id": "demo", "actor_id": "demo.engineer"}}' ATHENA_DQ_DEMO=1 \
  uvicorn api.gateway_router:create_app_from_env --factory
```

## ATHENA N0 — Data Intelligence, Quality & Governance Gateway

`athena_n0_starter/` is a standalone FastAPI service. It gates every package before N1–N8 see it,
using six stages (N0.1 intake → N0.6 release), eight `*.v1` JSON Schema 2020-12 contracts and
durable, append-only storage. LLM extraction runs behind a rights-gated, schema-constrained
boundary. See [athena_n0_starter/README.md](athena_n0_starter/README.md).

```bash
cd athena_n0_starter && pip install -r requirements.txt && python -m pytest -q
```

## ATHENA N1 — Market & Opportunity Intelligence Engine

`athena_n1_integrated/` mounts N1 on the N0 gateway. N0-approved market and buyer evidence becomes
evidence-backed SAM estimates, heuristic buyer shortlists, risk flags and non-executing,
cost-ranked actions. Synthetic data never counts as verified SAM, and every N2/N6/N7 handoff is
re-checked and released through N0. See [athena_n1_integrated/README.md](athena_n1_integrated/README.md).

```bash
cd athena_n1_integrated && pip install -r requirements.txt && python -m pytest -q
python examples/run_local_demo.py && streamlit run dashboard/n1_dashboard.py
```

## ATHENA data acquisition pipeline

`data_pipeline/` fetches, verifies and formats the 75 N0–N8 sources (SRC-001..SRC-075). It uses
isolated `data_lake/` partitions, immutable provenance manifests, and 12 hard promotion controls. See
[docs/DATA_PIPELINE.md](docs/DATA_PIPELINE.md). The source catalog is a reconstruction pending steward
verification.

```bash
python -m data_pipeline.cli --nodes N0..N8 --priority P0 --mode all --action plan
python -m pytest tests/test_dq_adversarial.py      # DQ-001..DQ-054
```

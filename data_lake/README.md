# data_lake/ — isolated acquisition store (not committed)

Populated by `python -m data_pipeline.cli`. Contents are git-ignored; only this file is tracked.

| Partition | Holds | May reach model training? |
|---|---|---|
| `train_candidates/<node>/` | TRAIN-CANDIDATE sources | Only via `--action export` after a steward-ledger `APPROVED` decision |
| `rag_reference/<node>/` | RAG/REFERENCE and LIVE-LOOKUP (`live_cache/`) | Never — runtime grounding only |
| `eval_benchmarks/<node>/` | EVAL/BENCHMARK | Never — offline evaluation only |
| `eval_noncommercial/<node>/` | Non-commercial / unasserted licenses (MVTec AD, AD 2, …) | Never |
| `private_ground_truth/<node>/` | 15 `internal://athena/*` vaults + synthetic fixtures | Never via this pipeline |

`_staging/`, `_governance/` (training-rights ledger), `_reports/` and `_exports/` are pipeline-owned.
This tree is separate from the production vector store (`rag/vectorstore/`); the pipeline refuses to
run with a lake root inside it. See `docs/DATA_PIPELINE.md`.

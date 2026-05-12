# Navadmin — Aviator RAG System

A Retrieval-Augmented Generation (RAG) pipeline for NPI engineering on the **Aviator (SM8845)** project. It indexes DOI/SOP PDFs into a FAISS vector store and exposes two capabilities:

1. **RCA / Control Plan queries** — ask engineering questions against the document corpus.
2. **Parametric shift detection** — compare production test logs against a golden sample and flag outliers.

## Installation

```bash
pip install -r requirements.txt
```

## Quick Start

```python
from rag_system import AviatorRAGSystem

# Bring your own LLM (example uses langchain-anthropic)
from langchain_anthropic import ChatAnthropic
llm = ChatAnthropic(model="claude-sonnet-4-6")

system = AviatorRAGSystem(
    pdf_docs=["Aviator_DOI_V3.pdf", "Aviator_SOP_Mainboard.pdf"],
    golden_sample_csv="Aviator_Testing_Golden.csv",
    llm=llm,
)

# --- RCA query ---
answer = system.query_engine(
    "Mic Sealing failure at Step 16 — what fixture and pressure spec does the DOI require?"
)
print(answer)

# --- Parametric shift detection ---
drift_report = system.check_parametric_shift("Production_Logs_Shift_B.csv")
print(drift_report)
```

## Input File Formats

### Golden sample CSV (`Aviator_Testing_Golden.csv`)
| TESTCODE | TESTVAL |
|----------|---------|
| MIC_SEAL | 200     |
| ...      | ...     |

### Production log CSV
Must contain the same `TESTCODE` and `TESTVAL` columns.

## Persisting the Vector Index

```python
system.save_index("faiss_index/")   # save after first build
system.load_index("faiss_index/")   # skip PDF re-processing on restart
```

## Drift Threshold

Process drift is flagged when a test value deviates more than **5 %** from the golden sample.
Change `DRIFT_THRESHOLD_PERCENT` in `rag_system.py` to adjust.

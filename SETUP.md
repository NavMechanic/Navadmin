# Navadmin RAG Pipeline — Setup Guide

## System Requirements

- **Python**: 3.9+ (tested on 3.11)
- **OS**: Linux, macOS, Windows (WSL2)
- **Disk**: 2+ GB (for vector index and PDFs)
- **RAM**: 4+ GB (8 GB recommended for batch simulations)

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/NavMechanic/Navadmin.git
cd Navadmin
git checkout main
```

### 2. Install Python dependencies

```bash
pip install -r requirements.txt
```

**Installed packages** (16 core dependencies):
- `anthropic>=0.25.0` — Claude API for text generation
- `sentence-transformers>=2.7.0` — all-MiniLM-L6-v2 (384-dim embeddings)
- `qdrant-client>=1.9.0` — Vector database with HNSW indexing
- `pypdf>=4.0.0` — PDF page extraction
- `python-docx>=1.1.0` — Word document parsing
- `rank-bm25>=0.2.2` — BM25 keyword retrieval
- `numpy>=1.26.0` — Numerical computing
- `tiktoken>=0.7.0` — Token counting for Claude API
- `google-api-python-client>=2.100.0` — Google Drive API
- `google-auth-httplib2>=0.2.0`, `google-auth-oauthlib>=1.1.0` — Google auth
- `streamlit>=1.35.0` — Interactive dashboard
- `plotly>=5.22.0` — Data visualization
- `paho-mqtt>=2.0.0` — MQTT broker for PLC streaming (optional)

### 3. Verify installation

```bash
python -c "
from rag.pipeline import RAGPipeline
from rag.digital_twin.twin import DigitalTwin
from rag.ingestion.pdf_pipeline import PDFPipeline
print('✓ All core modules imported successfully')
"
```

---

## VS Code Setup

### Recommended Extensions

Install these extensions from the VS Code Marketplace:

| Extension | Publisher | Purpose |
|-----------|-----------|---------|
| **Python** | Microsoft | Linting, debugging, intellisense |
| **Pylance** | Microsoft | Type checking and smart completions |
| **Prettier** | Prettier | Code formatting |
| **Thunder Client** / **REST Client** | Rangav / Huachao Mao | Test API endpoints |
| **Mermaid Markdown Syntax Highlighting** | Alolant | Preview fishbone diagrams |
| **YAML** | Red Hat | Config file syntax highlighting |
| **Git Graph** | mhutchie | Visual git history |

**Install via command line:**
```bash
code --install-extension ms-python.python
code --install-extension ms-python.vscode-pylance
code --install-extension esbenp.prettier-vscode
code --install-extension rangav.vscode-thunder-client
code --install-extension alolant.mermaid-markdown-syntax-highlighting
code --install-extension redhat.vscode-yaml
code --install-extension mhutchie.git-graph
```

### VS Code settings.json

Add to `.vscode/settings.json`:

```json
{
  "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
  "python.linting.enabled": true,
  "python.linting.pylintEnabled": true,
  "python.linting.pylintArgs": ["--max-line-length=100"],
  "python.formatting.provider": "black",
  "editor.formatOnSave": true,
  "editor.defaultFormatter": "ms-python.python",
  "[python]": {
    "editor.formatOnSave": true,
    "editor.defaultFormatter": "ms-python.python"
  },
  "files.exclude": {
    "**/__pycache__": true,
    "**/.pytest_cache": true,
    "**/*.pyc": true,
    ".venv": true
  },
  "python.testing.pytestEnabled": true,
  "python.testing.pytestArgs": ["tests"],
  "mermaid.useMaxWidth": false
}
```

### VS Code launch.json

Create `.vscode/launch.json` for debugging:

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Python: CLI",
      "type": "python",
      "request": "launch",
      "module": "main",
      "args": ["--help"],
      "console": "integratedTerminal",
      "justMyCode": true
    },
    {
      "name": "Python: Streamlit Dashboard",
      "type": "python",
      "request": "launch",
      "module": "streamlit",
      "args": ["run", "dashboard/app.py"],
      "console": "integratedTerminal",
      "justMyCode": true
    },
    {
      "name": "Python: Current File",
      "type": "python",
      "request": "launch",
      "program": "${file}",
      "console": "integratedTerminal",
      "justMyCode": true
    }
  ]
}
```

---

## Quick Start

### 1. Run a batch simulation

```bash
python main.py twin-simulate -n 50
```

Expected output:
```
Running batch simulation (50 cycles)…
Predicted yield: 94.0%

Predicted failure mode Pareto:
  12x  IP68_VACUUM_FAIL
   8x  FINGERPRINT_TEST_FAIL
   5x  THERMAL_RUNAWAY
```

### 2. Launch the dashboard

```bash
# Simulation mode (no RAG)
streamlit run dashboard/app.py

# With RAG pipeline
streamlit run dashboard/app.py -- --with-rag
```

Dashboard opens at `http://localhost:8501`

### 3. Ingest a PDF

```bash
# Single file
python main.py ingest-pdf data/aviator/root/battery_spec.pdf

# Entire directory
python main.py ingest-pdf data/aviator/root/
```

### 4. Sync from Google Drive (optional)

Requires `service_account.json` with Google Drive API credentials:

```bash
# Incremental sync (skips already-indexed files)
python main.py ingest-pdf-drive --credentials service_account.json

# Local mirror (offline)
python main.py ingest-pdf-drive --local-mirror data/aviator/
```

### 5. Run FMEA assessment

```bash
python main.py fmea-check-twin -n 50
```

### 6. Generate fishbone diagrams

```bash
# All high-RPN items
python main.py fishbone-diagram

# Specific item
python main.py fishbone-diagram --item-id PFMEA-S22-001

# Custom threshold
python main.py fishbone-diagram --rpn-threshold 150
```

---

## Project Structure

```
Navadmin/
├── rag/                          # Core RAG pipeline
│   ├── pipeline.py               # Main orchestrator
│   ├── chunking/                 # Text chunking strategies
│   ├── embeddings/               # Vector embedding (SentenceTransformer)
│   ├── retrieval/                # Hybrid BM25 + dense search
│   ├── reranking/                # Cross-encoder reranking
│   ├── generation/               # Claude API text generation
│   ├── evaluation/               # RAG evaluation metrics
│   ├── vectorstore/              # Qdrant integration
│   ├── ingestion/                # Document loaders
│   │   ├── loaders.py            # PDF, DOCX, TXT, Markdown
│   │   ├── pdf_pipeline.py       # Section detection, table detection
│   │   ├── drive_loader.py       # Google Drive connector
│   │   ├── drive_sync.py         # Incremental Drive sync manifest
│   │   ├── telemetry_parser.py   # NexTest log parser
│   │   ├── physics_parser.py     # FEA mesh ingestion
│   │   └── cad_parser.py         # STEP/CAD manifest parser
│   ├── digital_twin/             # Process simulation
│   │   ├── twin.py               # Twin orchestrator (3 nodes)
│   │   ├── nodes/                # Assembly process nodes
│   │   │   ├── fastening_node.py # Node A: torque → RF RSSI
│   │   │   ├── dispensing_node.py # Node B: thermal stack PINN
│   │   │   └── press_node.py     # Node C: gasket compliance
│   │   ├── broker/               # MQTT PLC data broker
│   │   │   └── mqtt_client.py
│   │   └── physics/              # Numerical models
│   │       ├── pinn.py           # Thermal PINN (numpy)
│   │       └── stress_model.py   # Stress calculations
│   ├── fmea/                     # FMEA assessment
│   │   ├── fmea.py               # PFMEA/DFMEA classes, RPN calculation
│   │   └── fishbone.py           # Mermaid fishbone diagrams
│   └── ipc/                      # IPC-A-610G compliance
│       └── ipc_a610g.py          # Class 2 criteria mapping
├── dashboard/                    # Streamlit frontend
│   ├── app.py                    # Main dashboard
│   └── components/               # Reusable panels
│       ├── node_cards.py         # Node status visualizations
│       ├── prediction_charts.py  # Risk trends, histograms, Pareto
│       ├── rag_panel.py          # RAG RCA retrieval
│       ├── ipc_panel.py          # IPC criteria heatmap
│       └── fishbone_panel.py     # Fishbone diagram renderer
├── data/                         # Datasets & configs
│   ├── PFMEA_Aviator_Assembly.csv # Process FMEA checklist
│   ├── DFMEA_Aviator_Design.csv  # Design FMEA checklist
│   ├── IPC_A610G_Criteria.csv    # IPC thresholds & units
│   ├── aviator/                  # Sample data (synced from Drive)
│   └── .drive_manifest.json      # PDF sync tracking
├── main.py                       # CLI entry point
├── requirements.txt              # Python dependencies
└── SETUP.md                      # This file
```

---

## Environment Variables

Optional configuration:

```bash
# Google Drive API credentials (required for live Drive sync)
export GOOGLE_APPLICATION_CREDENTIALS="/path/to/service_account.json"

# Claude API key (defaults to ANTHROPIC_API_KEY)
export ANTHROPIC_API_KEY="sk-ant-..."

# MQTT broker (defaults to localhost:1883 in simulation mode)
export MQTT_BROKER_HOST="mqtt.example.com"
export MQTT_BROKER_PORT="1883"

# Vector database (defaults to :6333 local)
export QDRANT_URL="http://localhost:6333"
```

---

## Testing

Run the smoke test suite:

```bash
python rag/ingestion/pdf_pipeline.py
python rag/digital_twin/nodes/fastening_node.py
python rag/digital_twin/nodes/dispensing_node.py
python rag/digital_twin/nodes/press_node.py
```

---

## Troubleshooting

### Issue: "ModuleNotFoundError: No module named 'sentence_transformers'"

**Solution**: Reinstall dependencies
```bash
pip install --upgrade -r requirements.txt
```

### Issue: "google.auth.exceptions.DefaultCredentialsError" (Drive sync)

**Solution**: Set up Google credentials
```bash
export GOOGLE_APPLICATION_CREDENTIALS="path/to/service_account.json"
python main.py ingest-pdf-drive
```

### Issue: "Connection refused" (Qdrant)

**Solution**: Start Qdrant locally (Docker recommended)
```bash
docker run -p 6333:6333 qdrant/qdrant
```

### Issue: Streamlit port 8501 already in use

**Solution**: Specify a different port
```bash
streamlit run dashboard/app.py --server.port 8502
```

---

## Next Steps

1. **Ingest your first PDF**: `python main.py ingest-pdf data/aviator/root/`
2. **Run a simulation**: `python main.py twin-simulate -n 50`
3. **Launch the dashboard**: `streamlit run dashboard/app.py`
4. **Explore the API**: See `main.py` for all CLI commands

---

## Support

For questions, see the code docstrings or check the inline comments in:
- `rag/pipeline.py` — RAG pipeline architecture
- `rag/digital_twin/twin.py` — Digital twin orchestration
- `main.py` — CLI documentation


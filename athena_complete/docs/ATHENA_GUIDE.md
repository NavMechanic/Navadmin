# Project Athena — Complete Hardware Production Orchestration Loop

## Overview

Project Athena is an end-to-end hardware production orchestration system that automates the entire lifecycle from demand forecasting through quality monetization. It integrates five specialized layers, each responsible for a distinct phase of production, and feeds all results back into the RAG knowledge base for continuous learning and RCA (root cause analysis).

### The Five Layers

```
┌─────────────────────────────────────────────────────────────────────┐
│ [1] Trigger & Demand Forecasting         (Pecan AI)                 │
│     ↓ High-probability deal → Forecast + Build quantity             │
├─────────────────────────────────────────────────────────────────────┤
│ [2] Design & Sourcing Lock               (Duro PLM + Nexar)         │
│     ↓ Master BOM + Part availability + Alternates                   │
├─────────────────────────────────────────────────────────────────────┤
│ [3] Simulation & Optimization            (Lokad + Ansys Twin)       │
│     ↓ Safety stocks + Physical validation                            │
├─────────────────────────────────────────────────────────────────────┤
│ [4] Execution                            (MacroFab + Fictiv)        │
│     ↓ Simultaneous PCBA + Mechanical production                     │
├─────────────────────────────────────────────────────────────────────┤
│ [5] Quality & Revenue Monetization       (Instrumental + TwinMaker) │
│     ↓ Vision analytics → RaaS/YaSS revenue shares                   │
└─────────────────────────────────────────────────────────────────────┘
         ↓
   ┌─────────────────────────────────┐
   │ Athena results ingested into RAG│
   │ for future RCA and predictions  │
   └─────────────────────────────────┘
```

---

## Quick Start

### CLI (Fastest Way)

**Dry-run with mock data (no API keys needed):**
```bash
python main.py athena-run
```

**With specific deal:**
```bash
python main.py athena-run --deal-id DEAL-ABC123
```

**Export full run data as JSON:**
```bash
python main.py athena-run --json-out run.json
```

### Standalone Athena CLI

**Quick production run:**
```bash
python -m athena.run_athena run
```

**With RAG integration (if ANTHROPIC_API_KEY is set):**
```bash
python -m athena.run_athena run --with-rag
```

**Start the FastAPI REST server:**
```bash
python -m athena.run_athena serve --port 8080
```

---

## Layer-by-Layer Reference

### [1] Demand Forecasting (Pecan AI)

**Input:** Deal signal from Pecan AI (or mock)  
**Output:** `DemandForecast` with recommended build quantity

**What it does:**
- Fetches high-probability enterprise deal signals from Pecan AI
- Filters by minimum win probability threshold (default: 75%)
- Applies 10% safety uplift to forecast units
- Rounds up to nearest 10 for manufacturing convenience

**Mock behavior (ATHENA_DRY_RUN=true):**
```python
Probability: 88%
Estimated units: 220
Recommended build qty: 250  (220 × 0.88 × 1.10 → round up to 250)
Confidence interval: 187–264 units
```

**Env vars:**
```bash
PECAN_API_KEY=sk-pecan-...
PECAN_BASE_URL=https://api.pecan.ai/v1
PECAN_MIN_PROBABILITY=0.75
```

---

### [2] Sourcing Lock (Duro PLM + Nexar)

**Input:** Build quantity + BOM ID (or fetch latest)  
**Output:** `SourcingResult` with part availability and alternates

**What it does:**
- Pulls master BOM from Duro PLM (product specs, quantities, references)
- Queries Nexar supply-chain API for real-time stock levels
- Automatically maps alternates for any shortage
- Rates sourcing risk: `low` (>95% available) → `high` (<80% available)

**Mock BOM (Aviator PCBA Rev-C):**
```
GD25Q64CSIG       (64Mb SPI Flash)      1×  ✓ available
STM32F405RGT6     (ARM Cortex-M4)       1×  ⚠ alternate_mapped
LM5166DRVT        (DC/DC Buck)          2×  ✓ available
CGA4J2X7R1H104K   (100nF 0805 Cap)      48× ✓ available
CC0805KKX5R8BB    (10uF 0805 Cap)       12× ✓ available
```

**Env vars:**
```bash
DURO_API_KEY=key-duro-...
DURO_BASE_URL=https://app.duro.io/api/v1
DURO_BOM_REVISION=latest
NEXAR_CLIENT_ID=...
NEXAR_CLIENT_SECRET=...
NEXAR_BASE_URL=https://api.nexar.com/graphql
```

---

### [3] Simulation & Optimization (Lokad + Ansys Twin Builder)

**Input:** BOM + Build quantity  
**Output:** `SimulationResult` with safety stocks + physical validation

**Lokad Module:**
- Computes probabilistic safety stocks at 95% cycle service level
- Calculates reorder points (ROP) and demand standard deviation per part
- Uses historical demand patterns (mocked in dry-run)

**Ansys Twin Builder Module:**
- Simulates stress under factory conditions (typically ±2.5A load)
- Computes thermal delta relative to 25°C ambient
- Validates vibration response (MIL-STD-810H profile)
- Flags failure modes if stress >80 MPa or thermal delta >35°C

**Mock output:**
```
Max stress:     57.86 MPa  ✓ (< 80 MPa)
Thermal delta:  23.96°C    ✓ (< 35°C)
Vibration:      1.43 g RMS ✓ (< 3.0 g)
Physical OK:    YES
```

**Env vars:**
```bash
LOKAD_TOKEN=token-lokad-...
LOKAD_BASE_URL=https://api.lokad.com/rest/v1
LOKAD_ACCOUNT_ID=...
ANSYS_TWIN_URL=https://ansys.example.com/api
ANSYS_API_KEY=key-ansys-...
```

---

### [4] Execution (MacroFab + Fictiv)

**Input:** Build quantity + BOM ID  
**Output:** `ExecutionResult` with two simultaneous production orders

**What it does:**
- Submits PCBA manufacturing order to MacroFab
- Submits mechanical chassis order to Fictiv in parallel
- Tracks estimated ship dates and lead times
- Computes total cost and consolidated delivery window

**Mock output:**
```
PCBA Order:     MF-852886A0  ETA 2026-07-15
Mechanical:     FV-093CB8E3  ETA 2026-07-11
Total cost:     $15,312.50 (250 units × $61.25/unit)
Lead time:      18 days (max of both suppliers)
```

**Env vars:**
```bash
MACROFAB_API_KEY=key-macrofab-...
MACROFAB_BASE_URL=https://macrofab.com/api/v1
FICTIV_CLIENT_ID=...
FICTIV_CLIENT_SECRET=...
FICTIV_BASE_URL=https://api.fictiv.com/v2
```

---

### [5] Quality & Revenue Monetization (Instrumental + TwinMaker + RaaS/YaSS)

**Input:** Build quantity + serial prefix  
**Output:** `QualityRevenueResult` with inspections, TwinMaker pushes, revenue shares

**Three sub-steps:**

**5a. Vision Analytics (Instrumental.ai)**
- Fetches latest inspection results for each unit serial
- Classifies defect severity: `none` → `minor` → `major` → `critical`
- Captures defect types: `solder_bridge`, `cold_joint`, `missing_component`, etc.
- Records confidence score (0.0–1.0)

**5b. 3-D Digital Twin Sync (AWS IoT TwinMaker)**
- Pushes inspection results and metadata to TwinMaker workspace
- Updates asset properties: `defect_severity`, `defect_types`, `inspection_confidence`
- Enables real-time 3-D visualization and analytics

**5c. Revenue Tier & Monetization (RaaS/YaSS)**
Computes per-unit revenue based on quality score:

```
Quality Score = Inspection Confidence – Severity Penalty
  (NONE: 0%) → (MINOR: 5%) → (MAJOR: 20%) → (CRITICAL: 50%)

Tier Assignment:
  Enterprise  (≥90%): Multiplier = 1.5×
  Premium     (75–89%): Multiplier = 1.2×
  Standard    (<75%): Multiplier = 1.0×

Revenue Calculation:
  RaaS Monthly = Base ($150) × Quality Score × Tier Multiplier
  YaSS Annual  = Base ($1,200) × Quality Score × Tier Multiplier
```

**Mock output (250 units, 86.4% yield):**
```
Defect-free units:  216 (86.4%)
  Enterprise tier:  222 units  → $50,906/month RAG revenue
  Premium tier:     18 units
  Standard tier:    10 units
Total YaSS annual:  $407,249
```

**Env vars:**
```bash
INSTRUMENTAL_API_KEY=key-instrumental-...
INSTRUMENTAL_BASE_URL=https://api.instrumental.com/v1
INSTRUMENTAL_STREAM_URL=wss://stream.instrumental.com/vision
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=...
AWS_SECRET_ACCESS_KEY=...
TWINMAKER_WORKSPACE_ID=athena-workspace
TWINMAKER_ASSET_MODEL_ID=athena-device
RAAS_BASE_MONTHLY_USD=150.0
YASS_BASE_ANNUAL_USD=1200.0
REVENUE_QUALITY_MULTIPLIER=1.5
```

---

## RAG Integration

Project Athena automatically ingests run results back into the RAG knowledge base for three core purposes:

### 1. Root Cause Analysis (RCA) During Quality Inspection

When a defect is detected:
1. Quality layer queries RAG for historical context on the defect type
2. RAG retrieves prior failure analyses, process adjustments, supplier alerts
3. Inspection results are enriched with contextual RCA data

**Example:**
```
Defect detected: "solder_bridge"
  ↓
RAG query: "What are known causes and fixes for solder_bridge defects?"
  ↓
Retrieved sources:
  - [1] PFMEA-S22-001: Cold solder joint due to low reflow temp
  - [2] Process audit: Oven calibration drift detected May 2026
  - [3] Nexar alert: STM32F405RGT6 lead finish variation
```

### 2. FMEA Correlation

After production, Athena correlates observed defects with PFMEA/DFMEA items:
- Matches defect types against failure mode descriptions
- Identifies triggered high-RPN items
- Updates risk prioritization for future runs

### 3. Knowledge Capture for Future Predictions

Completed runs are converted to narrative documents and indexed in RAG:
```
# Project Athena Production Run ATH-593C63E1A2

## Demand Forecast (Pecan AI)
- Deal: DEAL-A3F5D9B7 — Acme Aerospace LLC
- Win probability: 88%
- Build quantity: 250 units

## Sourcing Lock
- BOM: BOM-8E34D0 rev C
- Shortages: 0 | Alternates mapped: 1
- Risk: LOW

[... all 5 layers ...]

## Quality & Revenue
- Yield rate: 86.0%
- Defects: {solder_bridge: 14, missing_component: 7, ...}
```

This document becomes searchable for:
- **Pre-run predictions**: "What yield rates should we expect for 250-unit runs?"
- **Defect trend analysis**: "Which defects appear most frequently?"
- **Supplier quality trends**: "Are shortages increasing from this supplier?"

---

## REST API Endpoints

When running with `python -m athena.run_athena serve --with-rag`:

### Core Production Endpoints

**POST /athena/runs**
```bash
curl -X POST http://localhost:8080/athena/runs \
  -H "Content-Type: application/json" \
  -d '{"deal_id": "DEAL-ABC123", "notes": "Enterprise deal"}'

# Response (202 Accepted)
{
  "run_id": "ATH-593C63E1A2",
  "status": "pending",
  "message": "Athena run ATH-593C63E1A2 queued. Poll /athena/runs/ATH-593C63E1A2 for results."
}
```

**GET /athena/runs/{run_id}**
```bash
curl http://localhost:8080/athena/runs/ATH-593C63E1A2

# Response (200 OK)
{
  "run_id": "ATH-593C63E1A2",
  "status": "success",
  "demand": { ... },
  "sourcing": { ... },
  "simulation": { ... },
  "execution": { ... },
  "quality": { ... },
  "errors": []
}
```

**GET /athena/runs**
```bash
curl http://localhost:8080/athena/runs

# Response: Map of all runs and statuses
{
  "ATH-593C63E1A2": "success",
  "ATH-2D8F1E9B": "running",
  "ATH-7A4C9E2": "failed"
}
```

### RAG Integration Endpoints

**POST /athena/rca/{run_id}**

Trigger root-cause analysis on a completed run (requires RAG):
```bash
curl -X POST http://localhost:8080/athena/rca/ATH-593C63E1A2

# Response (202 Accepted)
{
  "run_id": "ATH-593C63E1A2",
  "message": "RCA analysis queued. Poll /athena/rca/ATH-593C63E1A2 for results."
}
```

**GET /athena/rca/{run_id}**

Retrieve RCA results:
```bash
curl http://localhost:8080/athena/rca/ATH-593C63E1A2

# Response
{
  "yield_rate": 0.864,
  "total_inspections": 250,
  "defect_types": {
    "solder_bridge": 14,
    "cold_joint": 8,
    "missing_component": 7
  },
  "triggered_high_rpn_fmea_items": [
    {
      "item_id": "PFMEA-S22-001",
      "failure_mode": "Solder joint fatigue",
      "rpn": 156,
      "matched_defects": ["solder_bridge"]
    }
  ]
}
```

**POST /athena/predict**

Predict run risk and yield before execution:
```bash
curl -X POST "http://localhost:8080/athena/predict?deal_id=DEAL-XYZ&build_qty=200"

# Response
{
  "deal_id": "DEAL-XYZ",
  "build_qty": 200,
  "prediction": {
    "prediction_context": "Based on similar runs, expect 85–90% yield..."
  }
}
```

**GET /athena/health**

Liveness probe:
```bash
curl http://localhost:8080/athena/health

# Response
{ "status": "ok", "service": "athena-orchestrator" }
```

---

## Configuration

All configuration is env-var driven. See `athena/config.py` for the full list.

**Minimal dry-run (no API keys):**
```bash
ATHENA_DRY_RUN=true python main.py athena-run
```

**Full production setup:**
```bash
# Layer 1 - Pecan AI
export PECAN_API_KEY=sk-pecan-...
export PECAN_BASE_URL=https://api.pecan.ai/v1

# Layer 2 - Duro PLM + Nexar
export DURO_API_KEY=key-duro-...
export NEXAR_CLIENT_ID=...
export NEXAR_CLIENT_SECRET=...

# Layer 3 - Lokad + Ansys
export LOKAD_TOKEN=token-lokad-...
export LOKAD_ACCOUNT_ID=...
export ANSYS_TWIN_URL=https://ansys.example.com/api
export ANSYS_API_KEY=key-ansys-...

# Layer 4 - MacroFab + Fictiv
export MACROFAB_API_KEY=key-macrofab-...
export FICTIV_CLIENT_ID=...
export FICTIV_CLIENT_SECRET=...

# Layer 5 - Instrumental + TwinMaker
export INSTRUMENTAL_API_KEY=key-instrumental-...
export AWS_REGION=us-east-1
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
export TWINMAKER_WORKSPACE_ID=athena-workspace

# RAG Integration
export ANTHROPIC_API_KEY=sk-ant-...

# Run with live APIs
ATHENA_DRY_RUN=false python main.py athena-run
```

---

## Architecture Highlights

### Error Isolation
Each layer is isolated. If sourcing fails (blocking), quality doesn't run. If simulation fails (non-blocking), quality still runs with the data available.

### Async REST API
- Runs execute in background tasks
- Immediate response with run_id
- Client polls `GET /athena/runs/{run_id}` for results

### Mock-First Design
All layers use ATHENA_DRY_RUN=true by default, enabling:
- Full testing without API keys
- Deterministic output for CI/CD
- Easy debugging of orchestration logic

### In-Memory Run Store
For production, replace with Redis/DynamoDB. Current design suitable for prototyping and demos.

---

## Digital Twin Integration

Athena feeds quality data into the existing Digital Twin (`rag/digital_twin/twin.py`):
1. Inspection results update asset properties in TwinMaker
2. Failure predictions inform FMEA risk scores
3. Historical defect patterns improve next-run forecasting

The twin can then simulate assembly processes end-to-end with Athena-sourced part specs and quality baselines.

---

## File Structure

```
athena/
├── __init__.py
├── config.py              # Env var configuration
├── models.py              # Pydantic data models (all 5 layers)
├── orchestrator.py        # Core sequential coordinator
├── api.py                 # FastAPI REST server
├── rag_integration.py     # RAG ↔ Athena bridge (RCA + knowledge capture)
├── run_athena.py          # CLI entry point
└── layers/
    ├── __init__.py
    ├── demand.py          # Layer 1: Pecan AI
    ├── sourcing.py        # Layer 2: Duro PLM + Nexar
    ├── simulation.py      # Layer 3: Lokad + Ansys
    ├── execution.py       # Layer 4: MacroFab + Fictiv
    └── quality.py         # Layer 5: Instrumental + TwinMaker + RaaS/YaSS
```

---

## Next Steps

1. **Live API Integration**: Set env vars for real Pecan/Duro/Nexar/etc. APIs
2. **Persistence**: Replace in-memory run store with Redis/DynamoDB
3. **Monitoring**: Add structured logging and telemetry (OpenTelemetry)
4. **Scaling**: Containerize with Docker, deploy to Kubernetes
5. **Advanced RCA**: Build custom RAG queries for supplier quality trends, defect root causes, process optimizations

---

## Support

For questions or issues with Project Athena:
1. Check the mock output (run with `ATHENA_DRY_RUN=true`)
2. Review layer-specific env vars in `athena/config.py`
3. Check API response formats in `athena/models.py`
4. Inspect RAG integration in `athena/rag_integration.py`

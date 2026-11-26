# UI Architecture & Dependencies

## 🎨 Current UI Files (7 Python files)

```
dashboard/
├── app.py                          (Main entry point - 250 lines)
└── components/
    ├── __init__.py
    ├── node_cards.py               (Status cards for 3 nodes)
    ├── prediction_charts.py         (Plotly visualizations)
    ├── rag_panel.py                (RAG retrieval results panel)
    ├── ipc_panel.py                (IPC-A-610G heatmap)
    └── fishbone_panel.py           (Mermaid fishbone diagrams)

.streamlit/
└── config.toml                     (Theme, server settings)
```

---

## 🏗️ Dependency Tree

```
┌─────────────────────────────────────────────────────────────────┐
│                    Browser (Web Browser)                         │
│                      :8501 HTTP/WebSocket                        │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                  Streamlit Frontend (JS/React)                   │
│              (Auto-generated from Python code)                   │
└────────────────────────────┬────────────────────────────────────┘
                             │
┌────────────────────────────▼────────────────────────────────────┐
│                   dashboard/app.py                              │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ • Load secrets (ANTHROPIC_API_KEY from st.secrets)     │   │
│  │ • Initialize DigitalTwin (3 nodes: A, B, C)           │   │
│  │ • Initialize MQTT broker (simulate=True)              │   │
│  │ • Render sidebar controls (sliders, toggles, buttons) │   │
│  │ • Route to components based on predictions            │   │
│  │ • Auto-refresh on st.rerun()                          │   │
│  └─────────────────────────────────────────────────────────┘   │
└────────────────────────────┬────────────────────────────────────┘
             │               │              │
     ┌───────▼───┐    ┌──────▼──────┐  ┌───▼────────┐
     │ Backend   │    │ Components  │  │ Data Flow  │
     │ RAG/Twin  │    │ (UI render) │  │ (State)    │
     └───────────┘    └─────────────┘  └────────────┘
```

---

## 📊 Component Breakdown

### 1. **dashboard/app.py** (Main Entry Point)
**Purpose**: Page layout, state management, event handling

**Renders**:
- Sidebar with sliders (torque, press force, humidity, etc.)
- Top metrics (risk gauge, yield rate)
- Live streaming toggle
- Run buttons (manual prediction, batch sim)

**Dependencies**:
```python
import streamlit as st              # Web framework
from rag.digital_twin.twin import DigitalTwin
from rag.digital_twin.broker.mqtt_client import MQTTBroker
from dashboard.components.node_cards import render_fastening_card, ...
from dashboard.components.prediction_charts import render_risk_gauge, ...
from dashboard.components.rag_panel import render_rag_panel
from dashboard.components.fishbone_panel import render_fishbone_dashboard
from rag.digital_twin.nodes.*.py import *Reading
```

**State Variables** (session_state):
```python
st.session_state["snapshots"]        # List of TwinSnapshot objects
st.session_state["last_snapshot"]    # Current prediction result
st.session_state["broker_started"]   # Boolean flag
```

---

### 2. **dashboard/components/node_cards.py**
**Purpose**: Display Node A, B, C current status as styled cards

**Functions**:
```python
render_fastening_card(fastening_pred)    # Node A card
render_dispensing_card(dispensing_pred)  # Node B card
render_press_card(press_pred)            # Node C card
```

**Renders**:
- Node name + emoji
- Key metrics (torque, RSSI, contact resistance)
- Pass/Fail indicator
- Confidence score

**Dependencies**:
```python
import streamlit as st
from rag.digital_twin.nodes.fastening_node import FasteningPrediction
from rag.digital_twin.nodes.dispensing_node import DispensingPrediction
from rag.digital_twin.nodes.press_node import PressPrediction
```

---

### 3. **dashboard/components/prediction_charts.py**
**Purpose**: Interactive visualizations using Plotly

**Functions**:
```python
render_risk_gauge(risk_score, overall_risk)         # Gauge chart
render_risk_trend(snapshots)                        # Time series
render_node_histogram(snapshots)                    # Distribution
render_failure_mode_bar(failure_modes)              # Pareto chart
```

**Renders**:
- Risk score as circular gauge (0-100%)
- Risk trend over time (line chart)
- Node metric distributions (histograms)
- Top failure modes (bar chart, Pareto)

**Dependencies**:
```python
import streamlit as st
import plotly.graph_objects as go
import plotly.express as px
```

---

### 4. **dashboard/components/rag_panel.py**
**Purpose**: Display RAG-retrieved context & answer

**Function**:
```python
render_rag_panel(twin, snapshot)
```

**Renders**:
- Query input box
- Retrieved document chunks (top-5 reranked)
- Sources (filename, page, chunk index)
- RAG evaluation metrics (if enabled)

**Dependencies**:
```python
import streamlit as st
from rag.pipeline import RAGPipeline
from rag.digital_twin.twin import DigitalTwin, TwinSnapshot
```

---

### 5. **dashboard/components/ipc_panel.py**
**Purpose**: IPC-A-610G compliance heatmap & criteria status

**Functions**:
```python
render_ipc_panel(ipc_results)            # Per-stage sections
render_ipc_variability_heatmap(history)  # Heatmap of criteria×cycles
```

**Renders**:
- 3 expandable sections (fastening stage, dispensing, press)
- 10 IPC criteria with Target/Acceptable/PI/Defect color bands
- Heatmap: rows=criteria, cols=cycles, color=condition

**Dependencies**:
```python
import streamlit as st
import plotly.graph_objects as go
from rag.ipc.ipc_a610g import IPCInspector, IPCClass, IPCCondition
```

---

### 6. **dashboard/components/fishbone_panel.py**
**Purpose**: Mermaid fishbone diagrams for FMEA root cause analysis

**Functions**:
```python
render_fishbone_diagram(diagram)                    # Single diagram
render_fishbone_panel_for_fmea_items(items, max_n) # Expandable list
render_fishbone_dashboard(triggered, high_rpn)     # Tabs view
```

**Renders**:
- Mermaid syntax → Streamlit markdown rendering
- Expandable sections per FMEA item (RPN-sorted)
- Effects, causes, recommendations
- Status badge (Open/Closed/Mitigated)

**Dependencies**:
```python
import streamlit as st
from rag.fmea.fishbone import FishboneGenerator
from rag.fmea.fmea import FMEAItem
```

---

### 7. **.streamlit/config.toml**
**Purpose**: Streamlit global settings (theme, server, browser)

**Settings**:
```toml
[theme]
primaryColor = "#1f77b4"           # Blue accent
backgroundColor = "#0e1117"        # Dark background
secondaryBackgroundColor = "#1e1e2e"
textColor = "#ffffff"              # White text
font = "sans serif"                # Typography

[server]
headless = true                    # No browser auto-open
enableCORS = false                 # Security
maxUploadSize = 50                 # Max upload 50 MB
port = 8501

[browser]
gatherUsageStats = false           # No telemetry
```

---

## 🔄 Data Flow Diagram

```
User Input (Sidebar Sliders)
         │
         ▼
   dashboard/app.py
    st.session_state
         │
    ┌────┴─────────────────────────┐
    │ User clicks button:           │
    │ • Run Manual Prediction       │
    │ • Run Batch Simulation        │
    │ • Toggle Live Streaming       │
    └────┬─────────────────────────┘
         │
         ▼
  rag/digital_twin/twin.py
  predict(reading_a, reading_b, reading_c)
         │
         ├─▶ Node A: fastening_node.predict()
         ├─▶ Node B: dispensing_node.predict()
         └─▶ Node C: press_node.predict()
         │
         ▼
  TwinSnapshot (dataclass)
  {
    snapshot_id, timestamp,
    fastening, dispensing, press,
    risk_score, flagged_tests,
    triggered_fmea, high_rpn_fmea,
    ipc_results
  }
         │
         ▼
  st.session_state["last_snapshot"]
         │
    ┌────┴──────────────────────────────────────┐
    │ Render components based on snapshot:       │
    ├─▶ node_cards.render_fastening_card()      │
    ├─▶ node_cards.render_dispensing_card()     │
    ├─▶ node_cards.render_press_card()          │
    ├─▶ prediction_charts.render_risk_gauge()   │
    ├─▶ prediction_charts.render_risk_trend()   │
    ├─▶ prediction_charts.render_node_histogram()
    ├─▶ rag_panel.render_rag_panel()            │
    ├─▶ ipc_panel.render_ipc_panel()            │
    └─▶ fishbone_panel.render_fishbone_dashboard()
         │
         ▼
    Streamlit Frontend (React/JS)
         │
         ▼
    Browser :8501
```

---

## 📦 UI Dependency Versions

| Package | Version | Purpose |
|---------|---------|---------|
| `streamlit` | 1.58.0 | Web framework (auto-generates HTML/CSS/JS) |
| `plotly` | 6.8.0 | Interactive charts (risk gauge, trends, heatmaps) |
| `numpy` | 2.4.5 | Data arrays for charting |

**Rendering Chain**:
```
Python Code (dashboard/*.py)
    ↓
Streamlit Session (st.markdown, st.plotly_chart, st.metric, etc.)
    ↓
Streamlit Server (port 8501, HTTP/WebSocket)
    ↓
React Frontend (auto-bundled JavaScript)
    ↓
Browser (Chrome, Safari, Firefox)
```

---

## 🎯 Where UI Code Lives

**To modify the UI, edit these files:**

1. **Layout & page structure** → `dashboard/app.py`
   - Change sidebar controls, add new sections, reorder panels
   - Example: Add a new slider for a 4th node parameter

2. **Node status display** → `dashboard/components/node_cards.py`
   - Change card colors, add metrics, rename labels
   - Example: Display torque range instead of just current value

3. **Charts & graphs** → `dashboard/components/prediction_charts.py`
   - Change Plotly colors, add new chart types, adjust axes
   - Example: Add 3D scatter plot for multi-parameter correlation

4. **RAG context panel** → `dashboard/components/rag_panel.py`
   - Change result layout, add filters, highlight matching terms
   - Example: Add document source selector dropdown

5. **IPC compliance** → `dashboard/components/ipc_panel.py`
   - Change heatmap colors, add scoring breakdown, adjust thresholds
   - Example: Sort criteria by failure rate instead of section order

6. **Fishbone diagrams** → `dashboard/components/fishbone_panel.py`
   - Change diagram styling, add cause grouping, adjust layout
   - Example: Filter to only show open/active FMEA items

7. **Theme & styling** → `.streamlit/config.toml`
   - Change colors, fonts, container width, button styles
   - Example: Switch to light theme, set custom brand colors

---

## 🌐 Browser Architecture

When you open `http://localhost:8501`:

```
┌──────────────────────────────────────────────┐
│  Browser                                     │
├──────────────────────────────────────────────┤
│  HTML (auto-generated)                       │
│  • <meta> tags, favicon, theme color         │
│  • <script> Streamlit client library (JS)    │
│  • <div id="root"> container                 │
│                                              │
│  CSS (Streamlit default)                     │
│  • Dark theme (#0e1117 background)           │
│  • Responsive grid layout                    │
│  • Plotly chart styles                       │
│                                              │
│  JavaScript (Streamlit + React)              │
│  • WebSocket to :8501 server                 │
│  • Handle user input (sliders, buttons)      │
│  • Re-render on st.rerun()                   │
│  • Plotly.js for chart interactivity         │
└──────────────────────────────────────────────┘
```

**No separate HTML/CSS files** — Streamlit generates everything from Python.

---

## 🚀 To Add New UI Elements

### Example 1: Add a new metric card
```python
# In dashboard/app.py, after node cards:
st.metric(
    label="Thermal Runaway Risk",
    value=f"{last.dispensing.thermal_risk:.1%}",
    delta="↓ -5%" if last.dispensing.thermal_risk < 0.35 else None,
)
```

### Example 2: Add a new chart
```python
# In dashboard/components/prediction_charts.py:
def render_torque_distribution(snapshots):
    import plotly.graph_objects as go
    import streamlit as st
    
    torques = [s.fastening.torque_nm for s in snapshots]
    fig = go.Figure(data=[go.Histogram(x=torques, nbinsx=20)])
    fig.update_layout(title="Torque Distribution", xaxis_title="Torque (Nm)")
    st.plotly_chart(fig, use_container_width=True)

# Call in app.py:
render_torque_distribution(snapshots)
```

### Example 3: Add a new sidebar control
```python
# In dashboard/app.py sidebar:
with st.sidebar:
    st.subheader("Advanced Settings")
    confidence_threshold = st.slider("Min confidence", 0.0, 1.0, 0.7)
    show_raw_physics = st.checkbox("Show physics calculations")
```

---

## 📱 Responsive Design

Streamlit is **mobile-responsive** by default:
- Desktop: sidebar on left, main content full width
- Mobile: sidebar collapses, hamburger menu
- Tablets: adaptable 2-column layout

No CSS media queries needed — Streamlit handles it.

---

## 🔐 Security in UI

- **Secrets**: `ANTHROPIC_API_KEY` loaded from `st.secrets` (Streamlit Cloud) or env vars
- **No API key in UI code** — never hardcoded
- **CORS disabled** in config (only same-origin requests)
- **Rate limiting**: Streamlit Cloud limits at 1 request/sec default

---

## 📈 Performance Notes

- **Caching**: `@st.cache_resource` for `_build_twin()` — initializes once per session
- **Reruns**: `st.rerun()` re-executes script; use `st.session_state` to preserve data
- **Plotly**: ~50-100ms to render per chart (acceptable for dashboards)
- **MQTT broker**: Simulates ~10 messages/sec; drain timeout=0.05s to avoid blocking


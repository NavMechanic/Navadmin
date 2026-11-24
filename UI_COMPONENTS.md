# UI Components Visual Map

## 🖼️ Dashboard Layout

```
┌────────────────────────────────────────────────────────────────────────┐
│                    🏭 Aviator Digital Twin                             │
│              4M Process Variables → Predicted Test Outcomes            │
└────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────┬──────────────────────────────────────────────┐
│                         │                                              │
│   SIDEBAR               │           MAIN CONTENT                       │
│   (Left, ~300px)        │           (Responsive Grid)                  │
│                         │                                              │
├─────────────────────────┼──────────────────────────────────────────────┤
│                         │                                              │
│ 🏭 Aviator Twin        │  ┌─────────────────────────────────────────┐ │
│ Phase 2 Simulation      │  │ Risk Gauge + Metrics (Row 1)           │ │
│                         │  │ ┌──────────────┬───────────────────┐  │ │
│ 🟢 RAG: Online ✓       │  │ │ Gauge 0-100% │ Snapshot ID      │  │ │
│ (API key loaded)        │  │ │              │ Timestamp        │  │ │
│                         │  │ │              │ Status badge    │  │ │
│ ═══════════════         │  │ └──────────────┴───────────────────┘  │ │
│                         │  └─────────────────────────────────────────┘ │
│ Simulation Controls     │                                              │
│ ☑ Live PLC Streaming   │  ┌─────────────────────────────────────────┐ │
│ ← Refresh (3s slider)   │  │ Node Status Cards (Row 2)               │ │
│                         │  │ ┌────────────┬────────────────────┐    │ │
│ Manual Inputs           │  │ │ Node A     │ Node B             │    │ │
│ Node A:                 │  │ │ Fastening  │ Dispensing         │    │ │
│ ⊢ Torque: 0.45 Nm      │  │ │            │                    │    │ │
│ ⊢ Depth:  3.2 mm       │  │ └────────────┴────────────────────┘    │ │
│                         │  │ ┌──────────────────────────────────┐   │ │
│ Node B:                 │  │ │ Node C: Press Station             │   │ │
│ ⊢ TIM Mass: 352 mg     │  │ └──────────────────────────────────┘   │ │
│ ⊢ Humidity: 45% RH     │  └─────────────────────────────────────────┘ │
│                         │                                              │
│ Node C:                 │  ┌─────────────────────────────────────────┐ │
│ ⊢ Press: 1400 N        │  │ Risk Trend Chart (Plotly Line)          │ │
│ ⊢ Offset: 0.0 mm       │  │ ───────────────────────────────────    │ │
│                         │  │   Risk Score over 50 cycles            │ │
│ [▶ Manual Pred]         │  └─────────────────────────────────────────┘ │
│ [⚡ Batch Sim]         │                                              │
│                         │  ┌─────────────────────────────────────────┐ │
│ ═══════════════         │  │ Node Distributions (Histograms)        │ │
│ Total: 50 cycles        │  │ ┌────────────────┬──────────────────┐ │ │
│ Yield: 94.0%            │  │ │ Torque Dist    │ TIM Temp Dist    │ │ │
│                         │  │ └────────────────┴──────────────────┘ │ │
│ github.com/NavMechanic/ │  └─────────────────────────────────────────┘ │
│ Navadmin                │                                              │
│                         │  ┌─────────────────────────────────────────┐ │
└─────────────────────────┤  │ Failure Mode Pareto (Bar Chart)          │ │
                          │  │ 12x IP68_VACUUM_FAIL                     │ │
                          │  │  8x FINGERPRINT_TEST_FAIL                │ │
                          │  │  5x THERMAL_RUNAWAY                      │ │
                          │  └─────────────────────────────────────────┘ │
                          │                                              │
                          │  ┌─────────────────────────────────────────┐ │
                          │  │ RAG Panel (Retrieval Results)            │ │
                          │  │ ┌ Query Input Box                       │ │
                          │  │ └ Top-5 Retrieved Chunks                │ │
                          │  │   [Source] [Page] [Score]              │ │
                          │  └─────────────────────────────────────────┘ │
                          │                                              │
                          │  ┌─────────────────────────────────────────┐ │
                          │  │ IPC-A-610G Compliance Panel             │ │
                          │  │ ▼ Fastening Stage (3 criteria)         │ │
                          │  │   ✓ Warp                  Target       │ │
                          │  │   ⚠ Contact Res           PI          │ │
                          │  │   ✗ RSSI                  Defect      │ │
                          │  │ ▼ Dispensing Stage (4 criteria)        │ │
                          │  │ ▼ Press Stage (3 criteria)             │ │
                          │  └─────────────────────────────────────────┘ │
                          │                                              │
                          │  ┌─────────────────────────────────────────┐ │
                          │  │ Fishbone RCA Diagrams (Tabs)            │ │
                          │  │ ┌Triggered─────┬─High-Risk────────────┐ │ │
                          │  │ │ PFMEA-S22-001│ PFMEA-S22-002        │ │ │
                          │  │ │ (RPN: 240)   │ (RPN: 225)           │ │ │
                          │  │ │              │                      │ │ │
                          │  │ │ Failure Mode:│ ┌─────────────────┐ │ │ │
                          │  │ │ Torque Error │ │   Fishbone      │ │ │ │
                          │  │ │              │ │   (Mermaid)     │ │ │ │
                          │  │ │ Causes:      │ └─────────────────┘ │ │ │
                          │  │ │ • Fixture    │                      │ │ │
                          │  │ │ • Calibration│                      │ │ │
                          │  │ │              │                      │ │ │
                          │  │ └──────────────┴──────────────────────┘ │ │
                          │  └─────────────────────────────────────────┘ │
                          │                                              │
                          └──────────────────────────────────────────────┘
```

---

## 📋 Component Imports & File Locations

### Row 1: Risk Gauge + Metrics
| Component | Source File | Type | Library |
|-----------|-----------|------|---------|
| Risk Gauge (0-100%) | `prediction_charts.py` | Plotly Gauge | plotly.graph_objects |
| Snapshot ID metric | `app.py` | st.metric | streamlit |
| Timestamp metric | `app.py` | st.metric | streamlit |
| Status badge | `app.py` | st.error / st.success | streamlit |

### Row 2: Node Status Cards
| Component | Source File | Type | Library |
|-----------|-----------|------|---------|
| Node A Card (Fastening) | `node_cards.py:render_fastening_card()` | st.metric stack | streamlit |
| Node B Card (Dispensing) | `node_cards.py:render_dispensing_card()` | st.metric stack | streamlit |
| Node C Card (Press) | `node_cards.py:render_press_card()` | st.metric stack | streamlit |

Each card shows:
- Node emoji + name
- 3-5 key metrics (values)
- Pass/Fail color indicator
- Confidence score

### Row 3: Risk Trend
| Component | Source File | Type | Library |
|-----------|-----------|------|---------|
| Line Chart | `prediction_charts.py:render_risk_trend()` | Plotly Line | plotly.graph_objects |
| X-axis | Cycle number (0-50) | – | – |
| Y-axis | Risk score (0-1) | – | – |
| Trace | Last 50 snapshots | – | – |

### Row 4: Node Distributions
| Component | Source File | Type | Library |
|-----------|-----------|------|---------|
| Torque Histogram | `prediction_charts.py:render_node_histogram()` | Plotly Histogram | plotly.graph_objects |
| TIM Temp Histogram | Same | Plotly Histogram | plotly.graph_objects |
| Data | All snapshots | – | numpy arrays |

### Row 5: Failure Mode Pareto
| Component | Source File | Type | Library |
|-----------|-----------|------|---------|
| Bar Chart | `prediction_charts.py:render_failure_mode_bar()` | Plotly Bar | plotly.graph_objects |
| X-axis | Failure mode codes | – | – |
| Y-axis | Count (0-20) | – | – |
| Sorting | Descending (highest first) | – | – |

### Row 6: RAG Panel
| Component | Source File | Type | Library |
|-----------|-----------|------|---------|
| Query Input | `rag_panel.py:render_rag_panel()` | st.text_input | streamlit |
| Retrieved Chunks | Same | st.expander (top-5) | streamlit |
| Source labels | Same | st.caption | streamlit |
| Scores | Same | st.metric | streamlit |

### Row 7: IPC Compliance
| Component | Source File | Type | Library |
|-----------|-----------|------|---------|
| 3 Tabs (stages) | `ipc_panel.py:render_ipc_panel()` | st.tabs | streamlit |
| Expandable sections | Same | st.expander | streamlit |
| Criteria rows | Same | st.columns | streamlit |
| Color bands | Same | st.markdown (CSS badge) | streamlit |
| Heatmap | `ipc_panel.py:render_ipc_variability_heatmap()` | Plotly Heatmap | plotly.graph_objects |

### Row 8: Fishbone Diagrams
| Component | Source File | Type | Library |
|-----------|-----------|------|---------|
| Tab group | `fishbone_panel.py:render_fishbone_dashboard()` | st.tabs | streamlit |
| Expandable items | Same | st.expander | streamlit |
| Mermaid diagram | `fishbone_panel.py:render_fishbone_diagram()` | st.markdown (code block) | streamlit (native mermaid) |
| FMEA metadata | Same | st.markdown | streamlit |

---

## 🎛️ Sidebar Components

| Control | Type | Range | Source |
|---------|------|-------|--------|
| RAG Status Badge | Custom text | Online / Offline | `app.py` line 42-45 |
| Live PLC Toggle | st.toggle | on / off | `app.py` line 50 |
| Refresh Interval | st.slider | 1-10 sec | `app.py` line 51 |
| Torque Slider | st.slider | 0.15-0.85 Nm | `app.py` line 56 |
| Depth Slider | st.slider | 2.5-4.0 mm | `app.py` line 57 |
| TIM Mass Slider | st.slider | 290-420 mg | `app.py` line 61 |
| Humidity Slider | st.slider | 20-90 %RH | `app.py` line 62 |
| Press Force Slider | st.slider | 980-1820 N | `app.py` line 66 |
| Frame Offset Slider | st.slider | -0.2-0.2 mm | `app.py` line 67 |
| Manual Pred Button | st.button (primary) | press / release | `app.py` line 69 |
| Batch Sim Button | st.button | press / release | `app.py` line 70 |
| Cycle Counter | st.caption | 0-50 | `app.py` line 73 |
| Yield Metric | st.metric | 0-100% | `app.py` line 75 |

---

## 🔌 UI-to-Backend Connections

```
┌─ UI Input ────────────────────────────┐
│                                       │
│  Slider: Torque = 0.45 Nm            │
│  Slider: Press Force = 1400 N         │
│  Slider: Humidity = 45% RH            │
│  Button: [Run Manual Prediction]     │
│                                       │
└────────────────┬──────────────────────┘
                 │
                 ▼
        dashboard/app.py
        if run_manual:
            fa_r = FasteningReading(torque_nm=0.45, ...)
            di_r = DispensingReading(humidity_pct=45, ...)
            pr_r = PressReading(inlay_force_n=1400, ...)
                 │
                 ▼
        rag/digital_twin/twin.py
        twin.predict(fa_r, di_r, pr_r)
                 │
        ┌────────┼────────┐
        │        │        │
        ▼        ▼        ▼
    NodeA    NodeB    NodeC
    .predict() .predict() .predict()
        │        │        │
        └────────┼────────┘
                 │
                 ▼
        TwinSnapshot
        {
          fastening: FasteningPrediction,
          dispensing: DispensingPrediction,
          press: PressPrediction,
          risk_score: 0.35,
          flagged_tests: ["IP68_VACUUM_FAIL"],
          triggered_fmea: [...],
          ipc_results: [...]
        }
                 │
                 ▼
        st.session_state["last_snapshot"]
                 │
        ┌────────┴────────────────────────┐
        │ UI Components re-render with:   │
        ├─ render_fastening_card()        │
        ├─ render_dispensing_card()       │
        ├─ render_press_card()            │
        ├─ render_risk_gauge()            │
        ├─ render_risk_trend()            │
        ├─ render_rag_panel()             │
        ├─ render_ipc_panel()             │
        └─ render_fishbone_dashboard()    │
        │                                 │
        └────────────────┬────────────────┘
                         │
                         ▼
                   Browser Display
                   (HTML/CSS/JS rendered
                    from Streamlit)
```

---

## 🎨 Color & Theme Reference

| Element | Color | Hex | Used In |
|---------|-------|-----|---------|
| Primary accent | Blue | #1f77b4 | Buttons, risk when OK, highlights |
| Success/Pass | Green | #00D084 | st.success(), RSSI PASS badge |
| Warning/PI | Yellow | #FDB913 | Process Indicator conditions |
| Danger/Fail | Red | #FF4B4B | st.error(), FAIL badges |
| Background | Dark | #0e1117 | Page background |
| Secondary BG | Dark Gray | #1e1e2e | Cards, containers |
| Text | White | #ffffff | All text |
| Borders | Gray | #30363D | Card borders |

---

## 📏 Responsive Breakpoints

| Device | Width | Layout |
|--------|-------|--------|
| Mobile | < 600px | Sidebar collapses, single column |
| Tablet | 600-1200px | 2-column grid (partial sidebar) |
| Desktop | > 1200px | Sidebar + 3+ column main content |

Streamlit handles all responsive behavior automatically.

---

## 🚀 Performance Metrics

| Component | Render Time | Heaviness |
|-----------|-------------|-----------|
| Node cards | 10-20ms | Light (st.metric) |
| Risk gauge | 50-100ms | Medium (Plotly) |
| Line chart | 50-100ms | Medium (Plotly) |
| Heatmap | 100-200ms | Heavy (Plotly, large data) |
| Fishbone (Mermaid) | 20-50ms | Light (SVG rendering) |
| **Total page load** | **300-500ms** | – |
| **Re-render on prediction** | **500-1000ms** | – |


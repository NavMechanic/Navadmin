# Streamlit Dashboard — Live Demo

## 🖥️ What You See in the Browser

When you open `http://localhost:8501`, here's the exact layout:

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃                   🏭 Aviator Digital Twin — Live Assembly Simulation           ┃
┃                 4M Process Variables → Predicted Functional Test Outcomes      ┃
┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛

┌─ LEFT SIDEBAR ─────────────────┬─ MAIN CONTENT ────────────────────────────────┐
│                                │                                               │
│ 🏭 Aviator Digital Twin        │  ┌─ Risk Gauge + Metrics ─────────────────┐  │
│ Phase 2 · Process Simulation   │  │                                         │  │
│                                │  │   ◯ Risk: 0.0%    │ Snapshot ID: SNAP- │  │
│ 🟢 RAG: Online ✓              │  │    (Gauge Chart)   │  Timestamp: 19:54  │  │
│                                │  │                    │ ✓ All tests PASS   │  │
│ ═════════════════              │  │                    │                    │  │
│                                │  │                    │                    │  │
│ Simulation Controls            │  └─────────────────────────────────────────┘  │
│ ☑ Live PLC Streaming          │                                               │
│ ← Refresh: 3s (slider)         │  ┌─ Node Status Cards (Row 2) ──────────────┐ │
│                                │  │                                           │ │
│ Manual Inputs — Node A         │  │ ┌──────────────┐ ┌──────────────┐       │ │
│ ⊢ Torque: 0.45 Nm (slider)    │  │ │ 📡 Node A    │ │ 🌡️  Node B   │       │ │
│ ⊢ Depth: 3.2 mm (slider)      │  │ │              │ │              │       │ │
│                                │  │ │ Resistance   │ │ Temp: 70.7°C │       │ │
│ Manual Inputs — Node B         │  │ │ 0.2716 mΩ    │ │ Margin: 14°C │       │ │
│ ⊢ TIM Mass: 352 mg (slider)   │  │ │              │ │              │       │ │
│ ⊢ Humidity: 45% RH (slider)   │  │ │ Test: PASS ✓ │ │ Test: PASS ✓ │       │ │
│                                │  │ └──────────────┘ └──────────────┘       │ │
│ Manual Inputs — Node C         │  │                                           │ │
│ ⊢ Press: 1400 N (slider)      │  │ ┌──────────────────────────────────────┐ │ │
│ ⊢ Offset: 0.0 mm (slider)     │  │ │           🔧 Node C: PRESS           │ │ │
│                                │  │ │ Gap: 0.11 mm │ Air Risk: 7.8%  PASS  │ │ │
│ ┌──────────────────────────┐   │  │ └──────────────────────────────────────┘ │ │
│ │ ▶ Manual Prediction      │   │  └───────────────────────────────────────────┘ │
│ │ ⚡ Batch Simulation (50) │   │                                               │
│ └──────────────────────────┘   │  ┌─ Risk Trend Chart (Plotly) ────────────────┐ │
│                                │  │                                            │ │
│ ═════════════════              │  │  Risk Score                                │ │
│                                │  │    │     ╱╲                              │ │
│ Total cycles: 48               │  │  100├─ ╱  ╲                              │ │
│                                │  │    │╱      ╲╱╲    ╱╲    ╱                │ │
│ Yield: 96.0%                  │  │   50├───────────╲──╱  ╲──╱─────           │ │
│                                │  │    │                                      │ │
│                                │  │    └─────┴─────┴─────┴─────────           │ │
│                                │  │    0   10   20   30   40   50             │ │
│                                │  │              Cycle                        │ │
│                                │  └────────────────────────────────────────────┘ │
│                                │                                               │
│                                │  ┌─ Node Distributions (Histograms) ──────────┐ │
│                                │  │ ┌──────────────┬─────────────────────┐   │ │
│                                │  │ │ Torque (Nm)  │ Temperature (°C)    │   │ │
│                                │  │ │   ▁▂▃▄▅▆▇▃  │ ▂▄▆▇▆▅▄▂           │   │ │
│                                │  │ │               │                    │   │ │
│                                │  │ └──────────────┴─────────────────────┘   │ │
│                                │  └────────────────────────────────────────────┘ │
│                                │                                               │
│                                │  ┌─ Failure Mode Pareto ─────────────────────┐ │
│                                │  │ 2  │██████████████████████████████│ VACUUM  │ │
│                                │  │    │                    │ THERMAL  │ │
│                                │  └────────────────────────────────────────────┘ │
│                                │                                               │
│                                │  ┌─ RAG Panel (AI-Powered RCA) ──────────────┐ │
│                                │  │ Query: [What causes RF mismatch?]         │ │
│                                │  │                                            │ │
│                                │  │ Retrieved Context:                         │ │
│                                │  │ ▸ Battery impedance specs (page 5)         │ │
│                                │  │ ▸ Antenna contact loss (page 12)           │ │
│                                │  │ ▸ Connector mating force (SOP-45)          │ │
│                                │  └────────────────────────────────────────────┘ │
│                                │                                               │
│                                │  ┌─ IPC-A-610G Compliance ───────────────────┐ │
│                                │  │ ▼ Fastening Stage (3 criteria)            │ │
│                                │  │   ✓ Warp Proxy        Target       0.003 │ │
│                                │  │   ⚠ Contact Resist    PI          0.35   │ │
│                                │  │   ✗ RF Mismatch       Defect      -5 dB  │ │
│                                │  │                                            │ │
│                                │  │ ▼ Dispensing Stage (4 criteria)           │ │
│                                │  │ ▼ Press Stage (3 criteria)                │ │
│                                │  └────────────────────────────────────────────┘ │
│                                │                                               │
│                                │  ┌─ Fishbone RCA Diagrams ──────────────────┐ │
│                                │  │ ┌─ Triggered Failures ─┬─ High-Risk ────┐│ │
│                                │  │ │ PFMEA-S22-001         │ PFMEA-S22-005 ││ │
│                                │  │ │ (RPN: 240)            │ (RPN: 216)    ││ │
│                                │  │ │                       │               ││ │
│                                │  │ │ Failure Mode:         │   Mermaid     ││ │
│                                │  │ │ Torque Error          │   Fishbone    ││ │
│                                │  │ │                       │   Diagram:    ││ │
│                                │  │ │ Causes:               │               ││ │
│                                │  │ │ • Fixture tolerance   │   Cause 1     ││ │
│                                │  │ │ • Calibration drift   │   Cause 2  ─→ ││ │
│                                │  │ │ • Temperature change  │   Cause 3  ─→ ││ │
│                                │  │ │                       │   Effect   ←── ││ │
│                                │  │ └───────────────────────┴──────────────────┘│ │
│                                │  └────────────────────────────────────────────┘ │
│                                │                                               │
└────────────────────────────────┴───────────────────────────────────────────────┘
```

---

## 📊 Dashboard Data Flow

### Real Demo Output (50 cycles)

```
✓ CYCLE 1 (Risk: 0.0% — PASS)
   📡 Node A: Contact Resistance = 0.2716 mΩ
   🌡️  Node B: Junction Temperature = 70.7°C (Margin: 14.3°C)
   🔧 Node C: Gap Clearance = 0.10913 mm (Air Pocket: 7.82%)

✓ CYCLE 2 (Risk: 0.0% — PASS)
   📡 Node A: Contact Resistance = 0.4357 mΩ
   🌡️  Node B: Junction Temperature = 74.5°C (Margin: 10.5°C)
   🔧 Node C: Gap Clearance = 0.10811 mm (Air Pocket: 10.23%)

✓ CYCLE 3 (Risk: 0.0% — PASS)
   📡 Node A: Contact Resistance = 0.2364 mΩ
   🌡️  Node B: Junction Temperature = 74.8°C (Margin: 10.2°C)
   🔧 Node C: Gap Clearance = 0.11437 mm (Air Pocket: 34.08%)

[... 47 more cycles ...]

RESULTS:
  Predicted Yield: 96.0% (48/50 PASS)
  Failure Mode: IP68_VACUUM_LEAK_TEST (2x)
```

---

## 🎮 Interactive Features

### 1. **Sidebar Sliders** → Real-Time Prediction

Move any slider → Click **[▶ Manual Prediction]** → Results update instantly

```
Before:
  Torque: 0.45 Nm

After moving to 0.65 Nm:
  Contact Resistance: 0.18 mΩ (↓ lower is better)
  RF RSSI: -0.5 dB (unchanged)
  Result: PASS ✓
```

### 2. **Batch Simulation** → 50 Cycles in 2 seconds

Click **[⚡ Run Batch Simulation (n=50)]**

```
┌─────────────────────────────────────┐
│ Simulating 50 assembly cycles…      │
│ ████████████████████ 50/50 100%     │
│                                     │
│ ✓ Batch complete                    │
│ Predicted yield: 96.0%              │
└─────────────────────────────────────┘
```

### 3. **Live Streaming** → Auto-refresh

Toggle **☑ Live PLC Streaming**

```
Refresh interval: 3s
  ↓ New MQTT data arrives every 1.5s
  ↓ Dashboard auto-updates
  ↓ Charts animate smoothly
  ↓ No click needed — watch it live!
```

### 4. **Charts are Interactive** (Plotly)

```
Risk Gauge:
  • Hover to see exact %
  • Click legend to hide traces

Risk Trend Chart:
  • Drag to zoom
  • Double-click to reset
  • Hover for exact cycle values

Pareto Chart:
  • Sort by clicking bars
  • Compare failure rates visually

IPC Heatmap:
  • Click to expand sections
  • Color shows Target/Acceptable/PI/Defect
```

---

## 🚀 Quick Start — Try It Now

```bash
cd /home/user/Navadmin

# Option 1: Launch dashboard (simulation mode)
streamlit run dashboard/app.py

# Option 2: Launch dashboard (with RAG enabled)
streamlit run dashboard/app.py -- --with-rag

# Access in browser:
open http://localhost:8501
```

Then:
1. Move sliders in the sidebar
2. Click **[▶ Manual Prediction]** → See results update
3. Click **[⚡ Run Batch Simulation]** → 50 cycles in 2 sec
4. Toggle **☑ Live PLC Streaming** → Auto-refresh dashboard
5. Scroll down → See all charts, fishbone diagrams, IPC heatmap

---

## 📱 Mobile Responsive

```
Desktop (> 1200px):
  ┌──────────┬─────────────────┐
  │ Sidebar  │ Main Content (3 cols)
  └──────────┴─────────────────┘

Tablet (600-1200px):
  ┌──────────┬──────────────┐
  │ Sidebar  │ Main (2 cols)
  └──────────┴──────────────┘

Mobile (< 600px):
  ┌──────────┐
  │ Hamburger│ (sidebar collapses)
  ├──────────┤
  │ Main (1) │
  └──────────┘
```

---

## 🎨 Theme

```
Color Scheme:
  • Background: #0e1117 (dark gray)
  • Accent: #1f77b4 (blue)
  • Success: #00D084 (green, PASS)
  • Warning: #FDB913 (yellow, PI)
  • Danger: #FF4B4B (red, FAIL)
  • Text: #ffffff (white)

Font: Sans serif (system default)
```

---

## 🔄 Data Update Cycle

```
                User Input
                   │
                   ▼
        Move slider / Click button
                   │
      ┌────────────┴────────────┐
      │                         │
      ▼                         ▼
   Manual                  Live Streaming
   Prediction              (auto every 3s)
      │                         │
      └────────────┬────────────┘
                   │
                   ▼
        twin.predict() [3 nodes]
                   │
        ┌──────────┼──────────┐
        │          │          │
        ▼          ▼          ▼
     Node A    Node B     Node C
                   │
        ┌──────────┴──────────┐
        │                     │
        ▼                     ▼
    TwinSnapshot        st.session_state
        │                     │
        └──────────┬──────────┘
                   │
                   ▼
        Components re-render
     (node_cards, charts, etc.)
                   │
                   ▼
            st.rerun()
                   │
                   ▼
          Browser updates instantly
```


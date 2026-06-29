# Project Athena: 6-Phase Manufacturing Orchestration

## Overview

Project Athena replaces traditional hardware prototyping cycles (EVT/DVT/PVT) with an AI-driven **Simulate-Optimize-Build** paradigm. The system orchestrates hardware manufacturing through **6 integrated phases**:

```
Demand → Sourcing → Simulation → Manufacturing → Testing → Quality/Financial
```

Each phase feeds forward; failures are isolated and logged for root-cause analysis. All systems default to mock mode (`ATHENA_DRY_RUN=true`) requiring no external API keys.

---

## Architecture

### Phase 1: Demand Forecasting (phase1_demand/)
**Goal:** Forecast demand with safety buffer for procurement planning.

- **Input:** Market size (annual units), safety uplift percentage
- **Process:**
  - Uses Pecan AI (mock) for demand forecasting
  - Applies 10% safety uplift by default
  - Returns procurement recommendation
- **Output:** `DemandForecast` → forecasted demand, safety margin
- **Error Handling:** Non-blocking; proceeds even if API unavailable

**Key Functions:**
```python
demand_forecast = demand_service.forecast(
    market_units=50000,
    safety_uplift_pct=10.0
)
# → {"demand_units": 55000, "safety_margin": 5000}
```

---

### Phase 2: Supply Chain Sourcing (phases/phase2_sourcing/)
**Goal:** Validate part availability and identify substitutes.

- **Input:** Bill of Materials (BOM)
- **Process:**
  - Queries Nexar GraphQL API for part availability
  - Checks lead times against manufacturing schedule
  - Maps alternate part numbers if lead time > 8 weeks
- **Output:** `SourcingResult` → availability status, alternates, lead times
- **Error Handling:** Gracefully handles API timeouts; falls back to historical data

**Key Functions:**
```python
sourcing_result = sourcing_service.validate_bom(bom_items)
# → {"available_parts": 145, "substitutions": 3, "critical_delays": []}
```

---

### Phase 3: Digital Twin Simulation (phases/phase4_digital_twin/)
**Goal:** Validate thermal and mechanical performance before manufacturing.

#### 3a. PINN Thermal Solver (`pinn_solver.py`)
- **Solves:** 2D Poisson heat equation (∇²T + q'''=0)
- **Speed:** <1ms (1,800,000× faster than CFD @ 30min)
- **Method:** Physics-Informed Neural Network with Jacobi iteration (50 iterations)
- **Boundary Conditions:** Isothermal edges (T=T_ambient), distributed interior heat generation
- **Output:** Max temp, mean temp, hotspot location (mm), confidence score

```python
pinn = PINNThermalSolver()
thermal_out = pinn.solve(ThermalInput(
    ambient_temp_c=25.0,
    power_dissipation_w=5.0,
    board_size_mm=(100, 80),
    component_locations=[{"x_mm": 50, "y_mm": 40, "power_w": 2.5}],
))
# → ThermalOutput(max_temp_c=72.5, passes_thermal_spec=True, confidence=0.95, solve_time_ms=0.8)
```

#### 3b. Cloud Simulation Dispatcher (`cloud_simulator.py`)
- **When:** If PINN confidence < 0.85 or design is novel
- **Simulations:** Thermal CFD, mechanical FEA, RF HFSS, multi-physics
- **Provider:** Ansys Cloud (via REST API)
- **Cost:** $150-500 per simulation
- **Duration:** ~30 minutes
- **Output:** `SimulationJob` → stress, thermal, vibration, margin of safety, recommendations

```python
dispatcher = CloudSimulationDispatcher(ansys_api_key="...")
job = dispatcher.submit_simulation(
    design_id="DESIGN-001",
    sim_type=SimulationType.THERMAL_CFD,
    cad_file_url="s3://designs/model.step",
    parameters={"mesh_density": "fine"}
)
# → SimulationJob(job_id="SIM-ABC123", status="queued", ...)

# Check status later
job = dispatcher.check_status("SIM-ABC123")
validation = dispatcher.validate_result(job)
# → {"passed": True, "max_stress_mpa": 85.3, "thermal_delta_c": 22.5, ...}
```

---

### Phase 4: Manufacturing Execution (phases/phase5_execution/)
**Goal:** Prepare design files, authorize build, dispatch to vendors.

#### File Preparation (`manufacturing_orchestrator.py`)
- **Validates:**
  - Gerber file checksums (SHA256)
  - STEP mechanical model
  - Bill of Materials completeness
- **Outputs:** `DesignPackage` with checksums and metadata

```python
orch = ManufacturingOrchestrator()
success, package, issues = orch.prepare_design_package(
    design_id="DESIGN-001",
    version="2.0",
    gerber_paths=["files/F.Cu.gbr", "files/B.Cu.gbr", ...],
    step_path="files/assembly.step",
    bom_items=[BOMItem("nRF52840", "SoC", 1, 8.50, 4, "Digi-Key"), ...],
    pcb_dims_mm=(100, 80),
    layer_count=4,
)
```

#### Authorization Gate
- **Master switch** to halt all manufacturing during supply chain disruptions
- **Checks:** Design completeness, cost within budget, part availability

```python
# Close manufacturing gate
orch.set_authorization_gate(False)

# Authorize specific build
success, msg = orch.authorize_build(build_id="BUILD-001", authorized_by="engineer@company.com")
```

#### Vendor Dispatch
- **Simultaneous dispatch** to MacroFab + Fictiv
- **Splits quantity** across vendors for redundancy
- **Tracks:** Order IDs, expected completion dates, costs

```python
build_job = orch.create_build_job(
    design_id="DESIGN-001",
    quantity=5000,
    design_package=package,
)

success, result = orch.dispatch_build(
    build_id=build_job.build_id,
    vendors=[VendorType.MACROFAB, VendorType.FICTIV],
)
# → {"macrofab": "MFB-12345", "fictiv": "FTV-67890"}
```

---

### Phase 5: Hardware Testing (phases/phase6_observability/hil_test_generator.py)
**Goal:** Auto-generate and execute Hardware-in-Loop (HIL) tests.

#### HIL Test Auto-Generator
- **Input:** Device specification JSON (example: `example_device_spec.json`)
- **Parses:** Components (Bluetooth, camera, battery, USB, IMU, NFC, etc.)
- **Generates:** pytest functions with component-specific tests
- **Output:** Executable pytest file

**Example Device Spec:**
```json
{
  "device_name": "WiFi Beacon v2.0",
  "components": [
    {
      "name": "Bluetooth",
      "type": "bluetooth",
      "part_number": "nRF52840",
      "min_voltage_v": 3.0,
      "max_voltage_v": 3.6,
      "timeout_s": 5.0
    },
    ...
  ]
}
```

**Generated Tests (pytest):**
- Bluetooth: AT command response, name query, voltage monitoring
- Camera: Sensor detection, frame capture, resolution validation
- Battery: Voltage read, SOH calculation, charge current
- USB: Connector detection, power delivery, enumeration
- IMU: Sensor presence, calibration verification, axis ranges
- NFC: Field detection, tag reading, protocol compliance

```python
generator = HILTestGenerator("device_spec.json")
suite = generator.generate_all_tests()
generator.save_pytest_file("test_hil_device.py")

# Summary
summary = generator.get_test_summary()
# → {"device": "WiFi Beacon v2.0", "total_tests": 18, "components": 6}
```

---

### Phase 6: Quality & Observability (phases/phase6_observability/)

#### 6a. Edge-Vision AOI (`edge_vision_aoi.py`)
**Real-time glue dot inspection on PCB assembly line.**

- **Input:** PCB image from line camera
- **Process:**
  - HSV color thresholding for red glue detection (H: 0-10° or 160-180°)
  - Morphological operations (close + open) for cleanup
  - cv2.findContours() with area filtering
  - Tolerance checking (±15% by default)
  - Proportional PLC feedback: 2% error = ±1 PSI
- **Output:** Pass/fail, defect count, pressure adjustment

```python
aoi = EdgeVisionAOI(
    target_glue_area_mm2=18.0,
    tolerance_pct=15.0,
    pixels_per_mm=10.0,
)

result = aoi.inspect_pcb("pcb_image.png", pcb_id="PCB-001")
# → AOIResult(
#     pass_fail=True,
#     defect_count=0,
#     pressure_adjustment_psi=2,  # Increase pressure by 2 PSI
#     saved_this_board=True,
# )

# Retrieve yield events for financial calculation
yield_events = aoi.get_yield_events(saved_only=True)
# → [{"pcb_id": "PCB-001", "area_mm2": 18.2, "passed": True}, ...]

# Fleet statistics for dashboard
stats = aoi.get_statistics()
# → {"total_inspected": 10000, "pass_rate_pct": 98.5, "saved_yield_events": 150}
```

#### 6b. MQTT Telemetry Listener (`mqtt_telemetry_listener.py`)
**Field device monitoring and predictive RMA.**

- **Listens:** `device/+/telemetry` MQTT topic
- **Metrics:** Battery health (%), operating temperature (°C), error codes, heartbeat
- **Analysis:** Replacement Rule Engine → RMA risk score (0.0-1.0)
- **Triggers:** Predictive replacement recommendations

**Replacement Rules:**
- Battery health < 40% SOH → +0.4 risk
- Temperature > 75°C → +0.3 risk
- Error rate > 10/day → +0.3 risk
- **Recommendation:** RMA score ≥ 0.75 OR battery < 25% OR temp > 90°C

```python
listener = MQTTTelemetryListener(
    broker_host="mqtt.local",
    broker_port=1883,
)

# Simulated incoming telemetry
listener.handle_telemetry("DEV-001", {
    "battery_health_pct": 35.0,
    "operating_temp_c": 78.5,
    "error_codes": [0xE001, 0xE002],
    "firmware_version": "2.1.3",
})

# Query device status
status = listener.get_device_status("DEV-001")
# → DeviceHealthStatus(
#     battery_health_pct=35.0,
#     operating_temp_c=78.5,
#     error_count=2,
#     rma_risk_score=0.65,
#     recommended_replacement=False,
# )

# Get devices at risk
at_risk = listener.get_devices_at_risk()
# → [DeviceHealthStatus(...), ...]

# Fleet statistics
fleet_stats = listener.get_fleet_statistics()
# → {
#   "total_devices": 250,
#   "avg_battery_health_pct": 87.3,
#   "devices_at_risk": 5,
#   "devices_at_risk_pct": 2.0,
# }

# Generate RMA report
rma_report = listener.generate_rma_report()
```

---

## Integration: Phase Orchestrator

The `PhaseOrchestrator` class coordinates all 6 phases:

```python
from phases.phase_orchestrator import PhaseOrchestrator

orchestrator = PhaseOrchestrator()

run = orchestrator.execute_full_pipeline(
    run_id="ORCH-20260629",
    design_id="DESIGN-WIFI-BEACON-V2",
    market_size=50000,
    quantity=10000,
    use_cloud_sim=False,
)

# Results
print(run.status)  # "complete"
print(run.total_duration_ms)  # Total execution time

for phase_name, result in run.phases.items():
    print(f"{result.phase_name}: {result.status.value}")
```

### Error Isolation
Each phase runs independently. If one fails:
1. Failure is logged and returned in `PhaseExecutionResult`
2. Downstream phases continue (error doesn't cascade)
3. Results are aggregated for RCA

---

## Database & Persistence

SQLite databases track:
- `aoi_results.db` - Glue dot inspection events, yield tracking
- `telemetry.db` - Device health snapshots, RMA predictions
- `manufacturing.db` - Build jobs, design packages, vendor orders

---

## Quick Start

### 1. Test Complete Pipeline
```bash
python test_phase_orchestration.py
```

### 2. Run Individual Phases
```python
# Phase 1: Demand
from phases.phase1_demand import DemandService
demand_svc = DemandService()
forecast = demand_svc.forecast(market_units=50000)

# Phase 4: PINN Thermal
from phases.phase4_digital_twin.pinn_solver import PINNThermalSolver
pinn = PINNThermalSolver()
result = pinn.solve(thermal_input)

# Phase 6: Edge-Vision AOI
from phases.phase6_observability.edge_vision_aoi import EdgeVisionAOI
aoi = EdgeVisionAOI()
result = aoi.inspect_pcb("image.png", "PCB-001")
```

### 3. Generate HIL Tests
```python
from phases.phase6_observability.hil_test_generator import HILTestGenerator
generator = HILTestGenerator("device_spec.json")
generator.generate_all_tests()
generator.save_pytest_file("test_device.py")
```

### 4. Monitor Fleet Health
```python
from phases.phase6_observability.mqtt_telemetry_listener import MQTTTelemetryListener
listener = MQTTTelemetryListener()
rma_report = listener.generate_rma_report()
```

---

## Environment Variables

Configure via `.env`:

```bash
# Phase 1: Demand
PECAN_API_KEY=your_key
PECAN_BASE_URL=https://api.pecan.ai

# Phase 2: Sourcing
NEXAR_API_KEY=your_key

# Phase 3: Simulation
ANSYS_API_KEY=your_key
ANSYS_BASE_URL=https://api.ansys.com/workbench/v1

# Phase 4-6: Mock mode
ATHENA_DRY_RUN=true  # All phases use mock/simulation

# Observability
MQTT_BROKER_HOST=mqtt.local
MQTT_BROKER_PORT=1883
```

---

## Metrics & Dashboard Integration

All phases export metrics for Streamlit dashboard:

```python
# Demand
demand_forecast.to_dict()  # → {"demand_units": 55000, ...}

# AOI Yield
aoi.get_statistics()  # → {"pass_rate_pct": 98.5, "saved_yield_events": 150, ...}

# Telemetry RMA
listener.get_fleet_statistics()  # → {"devices_at_risk": 5, ...}

# Phase orchestration
run.to_dict()  # → {"phase_results": [...], "total_duration_ms": 123.45}
```

---

## Architecture Principles

1. **Mock-First**: All systems work without external APIs
2. **Error Isolation**: Phase failures don't cascade
3. **Forward Feeding**: Each phase uses previous results
4. **Observability**: All transitions logged to SQLite
5. **Modularity**: Phases are independent; swap implementations
6. **Testability**: Complete pytest HIL generation pipeline

---

## File Structure

```
phases/
├── phase1_demand/           # Demand forecasting
├── phase2_sourcing/         # Supply chain validation
├── phase3_simulation/       # (Virtual in Athena)
├── phase4_digital_twin/     # PINN + Cloud simulation
│   ├── pinn_solver.py
│   ├── cloud_simulator.py
│   └── __init__.py
├── phase5_execution/        # Manufacturing orchestration
│   ├── manufacturing_orchestrator.py
│   └── __init__.py
├── phase6_observability/    # Quality, monitoring, financial
│   ├── edge_vision_aoi.py
│   ├── hil_test_generator.py
│   ├── mqtt_telemetry_listener.py
│   ├── example_device_spec.json
│   └── __init__.py
├── phase_orchestrator.py    # Unified coordinator
└── __init__.py

test_phase_orchestration.py   # Integration test
PHASES_ARCHITECTURE.md        # This file
```

---

## Next Steps

1. **Integrate with Athena:** Update `athena/orchestrator.py` to use `PhaseOrchestrator`
2. **Connect RAG**: Feed Phase results to knowledge base for RCA
3. **Real Hardware**: Replace mock implementations with actual APIs
4. **Dashboard**: Display all 6 phases in Streamlit with real-time metrics
5. **Monetization**: Link Phase 6 metrics to RaaS/YaaS revenue calculations

---

## References

- **Project Athena GUIDE**: `ATHENA_GUIDE.md`
- **Complete Platform**: `ATHENA_COMPLETE_PLATFORM.md`
- **Example Device Spec**: `phases/phase6_observability/example_device_spec.json`

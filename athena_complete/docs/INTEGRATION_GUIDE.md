# Project Athena — Integration Guide

Complete guide for connecting the 7 microservices via event streams and establishing end-to-end data flows.

---

## Event-Driven Architecture

The services communicate through **Redpanda/Kafka** event topics. Each service publishes domain events independently; consumers react to those events without blocking.

### Event Topics

| Topic | Produced By | Consumed By | Retention | Schema |
|-------|-------------|-------------|-----------|--------|
| `demand.updated` | Node 1 | Node 3 | 30 days | SKU, forecasts, confidence |
| `gate.status_changed` | Node 2 | Node 3, Node 4 | 90 days | gate_id, design_id, stage, status |
| `kitting.released` | Node 3 | Node 5 | 14 days | work_order_id, components |
| `quality.spc_alert` | Node 5 | Node 2 | 30 days | characteristic, cpk, out_of_control |
| `fleet.capa_triggered` | Node 6 | Node 2 | 90 days | cluster_id, failure_pattern, devices |

---

## Integration Points

### 1. Demand Flow (Node 1 → Node 3)

**Trigger**: User requests demand forecast for SKU

```
┌─────────────┐
│   Gateway   │ POST /n1/demand/forecast
└──────┬──────┘
       │
       ▼
┌──────────────────┐
│  Node 1 (8001)   │ ──┬─→ Calculate forecast
│  Demand Sensing  │   │
└──────────────────┘   └─→ [Publish demand.updated]
       │
       │ demand.updated event
       │  {sku, forecasts, confidence}
       ▼
   [Kafka Topic]
       │
       ▼
┌──────────────────┐
│  Node 3 (8003)   │ ──→ Update inventory targets
│  Supply Chain    │    Adjust lead times
└──────────────────┘    Pre-stage supplier orders
```

**Implementation**:

In `services/node1/main.py`, after forecast calculation:

```python
# Publish demand.updated event
async def publish_demand_event(sku: str, forecasts: dict):
    from confluent_kafka import Producer
    
    producer = Producer({
        'bootstrap.servers': os.getenv("ATHENA_KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
    })
    
    event = {
        "sku": sku,
        "forecasts": forecasts,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    producer.produce(
        topic="demand.updated",
        value=json.dumps(event).encode('utf-8'),
        key=sku.encode('utf-8'),
    )
    producer.flush()
```

In `services/node3/main.py`, add Kafka consumer:

```python
from confluent_kafka import Consumer

async def consume_demand_updates():
    consumer = Consumer({
        'bootstrap.servers': os.getenv("ATHENA_KAFKA_BOOTSTRAP_SERVERS"),
        'group.id': 'node3-demand',
        'auto.offset.reset': 'earliest',
    })
    
    consumer.subscribe(['demand.updated'])
    
    while True:
        msg = consumer.poll(timeout=10)
        if msg:
            event = json.loads(msg.value().decode('utf-8'))
            sku = event['sku']
            
            # Update inventory targets based on new forecast
            update_inventory_targets(sku, event['forecasts'])
            
            logger.info(f"Updated inventory for {sku} based on demand.updated")
```

---

### 2. PLM Gate Flow (Node 2 ↔ Node 3, Node 4)

**Trigger**: Engineer evaluates design gate

```
┌─────────────┐
│   Gateway   │ POST /n2/gates/gate-001/evaluate
└──────┬──────┘
       │
       ▼
┌──────────────────┐
│  Node 2 (8002)   │ ──┬─→ Validate gate (PASS/FAIL/HOLD)
│  PLM & Gates     │   │   Consume quality.spc_alert if triggered
└──────────────────┘   └─→ [Publish gate.status_changed]
       │
       │ gate.status_changed event
       │  {gate_id, design_id, stage, status}
       ▼
   [Kafka Topic]
       ├─→ Node 3 (update kitting)
       └─→ Node 4 (schedule thermal sim)
```

**Implementation**:

In `services/node2/main.py`:

```python
@app.post("/api/v1/gates/{gate_id}/evaluate")
async def evaluate_gate(gate_id: str, body: GateEvaluation):
    # Store evaluation
    gates_store[gate_id] = { ... }
    
    # Publish gate.status_changed
    from confluent_kafka import Producer
    producer = Producer({
        'bootstrap.servers': os.getenv("ATHENA_KAFKA_BOOTSTRAP_SERVERS")
    })
    
    event = {
        "gate_id": gate_id,
        "design_id": body.design_id,
        "stage": body.stage.value,
        "status": body.status.value,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    producer.produce(
        topic="gate.status_changed",
        value=json.dumps(event).encode('utf-8'),
    )
    producer.flush()
    
    return {"gate_id": gate_id, "status": body.status.value}
```

In `services/node3/main.py`, consume and update kitting:

```python
async def consume_gate_events():
    consumer = Consumer({
        'bootstrap.servers': os.getenv("ATHENA_KAFKA_BOOTSTRAP_SERVERS"),
        'group.id': 'node3-gates',
        'auto.offset.reset': 'earliest',
    })
    
    consumer.subscribe(['gate.status_changed'])
    
    while True:
        msg = consumer.poll(timeout=10)
        if msg:
            event = json.loads(msg.value().decode('utf-8'))
            
            if event['status'] == 'pass':
                # Release kitting for manufacturing
                create_work_order(event['design_id'])
                logger.info(f"Released kitting for {event['design_id']}")
```

In `services/node4/main.py`, consume and schedule thermal simulation:

```python
async def consume_gate_events():
    consumer = Consumer({ ... })
    consumer.subscribe(['gate.status_changed'])
    
    while True:
        msg = consumer.poll(timeout=10)
        if msg:
            event = json.loads(msg.value().decode('utf-8'))
            
            if event['stage'] == 'validation':
                # Schedule thermal simulation during validation gate
                logger.info(f"Scheduling thermal sim for {event['design_id']}")
                # Trigger async simulation job
```

---

### 3. SPC Alert Flow (Node 5 → Node 2)

**Trigger**: Quality system detects out-of-control SPC

```
┌──────────────────┐
│  Node 5 (8005)   │ ──┬─→ Monitor SPC charts
│  Quality & AOI   │   │   Detect trend/violation
└──────────────────┘   └─→ [Publish quality.spc_alert]
       │
       │ quality.spc_alert event
       │  {characteristic, cpk, alert_type}
       ▼
   [Kafka Topic]
       │
       ▼
┌──────────────────┐
│  Node 2 (8002)   │ ──→ Trigger design gate re-evaluation
│  PLM & Gates     │    Propose CAPA if necessary
└──────────────────┘    Notify engineering team
```

**Implementation**:

In `services/node5/main.py`:

```python
@app.post("/api/v1/quality/spc-alert")
async def trigger_spc_alert(body: SPCAlert):
    from confluent_kafka import Producer
    producer = Producer({
        'bootstrap.servers': os.getenv("ATHENA_KAFKA_BOOTSTRAP_SERVERS")
    })
    
    event = {
        "characteristic": body.characteristic,
        "value": body.value,
        "cpk": body.cpk,
        "alert_type": body.alert_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    producer.produce(
        topic="quality.spc_alert",
        value=json.dumps(event).encode('utf-8'),
    )
    producer.flush()
    
    return {"status": "alert_recorded"}
```

In `services/node2/main.py`, consume and re-evaluate:

```python
async def consume_spc_alerts():
    consumer = Consumer({
        'bootstrap.servers': os.getenv("ATHENA_KAFKA_BOOTSTRAP_SERVERS"),
        'group.id': 'node2-quality',
        'auto.offset.reset': 'latest',
    })
    
    consumer.subscribe(['quality.spc_alert'])
    
    while True:
        msg = consumer.poll(timeout=10)
        if msg:
            alert = json.loads(msg.value().decode('utf-8'))
            
            if alert['cpk'] < 1.33:  # Out of control
                logger.warning(f"SPC Alert: {alert['characteristic']} cpk={alert['cpk']}")
                
                # Trigger design gate hold
                gate_status = "HOLD"
                create_capa_proposal(
                    issue=f"SPC {alert['characteristic']} cpk={alert['cpk']}",
                    proposed_action="Adjust process parameters"
                )
```

---

### 4. CAPA & Fleet Failure Flow (Node 6 → Node 2)

**Trigger**: Fleet cluster detects anomaly pattern

```
┌──────────────────┐
│  Node 6 (8006)   │ ──┬─→ Analyze field failures
│  Fleet Twin      │   │   Cluster by pattern (K-means)
│  & RMA           │   └─→ [Publish fleet.capa_triggered]
└──────────────────┘
       │
       │ fleet.capa_triggered event
       │  {cluster_id, failure_pattern, devices}
       ▼
   [Kafka Topic]
       │
       ▼
┌──────────────────┐
│  Node 2 (8002)   │ ──→ Create CAPA from cluster
│  PLM & Gates     │    (Can re-rate FMEA — privilege held)
└──────────────────┘    Link to design gate
```

**Important**: Node 6 **cannot** apply FMEA re-ratings. Only Node 2 (fmea_expert role) can do that.

**Implementation**:

In `services/node6/main.py`:

```python
@app.post("/api/v1/fleet/capa-proposal")
async def create_capa_from_field(body: dict):
    from confluent_kafka import Producer
    producer = Producer({
        'bootstrap.servers': os.getenv("ATHENA_KAFKA_BOOTSTRAP_SERVERS")
    })
    
    cluster_id = body.get("cluster_id")
    
    # Node 6 publishes the event; Node 2 processes it
    event = {
        "cluster_id": cluster_id,
        "failure_pattern": body.get("failure_pattern"),
        "devices": body.get("devices", []),
        "evidence": body.get("evidence", {}),
        "proposed_by": "fleet_twin",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    producer.produce(
        topic="fleet.capa_triggered",
        value=json.dumps(event).encode('utf-8'),
        key=cluster_id.encode('utf-8'),
    )
    producer.flush()
    
    return {
        "capa_id": f"CAPA-{cluster_id}",
        "status": "proposed"
    }
```

In `services/node2/main.py`, consume and handle:

```python
async def consume_fleet_capa():
    consumer = Consumer({
        'bootstrap.servers': os.getenv("ATHENA_KAFKA_BOOTSTRAP_SERVERS"),
        'group.id': 'node2-fleet',
        'auto.offset.reset': 'latest',
    })
    
    consumer.subscribe(['fleet.capa_triggered'])
    
    while True:
        msg = consumer.poll(timeout=10)
        if msg:
            event = json.loads(msg.value().decode('utf-8'))
            
            logger.info(f"Fleet CAPA: {event['failure_pattern']}")
            
            # Create CAPA
            capa = create_capa(
                issue=event['failure_pattern'],
                evidence=event['devices'],
                proposed_action="TBD"
            )
            
            # Re-rate FMEA rows (ONLY Node 2 can do this)
            # This is where the privilege boundary is enforced
            if event['failure_pattern'] == "Battery degradation":
                re_rate_fmea_row(
                    design_id="design-001",
                    row_id="F001",
                    new_rpn=120  # Update based on field evidence
                )
```

---

### 5. Kitting & Production Flow (Node 3 → Node 5)

**Trigger**: Work order released for manufacturing

```
┌──────────────────┐
│  Node 3 (8003)   │ ──┬─→ Check part availability
│  Supply Chain    │   │   Reserve supplier lots
└──────────────────┘   └─→ [Publish kitting.released]
       │
       │ kitting.released event
       │  {work_order_id, components, ioc_results}
       ▼
   [Kafka Topic]
       │
       ▼
┌──────────────────┐
│  Node 5 (8005)   │ ──→ Configure AOI & recipe
│  Quality & AOI   │    Load component BOM
└──────────────────┘    Start inspection procedures
```

**Implementation**:

In `services/node3/main.py`:

```python
@app.post("/api/v1/work-orders")
async def create_work_order(body: WorkOrder):
    from confluent_kafka import Producer
    producer = Producer({
        'bootstrap.servers': os.getenv("ATHENA_KAFKA_BOOTSTRAP_SERVERS")
    })
    
    # Create work order
    work_orders[body.work_order_id] = { ... }
    
    # Publish kitting.released
    event = {
        "work_order_id": body.work_order_id,
        "design_id": body.design_id,
        "components": body.components,
        "status": "released",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    
    producer.produce(
        topic="kitting.released",
        value=json.dumps(event).encode('utf-8'),
    )
    producer.flush()
    
    return {"work_order_id": body.work_order_id, "status": "created"}
```

In `services/node5/main.py`, consume and configure production:

```python
async def consume_kitting_events():
    consumer = Consumer({
        'bootstrap.servers': os.getenv("ATHENA_KAFKA_BOOTSTRAP_SERVERS"),
        'group.id': 'node5-kitting',
        'auto.offset.reset': 'latest',
    })
    
    consumer.subscribe(['kitting.released'])
    
    while True:
        msg = consumer.poll(timeout=10)
        if msg:
            event = json.loads(msg.value().decode('utf-8'))
            
            logger.info(f"Kitting released: {event['work_order_id']}")
            
            # Configure AOI for these components
            configure_aoi_for_bom(event['components'])
            
            # Load/activate recipe
            activate_recipe_for_design(event['design_id'])
```

---

## Telemetry Ingestion Flow

### Node 6 Telemetry Ingestion Pipeline

```
┌──────────────────┐
│   Field Devices  │ (WiFi Beacons, sensors)
│   124,500 active │
└────────┬─────────┘
         │ MQTT/HTTP
         ▼
   ┌──────────────────────┐
   │  Ingestion Service   │ ──→ Parse telemetry
   │  (8007)              │    Validate schema
   └──────┬───────────────┘    Rate-limit
          │
          ▼
   ┌──────────────────────┐
   │  Direct to Node 6    │ POST /api/v1/fleet/telemetry
   │  (no operator queue) │ ──→ Update device health
   └──────────────────────┘    Evaluate RMA risk
          │                     Cluster anomalies
          ▼                     Publish fleet.capa_triggered
   [TimescaleDB Hypertable]
          │
          ├─→ Device health snapshots
          ├─→ Time-series analytics
          └─→ Predictive RMA scoring
```

**Implementation**:

In `services/ingestion/main.py`:

```python
@app.post("/ingest/fleet/telemetry")
async def ingest_fleet_telemetry(body: List[dict]):
    import httpx
    
    # Parse and validate
    parsed_devices = []
    for telemetry in body:
        try:
            parsed = TelemetryParser.parse(telemetry)
            parsed_devices.append(parsed)
        except ValueError as e:
            logger.warning(f"Telemetry parse error: {str(e)}")
    
    # Forward to Node 6
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "http://node6:8006/api/v1/fleet/telemetry",
            json=parsed_devices,
            timeout=30.0
        )
    
    return {"status": "ingested", "device_count": len(parsed_devices)}
```

---

## Startup Sequence

1. **Infrastructure** (30s)
   - PostgreSQL initializes & creates databases
   - Redis starts
   - Redpanda broker starts & advertises

2. **Gateway** (5s)
   - Starts on 8000
   - Waits for infrastructure
   - Health check passes

3. **Nodes** (10s each)
   - Node 1-6 start in parallel
   - Connect to infrastructure
   - Health checks pass

4. **Consumers** (startup)
   - Each node spawns async Kafka consumer tasks
   - Subscribe to relevant topics
   - Ready to process events

**Verification**:

```bash
# All infrastructure ready
docker compose ps

# Gateway can reach all nodes
curl http://localhost:8000/health | jq '.services'

# Kafka topics exist (or auto-create on first event)
docker compose exec redpanda kafka-topics.sh --list --bootstrap-server localhost:9092
```

---

## Testing Event Flows

### Test 1: Complete Demand → Kitting → QA Flow

```bash
# 1. Login
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "p.nair", "password": "p.nair-demo"}' | jq -r '.access_token')

# 2. Forecast demand (Node 1)
curl -X POST http://localhost:8000/n1/api/v1/demand/forecast \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sku": "nRF52840", "historical_weeks": 12}'

# Monitor Kafka for demand.updated event
# docker compose exec redpanda kafka-console-consumer.sh \
#   --topic demand.updated --from-beginning --bootstrap-server localhost:9092

# 3. Verify Node 3 received and updated
curl http://localhost:8000/n3/api/v1/parts?pn=nRF52840 \
  -H "Authorization: Bearer $TOKEN" | jq

# 4. Evaluate design gate (Node 2)
curl -X POST http://localhost:8000/n2/api/v1/gates/gate-001/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "design_id": "design-001",
    "stage": "validation",
    "status": "pass",
    "evaluator": "p.nair",
    "evidence": {"test_results": "passed"}
  }'

# Monitor Kafka for gate.status_changed
# docker compose exec redpanda kafka-console-consumer.sh \
#   --topic gate.status_changed --from-beginning --bootstrap-server localhost:9092

# 5. Verify Node 3 released kitting
curl http://localhost:8000/n3/api/v1/work-orders/WO-001 \
  -H "Authorization: Bearer $TOKEN"

# 6. Verify Node 5 is ready
curl http://localhost:8000/n5/api/v1/quality/recipe \
  -H "Authorization: Bearer $TOKEN" | jq
```

### Test 2: SPC Alert → CAPA Flow

```bash
# 1. Trigger SPC alert (Node 5)
curl -X POST http://localhost:8000/n5/api/v1/quality/spc-alert \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "characteristic": "reflow_peak_temp",
    "value": 248,
    "ucl": 250,
    "lcl": 240,
    "cpk": 1.2,
    "alert_type": "TREND"
  }'

# 2. Verify Node 2 created CAPA
curl http://localhost:8000/n2/api/v1/capa \
  -H "Authorization: Bearer $TOKEN" | jq '.capa_proposals | length'
```

---

## Monitoring Event Flow

### Kafka Consumer Groups

```bash
# View all consumer groups
docker compose exec redpanda kafka-consumer-groups.sh \
  --list --bootstrap-server localhost:9092

# View lag for a group
docker compose exec redpanda kafka-consumer-groups.sh \
  --group node3-demand \
  --describe \
  --bootstrap-server localhost:9092
```

### Event Tracing

```bash
# Follow all events across all topics
docker compose exec redpanda kafka-console-consumer.sh \
  --topics 'demand.updated,gate.status_changed,kitting.released,quality.spc_alert,fleet.capa_triggered' \
  --from-beginning \
  --bootstrap-server localhost:9092 | jq .
```

---

## Production Considerations

1. **Event Ordering**: Use partition keys (sku, design_id, etc) to maintain order per key
2. **Dead Letter Queues**: Send parse failures to `<topic>-dlq` for manual review
3. **Event Versioning**: Include `schema_version` in each event for backward compatibility
4. **Idempotency**: Consumers should be idempotent (handle duplicate messages)
5. **Monitoring**: Alert on consumer lag > 60s, event processing errors

---

## Next Steps

1. Implement Kafka producer/consumer loops in each service
2. Create database schemas & migrations for each node
3. Build React console with event subscription (WebSocket)
4. Add Celery workers for long-running tasks (model retraining, RMA analysis)
5. Implement monitoring & alerting (Prometheus, Grafana)
6. Create automated tests for event flows

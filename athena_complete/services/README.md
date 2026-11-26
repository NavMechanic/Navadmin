# Project Athena — Microservices Implementation

Seven-service microservices architecture for enterprise manufacturing orchestration.

## Architecture

```
┌─ API Gateway (8000) ────────────────────────────────────────────┐
│  • Console serving (React)                                        │
│  • Identity & auth (JWT + refresh tokens)                        │
│  • Reverse proxy to nodes                                        │
│  • Circuit breaker pattern                                       │
│  • Rate limiting & health checks                                 │
└─────────────────────────────────────────────────────────────────┘
    ↓            ↓            ↓            ↓            ↓            ↓
┌────────┐  ┌────────┐  ┌────────┐  ┌─────────┐  ┌────────┐  ┌────────┐
│ Node 1 │  │ Node 2 │  │ Node 3 │  │ Node 4  │  │ Node 5 │  │ Node 6 │
│ Demand │  │  PLM   │  │ Supply │  │ Thermal │  │Quality │  │ Fleet  │
│ 8001   │  │ 8002   │  │ 8003   │  │ 8004    │  │ 8005   │  │ 8006   │
└────────┘  └────────┘  └────────┘  └─────────┘  └────────┘  └────────┘

┌──────────────────┐
│  Ingestion (8007)│
│  • PDF parsing   │
│  • CAD parsing   │
│  • Telemetry    │
└──────────────────┘

Infrastructure:
  • PostgreSQL (TimescaleDB) — Node databases
  • Redis — Caching, rate limiting
  • Redpanda/Kafka — Event pub/sub
```

## Quick Start

### 1. Clone & Setup

```bash
cd services
cp .env.example .env
```

### 2. Docker Compose (Recommended)

```bash
# Build all services
docker compose build

# Start all services
docker compose up -d

# View logs
docker compose logs -f gateway

# Stop all services
docker compose down
```

### 3. Verify Installation

```bash
# Gateway health
curl -s http://localhost:8000/health | jq

# Login
TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "p.nair", "password": "p.nair-demo"}' | jq -r '.access_token')

# Query Node 1 (demand)
curl -s http://localhost:8000/n1/api/v1/demand/forecast \
  -H "Authorization: Bearer $TOKEN" | jq

# Query Node 2 (PLM gates)
curl -s http://localhost:8000/n2/api/v1/gates \
  -H "Authorization: Bearer $TOKEN" | jq
```

## Services Overview

### Node 1: Demand Sensing (Port 8001)

AI-driven demand forecasting.

```bash
# Forecast demand for SKU
curl -X POST http://localhost:8000/n1/api/v1/demand/forecast \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"sku": "nRF52840", "historical_weeks": 12}'

# Get forecast
curl http://localhost:8000/n1/api/v1/demand/forecast?sku=nRF52840 \
  -H "Authorization: Bearer $TOKEN" | jq
```

### Node 2: Micro-PLM & Stage-Gate (Port 8002)

Product lifecycle management with governance gates.

```bash
# List gates
curl http://localhost:8000/n2/api/v1/gates \
  -H "Authorization: Bearer $TOKEN" | jq

# Evaluate gate
curl -X POST http://localhost:8000/n2/api/v1/gates/gate-001/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "design_id": "design-001",
    "stage": "validation",
    "status": "pass",
    "evaluator": "p.nair",
    "evidence": {"test_results": "passed"}
  }' | jq

# Get BOM
curl http://localhost:8000/n2/api/v1/designs/design-001/bom \
  -H "Authorization: Bearer $TOKEN" | jq

# Get FMEA
curl http://localhost:8000/n2/api/v1/fmea/design-001 \
  -H "Authorization: Bearer $TOKEN" | jq
```

### Node 3: Supply Chain (Port 8003)

Part availability and supply chain optimization.

```bash
# Query parts
curl 'http://localhost:8000/n3/api/v1/parts?pn=nRF52840' \
  -H "Authorization: Bearer $TOKEN" | jq

# Get alternates
curl http://localhost:8000/n3/api/v1/parts/nRF52840/alternates \
  -H "Authorization: Bearer $TOKEN" | jq

# Calculate landed cost
curl 'http://localhost:8000/n3/api/v1/landed-cost?design_id=design-001' \
  -H "Authorization: Bearer $TOKEN" | jq
```

### Node 4: Physics-Informed Digital Twin (Port 8004)

Thermal, stress, vibration validation (PINN solver <1ms).

```bash
# Run thermal simulation
curl -X POST http://localhost:8000/n4/api/v1/thermal/simulate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"design_id": "design-001", "power_w": 5.0}' | jq

# Get thermal cases
curl http://localhost:8000/n4/api/v1/thermal/cases/design-001 \
  -H "Authorization: Bearer $TOKEN" | jq

# Run stress simulation
curl -X POST http://localhost:8000/n4/api/v1/stress/simulate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"design_id": "design-001", "load_n": 100}' | jq
```

### Node 5: Quality & Machine Vision (Port 8005)

AOI inspection, SPC, recipe management.

```bash
# Inspect board
curl -X POST http://localhost:8000/n5/api/v1/quality/board \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"board_id": "PCB-001"}' | jq

# Get SPC data
curl http://localhost:8000/n5/api/v1/quality/spc \
  -H "Authorization: Bearer $TOKEN" | jq

# Get recipes
curl http://localhost:8000/n5/api/v1/quality/recipe \
  -H "Authorization: Bearer $TOKEN" | jq
```

### Node 6: Fleet Twin & After-Sales (Port 8006)

Field device monitoring, predictive RMA.

```bash
# Ingest device telemetry
curl -X POST http://localhost:8000/ingest/fleet/telemetry \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '[{
    "device_id": "DEV-000001",
    "battery_health_pct": 85,
    "operating_temp_c": 35,
    "firmware_version": "2.1.0"
  }]' | jq

# Get device status
curl http://localhost:8000/n6/api/v1/fleet/device/DEV-000001 \
  -H "Authorization: Bearer $TOKEN" | jq

# Detect anomalies
curl http://localhost:8000/n6/api/v1/fleet/anomalies \
  -H "Authorization: Bearer $TOKEN" | jq

# Forecast RMA
curl http://localhost:8000/n6/api/v1/fleet/rma-forecast \
  -H "Authorization: Bearer $TOKEN" | jq
```

### Ingestion Service (Port 8007)

PDF, CAD, telemetry parsing with operator review queue.

```bash
# Upload PDF
curl -X POST http://localhost:8000/ingest/pdf \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@datasheet.pdf" | jq

# Upload CAD
curl -X POST http://localhost:8000/ingest/cad \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@design.step" | jq

# List queue
curl http://localhost:8000/ingest/queue \
  -H "Authorization: Bearer $TOKEN" | jq

# Approve ingestion
curl -X POST http://localhost:8000/ingest/queue/ing_abc12345/approve \
  -H "Authorization: Bearer $TOKEN" | jq
```

## Demo Credentials

```
Username: p.nair       | Password: p.nair-demo        | Role: design_engineer
Username: s.khan       | Password: s.khan-demo        | Role: fmea_expert
Username: line.operator| Password: operator-demo      | Role: read_only
```

## Environment Configuration

See `.env.example` for all configuration options:

- **ATHENA_LOG_LEVEL**: INFO, DEBUG
- **ATHENA_REQUIRE_AUTH**: true/false
- **ATHENA_JSON_LOGS**: true/false for structured logging
- **ATHENA_BREAKER_THRESHOLD**: Failures before circuit breaker opens (default: 5)
- **ATHENA_BREAKER_COOLDOWN**: Seconds before half-open attempt (default: 20)
- **ATHENA_TIMESCALE_REQUIRED**: Enable if using TimescaleDB for Node 6

## Database Operations

```bash
# Connect to PostgreSQL
docker compose exec postgres psql -U athena -d athena

# View databases
\l

# Create node database
CREATE DATABASE node2;

# Run migrations (when added)
alembic upgrade head
```

## Kafka Topic Management

```bash
# List topics
docker compose exec redpanda kafka-topics.sh --list \
  --bootstrap-server localhost:9092

# Create demand.updated topic
docker compose exec redpanda kafka-topics.sh --create \
  --topic demand.updated \
  --partitions 3 \
  --replication-factor 1 \
  --bootstrap-server localhost:9092

# Monitor topic
docker compose exec redpanda kafka-console-consumer.sh \
  --topic demand.updated \
  --from-beginning \
  --bootstrap-server localhost:9092
```

## Testing

```bash
# Health check all services
curl -s http://localhost:8000/health | jq '.services'

# Circuit breaker state
curl -s http://localhost:8000/breaker-state | jq

# Metrics
curl -s http://localhost:8000/metrics | head -20
```

## Troubleshooting

### Gateway won't start
```bash
docker compose logs gateway
docker compose exec gateway curl http://localhost:8000/health
```

### Node connection failures
```bash
# Check if node is up
docker compose logs node1

# Test direct connection
curl -s http://node1:8001/health
```

### Database connection errors
```bash
# Verify PostgreSQL is running
docker compose ps postgres

# Test connection
psql -h localhost -U athena -d athena -c "SELECT 1"
```

### Circuit breaker keeps opening
```bash
# Check which nodes are failing
curl -s http://localhost:8000/breaker-state | jq '.breakers'

# Increase timeout for development
ATHENA_GATEWAY_TIMEOUT=120 docker compose up gateway
```

## Performance Tuning

- **Gateway workers**: `UVICORN_WORKERS=8`
- **Connection pool**: `HTTPX_MAX_CONNECTIONS=100`
- **Request timeout**: `ATHENA_GATEWAY_TIMEOUT=120` (for large payloads)

## Production Deployment

See `../DEVELOPMENT_SETUP.md` for:
- Kubernetes manifests
- CI/CD pipeline configuration
- Monitoring & alerting setup
- Backup strategy
- High availability configuration

## Architecture Documentation

- **ATHENA_ENTERPRISE_GUIDE.md** — Complete architecture reference
- **NODE_API_SPECIFICATIONS.md** — Full API endpoint documentation
- **DEVELOPMENT_SETUP.md** — Setup and operational guide

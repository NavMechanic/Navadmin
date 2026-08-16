# Project Athena — Startup Guide

Complete walkthrough for launching the enterprise manufacturing platform locally or in production.

---

## Prerequisites

### System Requirements
- **Docker & Docker Compose** (v24.0+)
- **Python 3.11+** (for local development without Docker)
- **Git** (to clone repository)
- **curl** or **Postman** (for API testing)

### Recommended Tools
- **jq** (JSON parsing): `sudo apt-get install jq`
- **psql** (PostgreSQL client): `sudo apt-get install postgresql-client`
- **Kafka CLI** (for topic inspection)

---

## Quick Start (Docker Compose)

### 1. Navigate to Services Directory

```bash
cd services
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` if needed (for production, set `ATHENA_REQUIRE_AUTH=true`).

### 3. Build & Launch

```bash
# Build all images
docker compose build

# Start all services (infrastructure + gateway + 6 nodes + ingestion)
docker compose up -d

# Verify everything is running
docker compose ps
```

### 4. Wait for Startup

Services take ~30-60 seconds to become healthy. Monitor with:

```bash
# Watch logs
docker compose logs -f gateway

# Check health status
curl -s http://localhost:8000/health | jq '.services'
```

Expected output when all services are UP:
```json
{
  "gateway": "ok",
  "services": {
    "n1": {"status": "up", "code": 200},
    "n2": {"status": "up", "code": 200},
    "n3": {"status": "up", "code": 200},
    "n4": {"status": "up", "code": 200},
    "n5": {"status": "up", "code": 200},
    "n6": {"status": "up", "code": 200}
  }
}
```

---

## API Testing Workflow

### 1. Authenticate

```bash
# Login (returns access + refresh tokens)
export TOKEN=$(curl -s -X POST http://localhost:8000/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username": "p.nair", "password": "p.nair-demo"}' | jq -r '.access_token')

echo "Token: $TOKEN"
```

Demo credentials:
| Username | Password | Role |
|----------|----------|------|
| p.nair | p.nair-demo | design_engineer, gate_evaluator |
| s.khan | s.khan-demo | fmea_expert |
| line.operator | operator-demo | read_only |

### 2. Test Node 1 (Demand Sensing)

```bash
# Forecast demand
curl -X POST http://localhost:8000/n1/api/v1/demand/forecast \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "sku": "nRF52840",
    "historical_weeks": 12,
    "include_seasonal": true
  }' | jq

# Get forecast
curl http://localhost:8000/n1/api/v1/demand/forecast?sku=nRF52840 \
  -H "Authorization: Bearer $TOKEN" | jq
```

### 3. Test Node 2 (PLM & Gates)

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
    "evidence": {"test_results": "all_passed"}
  }' | jq

# Get BOM
curl http://localhost:8000/n2/api/v1/designs/design-001/bom \
  -H "Authorization: Bearer $TOKEN" | jq

# Get FMEA
curl http://localhost:8000/n2/api/v1/fmea/design-001 \
  -H "Authorization: Bearer $TOKEN" | jq
```

### 4. Test Node 3 (Supply Chain)

```bash
# Query part availability
curl 'http://localhost:8000/n3/api/v1/parts?pn=nRF52840' \
  -H "Authorization: Bearer $TOKEN" | jq

# Get part alternates
curl http://localhost:8000/n3/api/v1/parts/nRF52840/alternates \
  -H "Authorization: Bearer $TOKEN" | jq

# Calculate landed cost
curl 'http://localhost:8000/n3/api/v1/landed-cost?design_id=design-001' \
  -H "Authorization: Bearer $TOKEN" | jq
```

### 5. Test Node 4 (Thermal Twin)

```bash
# Run thermal simulation (PINN <1ms)
curl -X POST http://localhost:8000/n4/api/v1/thermal/simulate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "design_id": "design-001",
    "power_w": 5.0,
    "ambient_temp_c": 25.0
  }' | jq

# Run stress simulation
curl -X POST http://localhost:8000/n4/api/v1/stress/simulate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "design_id": "design-001",
    "load_n": 100,
    "material": "FR4"
  }' | jq
```

### 6. Test Node 5 (Quality & Vision)

```bash
# Inspect board via AOI
curl -X POST http://localhost:8000/n5/api/v1/quality/board \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"board_id": "PCB-001"}' | jq

# Get SPC data
curl http://localhost:8000/n5/api/v1/quality/spc \
  -H "Authorization: Bearer $TOKEN" | jq

# List recipes
curl http://localhost:8000/n5/api/v1/quality/recipe \
  -H "Authorization: Bearer $TOKEN" | jq
```

### 7. Test Node 6 (Fleet Monitoring)

```bash
# Ingest telemetry
curl -X POST http://localhost:8000/ingest/fleet/telemetry \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '[{
    "device_id": "DEV-000001",
    "battery_health_pct": 85,
    "operating_temp_c": 35,
    "firmware_version": "2.1.0",
    "error_codes": []
  }]' | jq

# Get device status
curl http://localhost:8000/n6/api/v1/fleet/device/DEV-000001 \
  -H "Authorization: Bearer $TOKEN" | jq

# Get devices at risk
curl http://localhost:8000/n6/api/v1/fleet/devices-at-risk \
  -H "Authorization: Bearer $TOKEN" | jq

# Detect anomalies
curl http://localhost:8000/n6/api/v1/fleet/anomalies \
  -H "Authorization: Bearer $TOKEN" | jq

# Forecast RMA
curl http://localhost:8000/n6/api/v1/fleet/rma-forecast \
  -H "Authorization: Bearer $TOKEN" | jq
```

### 8. Test Ingestion Service

```bash
# Upload PDF (create a test file first)
echo "Test PDF content" > test.pdf
curl -X POST http://localhost:8000/ingest/pdf \
  -H "Authorization: Bearer $TOKEN" \
  -F "file=@test.pdf" | jq

# List ingestion queue
curl http://localhost:8000/ingest/queue \
  -H "Authorization: Bearer $TOKEN" | jq

# Approve ingestion
INGESTION_ID=$(curl -s http://localhost:8000/ingest/queue \
  -H "Authorization: Bearer $TOKEN" | jq -r '.items[0].ingestion_id')

curl -X POST http://localhost:8000/ingest/queue/$INGESTION_ID/approve \
  -H "Authorization: Bearer $TOKEN" | jq
```

---

## Monitoring & Debugging

### Health Checks

```bash
# Gateway health + node fan-out
curl -s http://localhost:8000/health | jq

# Circuit breaker state
curl -s http://localhost:8000/breaker-state | jq '.breakers'

# Metrics (Prometheus format)
curl -s http://localhost:8000/metrics
```

### Logs

```bash
# Gateway logs
docker compose logs -f gateway

# Specific node
docker compose logs -f node2

# All logs
docker compose logs -f

# Follow errors only
docker compose logs -f | grep -i error
```

### Database Access

```bash
# Connect to PostgreSQL
docker compose exec postgres psql -U athena -d athena

# View databases
\l

# Connect to node2 database
\c node2

# List tables
\dt

# Example queries
SELECT * FROM designs LIMIT 10;
SELECT COUNT(*) FROM gates;
```

### Kafka/Redpanda

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

### Redis

```bash
# Connect to Redis
docker compose exec redis redis-cli

# Check keys
KEYS *

# View rate-limit state
HGETALL user:p.nair:rate_limit
```

---

## Troubleshooting

### Gateway won't start

```bash
# Check logs
docker compose logs gateway

# Verify port not in use
lsof -i :8000

# Restart
docker compose restart gateway
```

### Node connection failures

```bash
# Check node logs
docker compose logs node1

# Test node directly
curl http://node1:8001/health

# Check circuit breaker
curl http://localhost:8000/breaker-state | jq
```

### Database connection errors

```bash
# Verify PostgreSQL
docker compose exec postgres pg_isready

# Check container logs
docker compose logs postgres

# Connect manually
psql -h localhost -U athena -d athena -c "SELECT 1"
```

### High latency (circuit breaker opening)

```bash
# Increase timeout
ATHENA_GATEWAY_TIMEOUT=120 docker compose up -d gateway

# Check node performance
docker compose stats node1 node2 node3
```

### Out of disk space

```bash
# Clean up Docker
docker system prune -a --volumes

# Remove containers
docker compose down -v

# Rebuild
docker compose up -d
```

---

## Production Deployment

### Pre-deployment Checklist

- [ ] All environment variables configured (`.env`)
- [ ] `ATHENA_REQUIRE_AUTH=true` (not development mode)
- [ ] SSL certificates generated for HTTPS
- [ ] Redis configured for production
- [ ] PostgreSQL with replication (high availability)
- [ ] Kafka topics created with retention policies
- [ ] OPC-UA endpoint configured (if using real PLC)
- [ ] Ansys Cloud credentials configured (if using thermal cloud)

### Kubernetes Deployment

See `services/README.md` for Kubernetes manifests and Helm charts.

### Docker Swarm

```bash
docker stack deploy -c docker-compose.yml athena
```

### Manual Deployment (VMs)

```bash
# On each VM
pip install -r services/requirements.txt

# Terminal 1: Gateway
ATHENA_NODE1_URL=http://node1:8001 ... uvicorn gateway.main:app --port 8000

# Terminal 2-8: Each node
cd services/node1 && python main.py
cd services/node2 && python main.py
...
```

---

## Performance Tuning

### Gateway

```bash
export UVICORN_WORKERS=8              # Parallel workers
export HTTPX_MAX_CONNECTIONS=100      # Connection pool
export ATHENA_GATEWAY_TIMEOUT=120     # Request timeout
```

### Database

```sql
-- Index common queries
CREATE INDEX idx_designs_status ON designs(status);
CREATE INDEX idx_gates_updated ON gates(updated_at DESC);

-- Analyze table stats
ANALYZE designs;
ANALYZE gates;
```

### Redis

```bash
# Increase maxmemory if needed
CONFIG SET maxmemory 1gb
CONFIG SET maxmemory-policy allkeys-lru
```

---

## Stopping & Cleanup

```bash
# Stop all services (keep volumes)
docker compose down

# Stop and remove volumes
docker compose down -v

# Restart specific service
docker compose restart node2
```

---

## Next Steps

1. **Implement Kafka producers/consumers** for event pub/sub between nodes
2. **Build React console** with 7 dashboards
3. **Add database migrations** (Alembic) for each node
4. **Implement Celery workers** for async tasks
5. **Set up CI/CD** (GitHub Actions)
6. **Deploy to Kubernetes** with monitoring

---

## Support & Documentation

- **Architecture**: See `ATHENA_ENTERPRISE_GUIDE.md`
- **API Reference**: See `NODE_API_SPECIFICATIONS.md`
- **Operations**: See `DEVELOPMENT_SETUP.md`
- **Code**: See `services/README.md` for per-service docs

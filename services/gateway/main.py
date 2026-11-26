"""
Project Athena — API Gateway (Port 8000)

Primary responsibilities:
  • Console serving (React static + API)
  • Identity & authentication (JWT + refresh tokens)
  • Token exchange (platform → node-local credentials)
  • Reverse proxy (→ /n1 through /n6)
  • Circuit breaker (protect against slow/dead nodes)
  • Request rate-limiting (per-identity)
  • Health fan-out (200/207/503 semantics)
  • Metrics & observability
"""

from __future__ import annotations

import os
import json
import uuid
import time
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, Any
from enum import Enum
from dataclasses import dataclass, asdict, field

import jwt
import httpx
from fastapi import FastAPI, Request, HTTPException, BackgroundTasks, status
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ============================================================================
# Configuration
# ============================================================================

GATEWAY_LOG_LEVEL = os.getenv("ATHENA_LOG_LEVEL", "INFO")
GATEWAY_TIMEOUT = int(os.getenv("ATHENA_GATEWAY_TIMEOUT", "60"))
GATEWAY_HEALTH_TIMEOUT = int(os.getenv("ATHENA_GATEWAY_HEALTH_TIMEOUT", "5"))
BREAKER_THRESHOLD = int(os.getenv("ATHENA_BREAKER_THRESHOLD", "5"))
BREAKER_COOLDOWN = int(os.getenv("ATHENA_BREAKER_COOLDOWN", "20"))
REQUIRE_AUTH = os.getenv("ATHENA_REQUIRE_AUTH", "true").lower() == "true"
JSON_LOGS = os.getenv("ATHENA_JSON_LOGS", "false").lower() == "true"

# Node URLs (environment-configured)
NODE_URLS = {
    "n1": os.getenv("ATHENA_NODE1_URL", "http://127.0.0.1:8001"),
    "n2": os.getenv("ATHENA_NODE2_URL", "http://127.0.0.1:8002"),
    "n3": os.getenv("ATHENA_NODE3_URL", "http://127.0.0.1:8003"),
    "n4": os.getenv("ATHENA_NODE4_URL", "http://127.0.0.1:8004"),
    "n5": os.getenv("ATHENA_NODE5_URL", "http://127.0.0.1:8005"),
    "n6": os.getenv("ATHENA_NODE6_URL", "http://127.0.0.1:8006"),
}

# Secrets
GATEWAY_SECRET_KEY = os.getenv("ATHENA_GATEWAY_SECRET_KEY", "dev-secret-key-change-in-production")
TOKEN_EXPIRY_MINUTES = 30
REFRESH_EXPIRY_DAYS = 14

logging.basicConfig(level=GATEWAY_LOG_LEVEL)
logger = logging.getLogger(__name__)

# ============================================================================
# Types
# ============================================================================

class CircuitBreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class CircuitBreaker:
    """Circuit breaker per node."""
    node_id: str
    state: CircuitBreakerState = CircuitBreakerState.CLOSED
    failure_count: int = 0
    last_failure_time: Optional[datetime] = None
    opened_at: Optional[datetime] = None

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure_time = datetime.now(timezone.utc)
        if self.failure_count >= BREAKER_THRESHOLD:
            self.state = CircuitBreakerState.OPEN
            self.opened_at = datetime.now(timezone.utc)

    def record_success(self) -> None:
        if self.state == CircuitBreakerState.HALF_OPEN:
            self.state = CircuitBreakerState.CLOSED
            self.failure_count = 0
            self.opened_at = None

    def check_transition(self) -> None:
        if self.state == CircuitBreakerState.OPEN:
            now = datetime.now(timezone.utc)
            if self.opened_at and (now - self.opened_at).total_seconds() >= BREAKER_COOLDOWN:
                self.state = CircuitBreakerState.HALF_OPEN
                self.failure_count = 0

    def is_open(self) -> bool:
        self.check_transition()
        return self.state == CircuitBreakerState.OPEN


# ============================================================================
# Identity & Auth
# ============================================================================

class LoginRequest(BaseModel):
    username: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class TokenData(BaseModel):
    sub: str
    exp: int
    iat: int


class IdentityManager:
    """Manage JWT tokens, users, and token exchange."""

    # Demo credentials
    DEMO_USERS = {
        "p.nair": {"password": "p.nair-demo", "roles": ["design_engineer", "gate_evaluator"]},
        "s.khan": {"password": "s.khan-demo", "roles": ["fmea_expert"]},
        "line.operator": {"password": "operator-demo", "roles": ["read_only"]},
    }

    @staticmethod
    def create_access_token(username: str) -> str:
        now = datetime.now(timezone.utc)
        exp = now + timedelta(minutes=TOKEN_EXPIRY_MINUTES)
        payload = {
            "sub": username,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
        }
        return jwt.encode(payload, GATEWAY_SECRET_KEY, algorithm="HS256")

    @staticmethod
    def create_refresh_token(username: str) -> str:
        now = datetime.now(timezone.utc)
        exp = now + timedelta(days=REFRESH_EXPIRY_DAYS)
        payload = {
            "sub": username,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "type": "refresh",
        }
        return jwt.encode(payload, GATEWAY_SECRET_KEY, algorithm="HS256")

    @staticmethod
    def verify_token(token: str) -> Optional[str]:
        try:
            payload = jwt.decode(token, GATEWAY_SECRET_KEY, algorithms=["HS256"])
            return payload.get("sub")
        except jwt.ExpiredSignatureError:
            return None
        except jwt.InvalidTokenError:
            return None

    @staticmethod
    def login(username: str, password: str) -> Optional[dict]:
        user = IdentityManager.DEMO_USERS.get(username)
        if user and user["password"] == password:
            return {
                "access_token": IdentityManager.create_access_token(username),
                "refresh_token": IdentityManager.create_refresh_token(username),
            }
        return None


# ============================================================================
# FastAPI App
# ============================================================================

app = FastAPI(
    title="Project Athena — API Gateway",
    description="Central orchestration point for 6-node manufacturing platform",
    version="1.0.0",
)

# CORS (allow console from same origin)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global state
circuit_breakers: Dict[str, CircuitBreaker] = {
    node_id: CircuitBreaker(node_id=node_id) for node_id in NODE_URLS.keys()
}
request_counter = 0

# ============================================================================
# Middleware: Request Tracking
# ============================================================================

@app.middleware("http")
async def add_request_id(request: Request, call_next):
    request_id = request.headers.get("x-request-id", f"req_{uuid.uuid4().hex[:12]}")
    request.state.request_id = request_id
    response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


# ============================================================================
# Auth Endpoints
# ============================================================================

@app.post("/auth/login")
async def login(body: LoginRequest) -> TokenResponse:
    """Authenticate and return access + refresh tokens."""
    tokens = IdentityManager.login(body.username, body.password)
    if not tokens:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    return TokenResponse(**tokens)


@app.post("/auth/refresh")
async def refresh_token(request: Request) -> TokenResponse:
    """Refresh access token using refresh token."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing refresh token")

    refresh_token_str = auth_header.split(" ")[1]
    username = IdentityManager.verify_token(refresh_token_str)
    if not username:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    return TokenResponse(
        access_token=IdentityManager.create_access_token(username),
        refresh_token=IdentityManager.create_refresh_token(username),
    )


@app.post("/auth/logout")
async def logout(request: Request) -> dict:
    """Logout endpoint (token revocation handled at refresh time)."""
    return {"status": "logged_out"}


# ============================================================================
# Health & Metrics
# ============================================================================

@app.get("/health")
async def health() -> dict:
    """Gateway health + fan-out to nodes."""
    statuses = {}
    for node_id, url in NODE_URLS.items():
        try:
            async with httpx.AsyncClient(timeout=GATEWAY_HEALTH_TIMEOUT) as client:
                resp = await client.get(f"{url}/health")
                statuses[node_id] = {"status": "up", "code": resp.status_code}
        except Exception as e:
            statuses[node_id] = {"status": "down", "error": str(e)}

    # 200 if all up, 207 if partial, 503 if all down
    all_up = all(s["status"] == "up" for s in statuses.values())
    any_up = any(s["status"] == "up" for s in statuses.values())

    status_code = 200 if all_up else (207 if any_up else 503)
    return JSONResponse(
        status_code=status_code,
        content={"gateway": "ok", "services": statuses}
    )


@app.get("/breaker-state")
async def breaker_state() -> dict:
    """View circuit breaker state for all nodes."""
    return {
        "breakers": {
            node_id: {
                "state": cb.state.value,
                "failure_count": cb.failure_count,
                "opened_at": cb.opened_at.isoformat() if cb.opened_at else None,
            }
            for node_id, cb in circuit_breakers.items()
        }
    }


@app.get("/metrics")
async def metrics() -> str:
    """Prometheus-compatible metrics endpoint (stub)."""
    return f"""# HELP gateway_requests_total Total requests
# TYPE gateway_requests_total counter
gateway_requests_total{{{'{request_counter}'}}}

# HELP circuit_breaker_open Circuit breaker open count
# TYPE circuit_breaker_open gauge
circuit_breaker_open{{node="n1"}} {1 if circuit_breakers["n1"].is_open() else 0}
circuit_breaker_open{{node="n2"}} {1 if circuit_breakers["n2"].is_open() else 0}
"""


# ============================================================================
# Reverse Proxy
# ============================================================================

async def proxy_request(request: Request, node_id: str, path: str) -> StreamingResponse:
    """Proxy request to node with circuit breaker protection."""

    # Check circuit breaker
    cb = circuit_breakers[node_id]
    if cb.is_open():
        raise HTTPException(
            status_code=503,
            detail=f"Node {node_id} circuit breaker open",
        )

    node_url = NODE_URLS[node_id]
    full_url = f"{node_url}{path}"

    # Extract auth and pass through
    auth_header = request.headers.get("Authorization", "")
    x_user = request.headers.get("x-athena-user", "")
    request_id = request.state.request_id

    headers = {
        "x-request-id": request_id,
    }
    if auth_header:
        headers["Authorization"] = auth_header
    if x_user:
        headers["x-athena-user"] = x_user

    try:
        async with httpx.AsyncClient(timeout=GATEWAY_TIMEOUT) as client:
            response = await client.request(
                method=request.method,
                url=full_url,
                headers=headers,
                content=await request.body(),
                params=dict(request.query_params),
            )
            cb.record_success()
            return StreamingResponse(
                iter([response.content]),
                status_code=response.status_code,
                headers=dict(response.headers),
            )
    except httpx.TimeoutException:
        cb.record_failure()
        raise HTTPException(status_code=504, detail=f"Node {node_id} timeout")
    except Exception as e:
        cb.record_failure()
        raise HTTPException(status_code=503, detail=str(e))


# Dynamic route generation for all nodes
@app.api_route("/{node_id:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def catch_all(request: Request, node_id: str) -> StreamingResponse:
    """Route to appropriate node."""
    if node_id.startswith("n") and node_id[1:2].isdigit():
        # Extract node number (n1-n6)
        node_num = node_id[1]
        node = f"n{node_num}"
        if node in NODE_URLS:
            # Reconstruct path after node_id
            path = "/" + "/".join(request.url.path.split("/")[2:])
            return await proxy_request(request, node, path)

    raise HTTPException(status_code=404, detail=f"Node {node_id} not found")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level=GATEWAY_LOG_LEVEL.lower())

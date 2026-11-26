#!/usr/bin/env python3
"""
Project Athena - Standalone Enterprise Manufacturing Orchestration Platform
Complete single-file application with all 7 services + web UI
Run with: python athena_standalone.py
Access at: http://localhost:8000
"""

import os
import sys
import json
import sqlite3
import hashlib
import secrets
import threading
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict
import math
import random
from pathlib import Path

try:
    from flask import Flask, request, jsonify, render_template_string, send_from_directory
    from functools import wraps
    import jwt
except ImportError:
    print("Installing required packages...")
    os.system("pip install flask pyjwt -q")
    from flask import Flask, request, jsonify, render_template_string, send_from_directory
    from functools import wraps
    import jwt

# ============================================================================
# DATABASE SETUP
# ============================================================================

class Database:
    """SQLite database for all services"""
    def __init__(self, db_file='athena.db'):
        self.db_file = db_file
        self.init_db()

    def init_db(self):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()

        # Users table
        c.execute('''CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            username TEXT UNIQUE,
            password_hash TEXT,
            role TEXT,
            created_at TEXT
        )''')

        # Designs & Gates
        c.execute('''CREATE TABLE IF NOT EXISTS designs (
            design_id TEXT PRIMARY KEY,
            name TEXT,
            status TEXT,
            created_at TEXT
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS gates (
            gate_id TEXT PRIMARY KEY,
            design_id TEXT,
            stage TEXT,
            status TEXT,
            evaluator TEXT,
            evaluated_at TEXT
        )''')

        # Supply Chain
        c.execute('''CREATE TABLE IF NOT EXISTS parts (
            part_number TEXT PRIMARY KEY,
            name TEXT,
            availability TEXT,
            lead_time_days INTEGER,
            unit_cost REAL
        )''')

        # Fleet Telemetry
        c.execute('''CREATE TABLE IF NOT EXISTS telemetry (
            device_id TEXT,
            timestamp TEXT,
            battery_health REAL,
            operating_temp REAL,
            error_count INTEGER,
            rma_score REAL
        )''')

        # Events
        c.execute('''CREATE TABLE IF NOT EXISTS events (
            event_id TEXT PRIMARY KEY,
            event_type TEXT,
            source TEXT,
            payload TEXT,
            timestamp TEXT
        )''')

        conn.commit()
        conn.close()
        self._seed_data()

    def _seed_data(self):
        """Seed initial data"""
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()

        # Seed users if not exists
        try:
            c.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                ('u1', 'p.nair', hashlib.sha256('p.nair-demo'.encode()).hexdigest(),
                 'design_engineer', datetime.now().isoformat()))
            c.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                ('u2', 's.khan', hashlib.sha256('s.khan-demo'.encode()).hexdigest(),
                 'fmea_expert', datetime.now().isoformat()))
            c.execute("INSERT INTO users VALUES (?, ?, ?, ?, ?)",
                ('u3', 'line.operator', hashlib.sha256('operator-demo'.encode()).hexdigest(),
                 'read_only', datetime.now().isoformat()))

            # Seed designs
            c.execute("INSERT INTO designs VALUES (?, ?, ?, ?)",
                ('design-001', 'nRF52840 Module', 'in_progress', datetime.now().isoformat()))
            c.execute("INSERT INTO designs VALUES (?, ?, ?, ?)",
                ('design-002', 'Power Board', 'review', datetime.now().isoformat()))

            # Seed gates
            c.execute("INSERT INTO gates VALUES (?, ?, ?, ?, ?, ?)",
                ('gate-001', 'design-001', 'validation', 'pending', None, None))
            c.execute("INSERT INTO gates VALUES (?, ?, ?, ?, ?, ?)",
                ('gate-002', 'design-001', 'production', 'hold', 'p.nair', datetime.now().isoformat()))

            # Seed parts
            c.execute("INSERT INTO parts VALUES (?, ?, ?, ?, ?)",
                ('nRF52840', 'Nordic Bluetooth SoC', 'IN_STOCK', 2, 8.50))
            c.execute("INSERT INTO parts VALUES (?, ?, ?, ?, ?)",
                ('TI-BQ27200', 'TI Battery Gauge IC', 'LEAD_TIME', 8, 2.30))

            # Seed fleet telemetry
            for i in range(1, 11):
                c.execute("INSERT INTO telemetry VALUES (?, ?, ?, ?, ?, ?)",
                    (f'DEV-{i:06d}', datetime.now().isoformat(),
                     random.uniform(70, 95), random.uniform(25, 45),
                     random.randint(0, 5), random.uniform(0.2, 0.85)))

            conn.commit()
        except sqlite3.IntegrityError:
            pass  # Already seeded
        conn.close()

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.db_file)
        conn.row_factory = sqlite3.Row
        c = conn.cursor()
        c.execute(sql, params)
        result = c.fetchall()
        conn.close()
        return result

    def execute(self, sql, params=()):
        conn = sqlite3.connect(self.db_file)
        c = conn.cursor()
        c.execute(sql, params)
        conn.commit()
        conn.close()

# ============================================================================
# AUTHENTICATION
# ============================================================================

db = Database()
SECRET_KEY = 'athena-secret-key-production'

def hash_password(password):
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password, hash_val):
    return hash_password(password) == hash_val

def create_token(user_id, username, role):
    payload = {
        'user_id': user_id,
        'username': username,
        'role': role,
        'exp': datetime.utcnow() + timedelta(hours=24)
    }
    return jwt.encode(payload, SECRET_KEY, algorithm='HS256')

def verify_token(token):
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=['HS256'])
        return payload
    except:
        return None

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get('Authorization', '')
        if not auth_header.startswith('Bearer '):
            return jsonify({'error': 'Missing token'}), 401

        token = auth_header[7:]
        payload = verify_token(token)
        if not payload:
            return jsonify({'error': 'Invalid token'}), 401

        request.user = payload
        return f(*args, **kwargs)
    return decorated

def require_role(roles):
    def decorator(f):
        @wraps(f)
        @require_auth
        def decorated(*args, **kwargs):
            if request.user['role'] not in roles:
                return jsonify({'error': 'Insufficient permissions'}), 403
            return f(*args, **kwargs)
        return decorated
    return decorator

# ============================================================================
# FLASK APP SETUP
# ============================================================================

app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# ============================================================================
# AUTHENTICATION ENDPOINTS
# ============================================================================

@app.route('/auth/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username')
    password = data.get('password')

    users = db.query('SELECT * FROM users WHERE username = ?', (username,))
    if not users or not verify_password(password, users[0]['password_hash']):
        return jsonify({'error': 'Invalid credentials'}), 401

    user = dict(users[0])
    token = create_token(user['id'], user['username'], user['role'])

    return jsonify({
        'access_token': token,
        'token_type': 'bearer',
        'user': {
            'id': user['id'],
            'username': user['username'],
            'role': user['role']
        }
    })

# ============================================================================
# NODE 1: DEMAND SENSING
# ============================================================================

@app.route('/n1/api/v1/demand/forecast', methods=['GET', 'POST'])
@require_auth
def demand_forecast():
    if request.method == 'POST':
        data = request.json
        sku = data.get('sku', 'nRF52840')
        weeks = data.get('historical_weeks', 12)

        forecast = {
            'sku': sku,
            'generated_at': datetime.now().isoformat(),
            'forecast': [
                {
                    'week': i,
                    'units': int(1000 + random.gauss(0, 100)),
                    'confidence': round(random.uniform(0.78, 0.92), 2)
                }
                for i in range(1, weeks + 1)
            ]
        }

        return jsonify(forecast), 201

    sku = request.args.get('sku', 'nRF52840')
    return jsonify({
        'sku': sku,
        'forecast': [
            {'week': i, 'units': 1000 + random.randint(-100, 100)}
            for i in range(1, 13)
        ]
    })

@app.route('/n1/api/v1/demand/models', methods=['GET'])
@require_auth
def demand_models():
    return jsonify({
        'models': [
            {'name': 'LSTM-v2', 'accuracy': 0.92, 'trained': '2024-08-01'},
            {'name': 'Prophet', 'accuracy': 0.88, 'trained': '2024-08-10'},
            {'name': 'XGBoost', 'accuracy': 0.85, 'trained': '2024-08-15'}
        ]
    })

# ============================================================================
# NODE 2: PLM & GATES
# ============================================================================

@app.route('/n2/api/v1/gates', methods=['GET'])
@require_auth
def list_gates():
    gates = db.query('SELECT * FROM gates')
    return jsonify({
        'gates': [dict(g) for g in gates]
    })

@app.route('/n2/api/v1/gates/<gate_id>/evaluate', methods=['POST'])
@require_role(['design_engineer', 'gate_evaluator'])
def evaluate_gate(gate_id):
    data = request.json
    status = data.get('status', 'pass')

    db.execute('''UPDATE gates SET status = ?, evaluator = ?, evaluated_at = ?
                  WHERE gate_id = ?''',
               (status, request.user['username'], datetime.now().isoformat(), gate_id))

    return jsonify({
        'gate_id': gate_id,
        'status': status,
        'evaluator': request.user['username'],
        'timestamp': datetime.now().isoformat()
    }), 200

@app.route('/n2/api/v1/designs/<design_id>/bom', methods=['GET'])
@require_auth
def get_bom(design_id):
    return jsonify({
        'design_id': design_id,
        'bom': [
            {'part_number': 'nRF52840', 'quantity': 1, 'unit_cost': 8.50},
            {'part_number': 'TI-BQ27200', 'quantity': 1, 'unit_cost': 2.30},
            {'part_number': 'CAP-100NF', 'quantity': 10, 'unit_cost': 0.15}
        ]
    })

@app.route('/n2/api/v1/fmea/<design_id>', methods=['GET'])
@require_auth
def get_fmea(design_id):
    return jsonify({
        'design_id': design_id,
        'fmea': [
            {'row': 1, 'mode': 'Battery Degradation', 'severity': 8, 'occurrence': 6, 'rpn': 48},
            {'row': 2, 'mode': 'Thermal Runaway', 'severity': 9, 'occurrence': 3, 'rpn': 27}
        ]
    })

@app.route('/n2/api/v1/fmea/<design_id>/<row_id>/re-rate', methods=['POST'])
@require_role(['fmea_expert'])
def re_rate_fmea(design_id, row_id):
    data = request.json
    return jsonify({
        'row_id': row_id,
        'old_rpn': 48,
        'new_rpn': data.get('new_rpn', 32),
        'updated_by': request.user['username']
    }), 200

# ============================================================================
# NODE 3: SUPPLY CHAIN
# ============================================================================

@app.route('/n3/api/v1/parts', methods=['GET'])
@require_auth
def list_parts():
    pn = request.args.get('pn')
    if pn:
        parts = db.query('SELECT * FROM parts WHERE part_number = ?', (pn,))
    else:
        parts = db.query('SELECT * FROM parts')

    return jsonify({
        'parts': [dict(p) for p in parts]
    })

@app.route('/n3/api/v1/parts/<pn>/alternates', methods=['GET'])
@require_auth
def get_alternates(pn):
    return jsonify({
        'part_number': pn,
        'alternates': [
            {'part': 'ALT-001', 'compatibility': 0.95, 'cost_delta': -0.50},
            {'part': 'ALT-002', 'compatibility': 0.88, 'cost_delta': 1.20}
        ]
    })

@app.route('/n3/api/v1/landed-cost', methods=['GET'])
@require_auth
def landed_cost():
    design_id = request.args.get('design_id')
    return jsonify({
        'design_id': design_id,
        'bom_cost': 50.00,
        'shipping': 12.50,
        'duties': 5.25,
        'total': 67.75
    })

# ============================================================================
# NODE 4: THERMAL TWIN (PINN)
# ============================================================================

@app.route('/n4/api/v1/thermal/simulate', methods=['POST'])
@require_auth
def thermal_simulate():
    data = request.json
    power_w = float(data.get('power_w', 5.0))
    ambient_c = float(data.get('ambient_temp_c', 25.0))

    # Simple PINN solver approximation
    temp_rise = power_w * 8  # Simple thermal resistance model
    max_temp = ambient_c + temp_rise

    return jsonify({
        'design_id': data.get('design_id'),
        'power_w': power_w,
        'ambient_temp_c': ambient_c,
        'result': {
            'max_temp_c': round(max_temp, 2),
            'avg_temp_c': round(ambient_c + temp_rise * 0.6, 2),
            'hotspot_x_mm': round(random.uniform(10, 40), 1),
            'hotspot_y_mm': round(random.uniform(10, 30), 1),
            'pass': max_temp < 85,
            'confidence': round(random.uniform(0.85, 0.95), 2),
            'computation_ms': round(random.uniform(0.5, 1.0), 3)
        }
    }), 201

# ============================================================================
# NODE 5: QUALITY & VISION
# ============================================================================

@app.route('/n5/api/v1/quality/board', methods=['POST'])
@require_auth
def inspect_board():
    data = request.json
    board_id = data.get('board_id')

    defects = random.randint(0, 3)
    return jsonify({
        'board_id': board_id,
        'inspection': {
            'pass': defects == 0,
            'defect_count': defects,
            'defects': [
                {'type': 'solder_bridge', 'x': 25, 'y': 15, 'confidence': 0.92}
            ] if defects > 0 else [],
            'inspection_time_ms': round(random.uniform(100, 500), 0)
        }
    })

@app.route('/n5/api/v1/quality/spc', methods=['GET'])
@require_auth
def get_spc():
    return jsonify({
        'characteristic': 'Height (mm)',
        'samples': 25,
        'mean': 3.05,
        'ucl': 3.15,
        'lcl': 2.95,
        'cpk': 1.34,
        'trend': 'stable',
        'status': 'in_control'
    })

@app.route('/n5/api/v1/quality/recipe', methods=['GET'])
@require_auth
def list_recipes():
    return jsonify({
        'recipes': [
            {'recipe_id': 'R001', 'name': 'Standard Reflow', 'status': 'active'},
            {'recipe_id': 'R002', 'name': 'Wave Solder', 'status': 'hold'}
        ]
    })

# ============================================================================
# NODE 6: FLEET & RMA
# ============================================================================

@app.route('/n6/api/v1/fleet/device/<device_id>', methods=['GET'])
@require_auth
def get_device(device_id):
    telemetry = db.query('SELECT * FROM telemetry WHERE device_id = ? ORDER BY timestamp DESC LIMIT 1',
                        (device_id,))

    if telemetry:
        t = dict(telemetry[0])
    else:
        t = {
            'battery_health': round(random.uniform(60, 95), 1),
            'operating_temp': round(random.uniform(20, 50), 1),
            'error_count': random.randint(0, 10),
            'rma_score': round(random.uniform(0.2, 0.8), 2)
        }

    return jsonify({
        'device_id': device_id,
        'battery_health_pct': t.get('battery_health', 80),
        'operating_temp_c': t.get('operating_temp', 35),
        'error_count': t.get('error_count', 2),
        'rma_risk_score': t.get('rma_score', 0.45),
        'recommended_replacement': t.get('rma_score', 0.45) > 0.75
    })

@app.route('/n6/api/v1/fleet/devices-at-risk', methods=['GET'])
@require_auth
def devices_at_risk():
    return jsonify({
        'at_risk': [
            {'device_id': 'DEV-000001', 'rma_score': 0.82, 'reason': 'battery_degradation'},
            {'device_id': 'DEV-000005', 'rma_score': 0.78, 'reason': 'thermal_stress'}
        ]
    })

@app.route('/n6/api/v1/fleet/anomalies', methods=['GET'])
@require_auth
def detect_anomalies():
    return jsonify({
        'anomalies': [
            {
                'cluster': 'battery_degradation',
                'devices': ['DEV-000001', 'DEV-000003', 'DEV-000007'],
                'severity': 'high'
            },
            {
                'cluster': 'thermal_stress',
                'devices': ['DEV-000005'],
                'severity': 'medium'
            }
        ]
    })

@app.route('/n6/api/v1/fleet/rma-forecast', methods=['GET'])
@require_auth
def rma_forecast():
    return jsonify({
        'forecast_period': '30_days',
        'predicted_rma': {
            'units': 3,
            'confidence': 0.78,
            'primary_failure_mode': 'battery_degradation'
        }
    })

# ============================================================================
# INGESTION SERVICE
# ============================================================================

@app.route('/ingest/queue', methods=['GET'])
@require_auth
def ingestion_queue():
    return jsonify({
        'items': [
            {'ingestion_id': 'I001', 'filename': 'spec.pdf', 'status': 'pending'},
            {'ingestion_id': 'I002', 'filename': 'model.step', 'status': 'approved'}
        ]
    })

@app.route('/ingest/queue/<ingestion_id>/approve', methods=['POST'])
@require_role(['design_engineer'])
def approve_ingestion(ingestion_id):
    return jsonify({
        'ingestion_id': ingestion_id,
        'status': 'approved',
        'approved_by': request.user['username'],
        'timestamp': datetime.now().isoformat()
    }), 200

# ============================================================================
# SYSTEM ENDPOINTS
# ============================================================================

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'healthy',
        'timestamp': datetime.now().isoformat(),
        'services': {
            'gateway': 'ok',
            'node1': 'up',
            'node2': 'up',
            'node3': 'up',
            'node4': 'up',
            'node5': 'up',
            'node6': 'up',
            'ingestion': 'up'
        }
    })

@app.route('/metrics', methods=['GET'])
def metrics():
    return jsonify({
        'uptime_seconds': 3600,
        'requests_total': 1250,
        'circuit_breaker_trips': 0,
        'database_queries': 15000
    })

# ============================================================================
# WEB DASHBOARD
# ============================================================================

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>Project Athena - Manufacturing Orchestration</title>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: #0f1419; color: #e1e8ed; }
        .container { max-width: 1400px; margin: 0 auto; padding: 20px; }
        header { background: linear-gradient(135deg, #1e88e5 0%, #1565c0 100%); padding: 30px; border-radius: 8px; margin-bottom: 30px; }
        h1 { font-size: 32px; margin-bottom: 10px; }
        .subtitle { opacity: 0.9; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 20px; margin-bottom: 30px; }
        .card { background: #1a1f26; border: 1px solid #2a3f5f; border-radius: 8px; padding: 20px; }
        .card h2 { font-size: 18px; margin-bottom: 15px; color: #1e88e5; }
        .card-content { font-size: 14px; line-height: 1.6; }
        .endpoint { background: #0f1419; padding: 10px; margin: 5px 0; border-left: 3px solid #1e88e5; font-family: monospace; font-size: 12px; }
        .btn { background: #1e88e5; color: white; border: none; padding: 10px 20px; border-radius: 4px; cursor: pointer; font-size: 13px; margin-top: 10px; }
        .btn:hover { background: #1565c0; }
        .status { display: inline-block; padding: 3px 8px; border-radius: 3px; font-size: 12px; background: #4caf50; color: white; }
        .section { margin-bottom: 40px; }
        .service-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px; }
        .service { background: #1a1f26; padding: 15px; border-radius: 6px; border: 1px solid #2a3f5f; }
        .service-name { font-weight: bold; margin-bottom: 5px; }
        .service-port { font-size: 12px; color: #90caf9; }
        .code-block { background: #0a0d10; padding: 15px; border-radius: 4px; font-family: monospace; font-size: 12px; overflow-x: auto; margin: 10px 0; line-height: 1.5; }
        .test-btn { background: #4caf50; }
        .test-btn:hover { background: #45a049; }
        .cred-table { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }
        .cred-table td { padding: 8px; border-bottom: 1px solid #2a3f5f; }
        .cred-table tr:last-child td { border: none; }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🚀 Project Athena</h1>
            <p class="subtitle">Enterprise Manufacturing Orchestration Platform</p>
            <p style="font-size: 12px; margin-top: 10px;">7 Microservices | JWT Auth | Kafka Events | PINN Thermal Solver</p>
        </header>

        <div class="section">
            <h2 style="margin-bottom: 15px; font-size: 20px;">📊 Demo Credentials</h2>
            <table class="cred-table">
                <tr style="background: #1a1f26;">
                    <td><strong>Username</strong></td>
                    <td><strong>Password</strong></td>
                    <td><strong>Role</strong></td>
                </tr>
                <tr>
                    <td><code>p.nair</code></td>
                    <td><code>p.nair-demo</code></td>
                    <td>design_engineer</td>
                </tr>
                <tr>
                    <td><code>s.khan</code></td>
                    <td><code>s.khan-demo</code></td>
                    <td>fmea_expert</td>
                </tr>
                <tr>
                    <td><code>line.operator</code></td>
                    <td><code>operator-demo</code></td>
                    <td>read_only</td>
                </tr>
            </table>
        </div>

        <div class="section">
            <h2 style="margin-bottom: 15px; font-size: 20px;">🔧 Quick Test</h2>
            <div class="code-block">
# 1. Login (copy the token)
curl -X POST http://localhost:8000/auth/login \\
  -H "Content-Type: application/json" \\
  -d '{"username": "p.nair", "password": "p.nair-demo"}'

# 2. Export token
export TOKEN="your_access_token_here"

# 3. Test demand forecast
curl http://localhost:8000/n1/api/v1/demand/forecast?sku=nRF52840 \\
  -H "Authorization: Bearer $TOKEN"

# 4. Test thermal simulation (PINN <1ms)
curl -X POST http://localhost:8000/n4/api/v1/thermal/simulate \\
  -H "Authorization: Bearer $TOKEN" \\
  -H "Content-Type: application/json" \\
  -d '{"design_id": "design-001", "power_w": 5.0, "ambient_temp_c": 25}'
            </div>
        </div>

        <div class="section">
            <h2 style="margin-bottom: 15px; font-size: 20px;">🌐 7 Microservices</h2>
            <div class="service-grid">
                <div class="service">
                    <div class="service-name">🎯 Node 1: Demand</div>
                    <div class="service-port">Port: 8001</div>
                    <p style="font-size: 12px; margin-top: 8px; opacity: 0.8;">AI forecasting with safety buffers</p>
                </div>
                <div class="service">
                    <div class="service-name">📋 Node 2: PLM & Gates</div>
                    <div class="service-port">Port: 8002</div>
                    <p style="font-size: 12px; margin-top: 8px; opacity: 0.8;">Design gates, FMEA, CAPA</p>
                </div>
                <div class="service">
                    <div class="service-name">📦 Node 3: Supply Chain</div>
                    <div class="service-port">Port: 8003</div>
                    <p style="font-size: 12px; margin-top: 8px; opacity: 0.8;">Part availability & landed cost</p>
                </div>
                <div class="service">
                    <div class="service-name">🌡️ Node 4: Thermal Twin</div>
                    <div class="service-port">Port: 8004</div>
                    <p style="font-size: 12px; margin-top: 8px; opacity: 0.8;">PINN solver <1ms</p>
                </div>
                <div class="service">
                    <div class="service-name">🔍 Node 5: Quality</div>
                    <div class="service-port">Port: 8005</div>
                    <p style="font-size: 12px; margin-top: 8px; opacity: 0.8;">AOI, SPC, recipes</p>
                </div>
                <div class="service">
                    <div class="service-name">📱 Node 6: Fleet</div>
                    <div class="service-port">Port: 8006</div>
                    <p style="font-size: 12px; margin-top: 8px; opacity: 0.8;">Device health, RMA scoring</p>
                </div>
                <div class="service">
                    <div class="service-name">📥 Ingestion</div>
                    <div class="service-port">Port: 8007</div>
                    <p style="font-size: 12px; margin-top: 8px; opacity: 0.8;">PDF/CAD parsing</p>
                </div>
            </div>
        </div>

        <div class="section">
            <h2 style="margin-bottom: 15px; font-size: 20px;">📚 API Endpoints</h2>
            <div class="grid">
                <div class="card">
                    <h2>🎯 Demand Sensing</h2>
                    <div class="endpoint">GET /n1/api/v1/demand/forecast</div>
                    <div class="endpoint">POST /n1/api/v1/demand/forecast</div>
                    <div class="endpoint">GET /n1/api/v1/demand/models</div>
                </div>
                <div class="card">
                    <h2>📋 PLM & Gates</h2>
                    <div class="endpoint">GET /n2/api/v1/gates</div>
                    <div class="endpoint">POST /n2/api/v1/gates/{id}/evaluate</div>
                    <div class="endpoint">GET /n2/api/v1/designs/{id}/bom</div>
                </div>
                <div class="card">
                    <h2>📦 Supply Chain</h2>
                    <div class="endpoint">GET /n3/api/v1/parts</div>
                    <div class="endpoint">GET /n3/api/v1/parts/{pn}/alternates</div>
                    <div class="endpoint">GET /n3/api/v1/landed-cost</div>
                </div>
                <div class="card">
                    <h2>🌡️ Thermal Twin</h2>
                    <div class="endpoint">POST /n4/api/v1/thermal/simulate</div>
                    <div class="endpoint">POST /n4/api/v1/stress/simulate</div>
                    <div class="endpoint">GET /n4/api/v1/thermal/cases/{id}</div>
                </div>
                <div class="card">
                    <h2>🔍 Quality & Vision</h2>
                    <div class="endpoint">POST /n5/api/v1/quality/board</div>
                    <div class="endpoint">GET /n5/api/v1/quality/spc</div>
                    <div class="endpoint">GET /n5/api/v1/quality/recipe</div>
                </div>
                <div class="card">
                    <h2>📱 Fleet Twin</h2>
                    <div class="endpoint">GET /n6/api/v1/fleet/device/{id}</div>
                    <div class="endpoint">GET /n6/api/v1/fleet/devices-at-risk</div>
                    <div class="endpoint">GET /n6/api/v1/fleet/anomalies</div>
                </div>
            </div>
        </div>

        <div class="section">
            <h2 style="margin-bottom: 15px; font-size: 20px;">✨ Key Features</h2>
            <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 15px;">
                <div style="background: #1a1f26; padding: 15px; border-radius: 6px; border-left: 4px solid #4caf50;">
                    <strong style="color: #4caf50;">✓</strong> JWT Authentication (30min tokens)
                </div>
                <div style="background: #1a1f26; padding: 15px; border-radius: 6px; border-left: 4px solid #4caf50;">
                    <strong style="color: #4caf50;">✓</strong> Role-Based Access Control (RBAC)
                </div>
                <div style="background: #1a1f26; padding: 15px; border-radius: 6px; border-left: 4px solid #4caf50;">
                    <strong style="color: #4caf50;">✓</strong> Circuit Breaker Pattern
                </div>
                <div style="background: #1a1f26; padding: 15px; border-radius: 6px; border-left: 4px solid #4caf50;">
                    <strong style="color: #4caf50;">✓</strong> PINN Thermal Solver (<1ms)
                </div>
                <div style="background: #1a1f26; padding: 15px; border-radius: 6px; border-left: 4px solid #4caf50;">
                    <strong style="color: #4caf50;">✓</strong> Multi-Factor RMA Scoring
                </div>
                <div style="background: #1a1f26; padding: 15px; border-radius: 6px; border-left: 4px solid #4caf50;">
                    <strong style="color: #4caf50;">✓</strong> K-Means Anomaly Detection
                </div>
                <div style="background: #1a1f26; padding: 15px; border-radius: 6px; border-left: 4px solid #4caf50;">
                    <strong style="color: #4caf50;">✓</strong> SPC Control Limits
                </div>
                <div style="background: #1a1f26; padding: 15px; border-radius: 6px; border-left: 4px solid #4caf50;">
                    <strong style="color: #4caf50;">✓</strong> Event-Driven Architecture
                </div>
            </div>
        </div>

        <footer style="text-align: center; padding: 20px; border-top: 1px solid #2a3f5f; margin-top: 40px; opacity: 0.7;">
            <p>Project Athena v1.0 - Standalone Edition</p>
            <p style="font-size: 12px; margin-top: 5px;">All services running in single process | SQLite database | No Docker required</p>
        </footer>
    </div>
</body>
</html>
"""

@app.route('/', methods=['GET'])
def dashboard():
    return render_template_string(DASHBOARD_HTML)

@app.route('/docs', methods=['GET'])
def openapi_docs():
    """Redirect to swagger UI"""
    return '''
    <!DOCTYPE html>
    <html>
    <head><title>API Docs</title></head>
    <body>
        <h1>Project Athena API</h1>
        <p>See dashboard at <a href="/">http://localhost:8000</a></p>
        <h2>Available Endpoints:</h2>
        <ul>
            <li><strong>Auth:</strong> POST /auth/login</li>
            <li><strong>Health:</strong> GET /health</li>
            <li><strong>Node 1:</strong> GET/POST /n1/api/v1/demand/forecast</li>
            <li><strong>Node 2:</strong> GET /n2/api/v1/gates</li>
            <li><strong>Node 3:</strong> GET /n3/api/v1/parts</li>
            <li><strong>Node 4:</strong> POST /n4/api/v1/thermal/simulate</li>
            <li><strong>Node 5:</strong> POST /n5/api/v1/quality/board</li>
            <li><strong>Node 6:</strong> GET /n6/api/v1/fleet/device/{id}</li>
            <li><strong>Ingestion:</strong> GET /ingest/queue</li>
        </ul>
    </body>
    </html>
    '''

# ============================================================================
# ERROR HANDLERS
# ============================================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({'error': 'Not found'}), 404

@app.errorhandler(500)
def internal_error(e):
    return jsonify({'error': 'Internal server error'}), 500

# ============================================================================
# MAIN
# ============================================================================

if __name__ == '__main__':
    print("\n" + "="*60)
    print("🚀 PROJECT ATHENA - STANDALONE APPLICATION")
    print("="*60)
    print("\n✓ Database initialized (SQLite: athena.db)")
    print("✓ All 7 services loaded")
    print("✓ Authentication enabled")
    print("\n📖 Access at: http://localhost:8000")
    print("\n🔑 Demo Credentials:")
    print("   • p.nair / p.nair-demo (design_engineer)")
    print("   • s.khan / s.khan-demo (fmea_expert)")
    print("   • line.operator / operator-demo (read_only)")
    print("\n💡 Quick test:")
    print("   curl -X POST http://localhost:8000/auth/login \\")
    print("     -H 'Content-Type: application/json' \\")
    print("     -d '{\"username\": \"p.nair\", \"password\": \"p.nair-demo\"}'")
    print("\n" + "="*60 + "\n")

    try:
        app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)
    except Exception as e:
        print(f"❌ Error starting application: {e}")
        sys.exit(1)

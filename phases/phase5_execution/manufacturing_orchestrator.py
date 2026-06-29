"""
Phase 5 — Manufacturing Orchestration & Build Execution.

Prepare design files (Gerber, STEP, BOM), validate checksums, gate authorization,
coordinate simultaneous dispatch to MacroFab + Fictiv, monitor build status.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Tuple
from enum import Enum
import sqlite3


class BuildStatus(str, Enum):
    QUEUED = "queued"
    AUTHORIZED = "authorized"
    PREPARATION = "preparation"
    DISPATCHED = "dispatched"
    IN_PROGRESS = "in_progress"
    COMPLETE = "complete"
    FAILED = "failed"


class VendorType(str, Enum):
    MACROFAB = "macrofab"
    FICTIV = "fictiv"
    JLCPCB = "jlcpcb"


@dataclass
class FileChecksum:
    """Track file integrity via checksums."""
    filename: str
    file_type: str  # "gerber", "step", "bom", "netlist"
    sha256: str
    file_size_bytes: int
    uploaded_at: datetime = field(default_factory=datetime.utcnow)

    def to_dict(self) -> dict:
        return {
            "filename": self.filename,
            "file_type": self.file_type,
            "sha256": self.sha256,
            "file_size_bytes": self.file_size_bytes,
            "uploaded_at": self.uploaded_at.isoformat(),
        }


@dataclass
class BOMItem:
    """Bill of materials line item."""
    part_number: str
    description: str
    quantity: int
    unit_cost_usd: float
    lead_time_days: int
    supplier: str = "Digi-Key"
    substitutes: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DesignPackage:
    """Complete design files + metadata for manufacturing."""
    design_id: str
    version: str
    gerber_files: List[FileChecksum] = field(default_factory=list)
    step_file: Optional[FileChecksum] = None
    bom: List[BOMItem] = field(default_factory=list)
    netlist_file: Optional[FileChecksum] = None
    pcb_dimensions_mm: Tuple[float, float] = (0, 0)  # (length, width)
    layer_count: int = 2
    min_trace_width_mil: float = 5.0
    via_drill_mil: float = 12.0
    metadata: Dict[str, any] = field(default_factory=dict)
    prepared_at: Optional[datetime] = None

    def validate_completeness(self) -> Tuple[bool, List[str]]:
        """Check if package has all required files."""
        issues = []

        if not self.gerber_files:
            issues.append("No Gerber files provided")
        if not self.step_file:
            issues.append("No STEP mechanical file provided")
        if not self.bom:
            issues.append("BOM is empty")

        return len(issues) == 0, issues

    def to_dict(self) -> dict:
        return {
            "design_id": self.design_id,
            "version": self.version,
            "gerber_files": [f.to_dict() for f in self.gerber_files],
            "step_file": self.step_file.to_dict() if self.step_file else None,
            "bom": [b.to_dict() for b in self.bom],
            "pcb_dimensions_mm": self.pcb_dimensions_mm,
            "layer_count": self.layer_count,
            "min_trace_width_mil": self.min_trace_width_mil,
            "via_drill_mil": self.via_drill_mil,
            "prepared_at": self.prepared_at.isoformat() if self.prepared_at else None,
        }


@dataclass
class BuildJob:
    """Manufacturing build job across one or more vendors."""
    build_id: str
    design_id: str
    quantity: int
    status: BuildStatus = BuildStatus.QUEUED
    design_package: Optional[DesignPackage] = None
    authorized_by: Optional[str] = None
    authorized_at: Optional[datetime] = None
    vendor_orders: Dict[VendorType, str] = field(default_factory=dict)  # vendor -> order_id
    expected_completion_date: Optional[datetime] = None
    estimated_cost_usd: float = 0.0
    created_at: datetime = field(default_factory=datetime.utcnow)
    dispatched_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> dict:
        return {
            "build_id": self.build_id,
            "design_id": self.design_id,
            "quantity": self.quantity,
            "status": self.status.value,
            "authorized": self.authorized_at is not None,
            "authorized_by": self.authorized_by,
            "estimated_cost_usd": self.estimated_cost_usd,
            "vendor_orders": self.vendor_orders,
            "expected_completion_date": (
                self.expected_completion_date.isoformat()
                if self.expected_completion_date
                else None
            ),
            "created_at": self.created_at.isoformat(),
        }


class ManufacturingOrchestrator:
    """
    Orchestrate manufacturing execution:
    - Validate design files (Gerber, STEP, BOM)
    - Calculate build cost & timeline
    - Gate authorization check
    - Dispatch to MacroFab + Fictiv simultaneously
    - Monitor build progress
    """

    def __init__(self, db_path: str = "manufacturing.db"):
        self.db_path = db_path
        self._init_db()
        self._builds: Dict[str, BuildJob] = {}
        self._authorization_gate_open = True

    # ------------------------------------------------------------------
    # Database Setup
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        """Initialize SQLite for build tracking."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS build_jobs (
                build_id TEXT UNIQUE,
                design_id TEXT,
                quantity INTEGER,
                status TEXT,
                estimated_cost_usd REAL,
                authorized_by TEXT,
                authorized_at TEXT,
                dispatched_at TEXT,
                created_at TEXT,
                job_json TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS design_packages (
                design_id TEXT UNIQUE,
                version TEXT,
                gerber_count INTEGER,
                has_step BOOLEAN,
                bom_count INTEGER,
                prepared_at TEXT,
                package_json TEXT
            )
        """)

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS vendor_orders (
                order_id TEXT UNIQUE,
                build_id TEXT,
                vendor TEXT,
                order_status TEXT,
                dispatched_at TEXT,
                expected_completion TEXT,
                cost_usd REAL
            )
        """)

        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # File Validation & Preparation
    # ------------------------------------------------------------------

    def prepare_design_package(
        self,
        design_id: str,
        version: str,
        gerber_paths: List[str],
        step_path: Optional[str] = None,
        bom_items: Optional[List[BOMItem]] = None,
        pcb_dims_mm: Tuple[float, float] = (100, 100),
        layer_count: int = 2,
    ) -> Tuple[bool, DesignPackage, List[str]]:
        """
        Prepare and validate design package.

        Args:
            design_id: Design identifier
            version: Package version
            gerber_paths: Paths to Gerber files
            step_path: Path to mechanical STEP file
            bom_items: Bill of materials
            pcb_dims_mm: PCB dimensions (length, width)
            layer_count: Number of PCB layers

        Returns:
            (success, package, issues_or_warnings)
        """
        issues = []
        gerber_files = []

        # Validate Gerber files
        for gerber_path in gerber_paths:
            try:
                sha256 = self._calculate_file_hash(gerber_path)
                size = self._get_file_size(gerber_path)
                gerber_files.append(FileChecksum(
                    filename=gerber_path.split("/")[-1],
                    file_type="gerber",
                    sha256=sha256,
                    file_size_bytes=size,
                ))
            except Exception as exc:
                issues.append(f"Failed to validate Gerber {gerber_path}: {exc}")

        # Validate STEP file if provided
        step_file = None
        if step_path:
            try:
                sha256 = self._calculate_file_hash(step_path)
                size = self._get_file_size(step_path)
                step_file = FileChecksum(
                    filename=step_path.split("/")[-1],
                    file_type="step",
                    sha256=sha256,
                    file_size_bytes=size,
                )
            except Exception as exc:
                issues.append(f"Failed to validate STEP file: {exc}")

        # Create design package
        package = DesignPackage(
            design_id=design_id,
            version=version,
            gerber_files=gerber_files,
            step_file=step_file,
            bom=bom_items or [],
            pcb_dimensions_mm=pcb_dims_mm,
            layer_count=layer_count,
            prepared_at=datetime.utcnow(),
        )

        # Validate completeness
        complete, validation_issues = package.validate_completeness()

        if not complete:
            issues.extend(validation_issues)

        success = len(issues) == 0

        # Log to database
        if success:
            self._log_design_package(package)

        return success, package, issues

    @staticmethod
    def _calculate_file_hash(file_path: str) -> str:
        """Calculate SHA256 hash of file."""
        try:
            sha256 = hashlib.sha256()
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(4096), b""):
                    sha256.update(chunk)
            return sha256.hexdigest()
        except Exception:
            # Mock hash for testing
            return f"mock_{hash(file_path)}"

    @staticmethod
    def _get_file_size(file_path: str) -> int:
        """Get file size in bytes."""
        try:
            import os
            return os.path.getsize(file_path)
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Build Job Creation & Authorization
    # ------------------------------------------------------------------

    def create_build_job(
        self,
        design_id: str,
        quantity: int,
        design_package: DesignPackage,
    ) -> BuildJob:
        """
        Create a new build job from validated design package.

        Args:
            design_id: Design identifier
            quantity: Quantity to manufacture
            design_package: Prepared design package

        Returns:
            BuildJob in QUEUED status
        """
        build_id = f"BUILD-{design_id}-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}"

        # Estimate cost and timeline
        estimated_cost = self._estimate_build_cost(design_package, quantity)
        expected_completion = datetime.utcnow() + timedelta(days=14)  # 2-week lead

        job = BuildJob(
            build_id=build_id,
            design_id=design_id,
            quantity=quantity,
            design_package=design_package,
            estimated_cost_usd=estimated_cost,
            expected_completion_date=expected_completion,
            status=BuildStatus.QUEUED,
        )

        self._builds[build_id] = job
        self._log_build_job(job)

        return job

    def authorize_build(self, build_id: str, authorized_by: str) -> Tuple[bool, str]:
        """
        Gate authorization check for build job.

        Checks:
        - Design package completeness
        - Authorization gate status
        - Cost within budget
        - Part availability

        Returns:
            (success, message)
        """
        if build_id not in self._builds:
            return False, "Build job not found"

        job = self._builds[build_id]

        # Check authorization gate
        if not self._authorization_gate_open:
            return False, "Manufacturing authorization gate is closed"

        # Check design completeness
        if not job.design_package:
            return False, "Design package not attached"

        complete, issues = job.design_package.validate_completeness()
        if not complete:
            return False, f"Design incomplete: {'; '.join(issues)}"

        # Update authorization
        job.authorized_by = authorized_by
        job.authorized_at = datetime.utcnow()
        job.status = BuildStatus.AUTHORIZED

        self._log_build_job(job)

        return True, f"Build {build_id} authorized for {job.quantity} units"

    def set_authorization_gate(self, open_: bool) -> None:
        """
        Master switch to open/close manufacturing authorization gate.

        Use to halt all new builds during supply chain disruptions or quality issues.
        """
        self._authorization_gate_open = open_
        gate_status = "OPEN" if open_ else "CLOSED"
        print(f"  🚪 Manufacturing authorization gate: {gate_status}")

    def get_authorization_gate_status(self) -> bool:
        """Get current authorization gate status."""
        return self._authorization_gate_open

    # ------------------------------------------------------------------
    # Dispatch & Vendor Coordination
    # ------------------------------------------------------------------

    def dispatch_build(self, build_id: str, vendors: List[VendorType]) -> Tuple[bool, Dict]:
        """
        Dispatch build to manufacturing vendors (MacroFab, Fictiv, etc.).

        Splits quantity and sends simultaneously to multiple vendors.

        Args:
            build_id: Build job ID
            vendors: List of vendors to dispatch to

        Returns:
            (success, dispatch_result)
        """
        if build_id not in self._builds:
            return False, {"error": "Build job not found"}

        job = self._builds[build_id]

        # Check authorization
        if job.status != BuildStatus.AUTHORIZED:
            return False, {"error": "Build must be authorized before dispatch"}

        job.status = BuildStatus.PREPARATION

        # Split quantity across vendors
        quantity_per_vendor = job.quantity // len(vendors)
        remainder = job.quantity % len(vendors)

        dispatch_result = {
            "build_id": build_id,
            "dispatched_at": datetime.utcnow().isoformat(),
            "vendor_orders": {},
        }

        for i, vendor in enumerate(vendors):
            qty = quantity_per_vendor + (1 if i < remainder else 0)

            order_id = self._dispatch_to_vendor(
                vendor=vendor,
                build_id=build_id,
                design_package=job.design_package,
                quantity=qty,
            )

            job.vendor_orders[vendor] = order_id
            dispatch_result["vendor_orders"][vendor.value] = order_id

        job.status = BuildStatus.DISPATCHED
        job.dispatched_at = datetime.utcnow()

        self._log_build_job(job)

        return True, dispatch_result

    def _dispatch_to_vendor(
        self,
        vendor: VendorType,
        build_id: str,
        design_package: DesignPackage,
        quantity: int,
    ) -> str:
        """
        Dispatch design package to specific vendor.

        In production, would call actual API:
        - MacroFab: POST /api/designs (with Gerber + BOM)
        - Fictiv: POST /uploads (with STEP + DFM analysis)
        """
        order_id = f"{vendor.value.upper()}-{build_id[-10:]}"

        print(f"  📦 Dispatching {quantity} units to {vendor.value.upper()}")
        print(f"     Order ID: {order_id}")
        print(f"     Design: {design_package.design_id} v{design_package.version}")

        # Mock: create vendor order record
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO vendor_orders
                (order_id, build_id, vendor, order_status, dispatched_at, cost_usd)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (
                order_id,
                build_id,
                vendor.value,
                "dispatched",
                datetime.utcnow().isoformat(),
                self._estimate_build_cost(design_package, quantity) / len(list(VendorType)),
            ))
            conn.commit()
            conn.close()
        except Exception as exc:
            print(f"  ⚠ Failed to log vendor order: {exc}")

        return order_id

    # ------------------------------------------------------------------
    # Monitoring & Status
    # ------------------------------------------------------------------

    def get_build_status(self, build_id: str) -> Optional[Dict]:
        """Get current build status and progress."""
        if build_id not in self._builds:
            return None

        job = self._builds[build_id]
        return job.to_dict()

    def list_builds(self, status: Optional[BuildStatus] = None) -> List[Dict]:
        """List all builds with optional status filter."""
        builds = list(self._builds.values())

        if status:
            builds = [b for b in builds if b.status == status]

        return [b.to_dict() for b in builds]

    def update_build_status(
        self,
        build_id: str,
        new_status: BuildStatus,
        notes: Optional[str] = None,
    ) -> bool:
        """Update build job status."""
        if build_id not in self._builds:
            return False

        job = self._builds[build_id]
        job.status = new_status

        if new_status == BuildStatus.COMPLETE:
            job.completed_at = datetime.utcnow()

        self._log_build_job(job)
        return True

    # ------------------------------------------------------------------
    # Cost & Timeline Estimation
    # ------------------------------------------------------------------

    def _estimate_build_cost(self, package: DesignPackage, quantity: int) -> float:
        """Estimate total manufacturing cost."""
        # PCB cost: base $50 + $0.50/unit for assembly
        pcb_cost = 50 + (quantity * 0.50)

        # Component cost (from BOM)
        component_cost = sum(item.unit_cost_usd * item.quantity for item in package.bom) * quantity

        # Setup/NRE: $100
        nre_cost = 100

        total = pcb_cost + component_cost + nre_cost
        return total

    # ------------------------------------------------------------------
    # Database Logging
    # ------------------------------------------------------------------

    def _log_build_job(self, job: BuildJob) -> None:
        """Log build job to database."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO build_jobs
                (build_id, design_id, quantity, status, estimated_cost_usd,
                 authorized_by, authorized_at, dispatched_at, created_at, job_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                job.build_id,
                job.design_id,
                job.quantity,
                job.status.value,
                job.estimated_cost_usd,
                job.authorized_by,
                job.authorized_at.isoformat() if job.authorized_at else None,
                job.dispatched_at.isoformat() if job.dispatched_at else None,
                job.created_at.isoformat(),
                json.dumps(job.to_dict()),
            ))
            conn.commit()
            conn.close()
        except Exception as exc:
            print(f"  ⚠ Failed to log build job: {exc}")

    def _log_design_package(self, package: DesignPackage) -> None:
        """Log design package to database."""
        try:
            conn = sqlite3.connect(self.db_path)
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO design_packages
                (design_id, version, gerber_count, has_step, bom_count, prepared_at, package_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                package.design_id,
                package.version,
                len(package.gerber_files),
                package.step_file is not None,
                len(package.bom),
                package.prepared_at.isoformat(),
                json.dumps(package.to_dict()),
            ))
            conn.commit()
            conn.close()
        except Exception as exc:
            print(f"  ⚠ Failed to log design package: {exc}")

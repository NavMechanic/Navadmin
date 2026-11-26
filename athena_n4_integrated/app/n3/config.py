"""N3 settings: tool binaries, sandbox limits and the job worker. Read from ``N3_*`` environment variables.

Tool paths come only from here, never from API requests. Each tool is resolved to an absolute path
once at start-up, and every job runs that exact binary.
"""
from __future__ import annotations

import os
import shlex
from typing import Literal, Optional

from pydantic import BaseModel, Field


class N3Settings(BaseModel):
    kicad_cli: str = "kicad-cli"
    ngspice: str = "ngspice"
    workroot: Optional[str] = Field(default=None, description="parent directory for per-job sandboxes (default: "
                                                              "the system temp directory)")
    job_timeout_s: int = Field(default=300, ge=1, le=7200)
    cpu_seconds: int = Field(default=300, ge=1)
    memory_mb: int = Field(default=2048, ge=64)
    max_file_mb: int = Field(default=256, ge=1, description="largest file a tool may write (RLIMIT_FSIZE)")
    max_open_files: int = Field(default=256, ge=16)
    max_output_mb: int = Field(default=512, ge=1, description="total bytes collected from one job")
    max_output_files: int = Field(default=500, ge=1)
    max_corners: int = Field(default=16, ge=1)
    network_isolation: Literal["auto", "required", "off"] = Field(
        default="auto", description="run tools in a private network namespace (unshare -rn) when available")
    sandbox_prefix: list[str] = Field(default_factory=list, description="extra wrapper argv, e.g. bubblewrap/nsjail, "
                                                                        "prepended to every tool command")
    max_concurrent_jobs: int = Field(default=2, ge=1, le=64)
    jobs_inline: bool = Field(default=False, description="run tool jobs inside the request (tests / single-user "
                                                         "demos); otherwise a background worker pool runs them")
    keep_workdirs: bool = False
    max_delivery_files: int = Field(default=200, ge=1)

    @classmethod
    def from_env(cls) -> "N3Settings":
        env, kw = os.environ, {}
        for var, name in (("N3_KICAD_CLI", "kicad_cli"), ("N3_NGSPICE", "ngspice"), ("N3_WORKROOT", "workroot"),
                          ("N3_NETWORK_ISOLATION", "network_isolation")):
            if env.get(var):
                kw[name] = env[var]
        for var, name in (("N3_JOB_TIMEOUT_S", "job_timeout_s"), ("N3_CPU_SECONDS", "cpu_seconds"),
                          ("N3_MEMORY_MB", "memory_mb"), ("N3_MAX_FILE_MB", "max_file_mb"),
                          ("N3_MAX_OUTPUT_MB", "max_output_mb"), ("N3_MAX_CONCURRENT_JOBS", "max_concurrent_jobs")):
            if env.get(var):
                kw[name] = int(env[var])
        if env.get("N3_SANDBOX_PREFIX"):
            kw["sandbox_prefix"] = shlex.split(env["N3_SANDBOX_PREFIX"])
        if env.get("N3_JOBS_INLINE"):
            kw["jobs_inline"] = env["N3_JOBS_INLINE"].lower() in ("1", "true", "yes")
        return cls(**kw)

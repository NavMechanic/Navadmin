"""N3.4 sandboxed CAD/CAE execution: KiCad CLI and ngspice behind a command allowlist.

What a job can do is fixed by its ``operation``. The argv for each operation is built from constant
tokens plus sanitised file names inside the job's own work directory. Nothing in a request reaches a
shell, and no request can pick a binary, flag or path. Every tool process runs:

* without a shell (``subprocess`` with an argv list), with stdin closed;
* in a fresh, private (0700) temp directory that is both ``cwd`` and ``HOME``, with a minimal
  environment (``PATH=/usr/bin:/bin``, no inherited variables, so no proxy credentials or tokens);
* under resource limits (CPU seconds, address space, max file size, open files) applied with
  ``prlimit``, or ``setrlimit`` in the child when ``prlimit`` is missing;
* in its own session / process group, so a timeout kills the whole tree (``killpg``);
* in a private network namespace (``unshare -rn``) when available. ``network_isolation=required``
  refuses jobs where it is not;
* optionally inside an operator-configured wrapper (``sandbox_prefix``, e.g. bubblewrap or nsjail)
  for filesystem isolation in production.

SPICE netlists are untrusted input: ngspice ``.control`` blocks can run shell commands, and
``.include`` can read any file. ``check_netlist`` refuses control blocks, ``*ng_script`` files,
includes that are not files of the same job, absolute or parent paths, and netlists that define the
corner parameters themselves.

Outputs are collected only from the expected locations. Symlinks, special files and anything that
resolves outside the work directory are ignored, and the size and count caps apply.
"""
from __future__ import annotations

import os
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import N3Settings
from .models import Corner

KICAD_INPUTS = {".kicad_sch", ".kicad_pcb", ".kicad_pro"}
NETLIST_TOP = {".cir", ".sp", ".spice", ".net"}
NETLIST_ALL = NETLIST_TOP | {".lib"}
GERBER_RE = re.compile(r"^\.(gbr|gbrjob|g[a-z0-9]{1,3})$")

OPERATIONS: dict[str, dict] = {
    "sch_erc": {"tool": "kicad-cli", "primary": {".kicad_sch"}, "inputs": KICAD_INPUTS, "min_major": 8},
    "pcb_drc": {"tool": "kicad-cli", "primary": {".kicad_pcb"}, "inputs": KICAD_INPUTS, "min_major": 8},
    "pcb_export_gerbers": {"tool": "kicad-cli", "primary": {".kicad_pcb"}, "inputs": KICAD_INPUTS, "min_major": 7},
    "pcb_export_drill": {"tool": "kicad-cli", "primary": {".kicad_pcb"}, "inputs": KICAD_INPUTS, "min_major": 7},
    "pcb_export_odb": {"tool": "kicad-cli", "primary": {".kicad_pcb"}, "inputs": KICAD_INPUTS, "min_major": 9},
    "pcb_export_step": {"tool": "kicad-cli", "primary": {".kicad_pcb"}, "inputs": KICAD_INPUTS, "min_major": 7},
    "sch_export_bom": {"tool": "kicad-cli", "primary": {".kicad_sch"}, "inputs": KICAD_INPUTS, "min_major": 8},
    "spice_corners": {"tool": "ngspice", "primary": NETLIST_TOP, "inputs": NETLIST_ALL, "min_major": 30},
}
BOM_FIELDS = ("Reference,Value,Footprint,${QUANTITY},MPN,Manufacturer,${DNP},V_Rating,I_Rating,P_Rating",
              "Refs,Value,Footprint,Qty,MPN,Manufacturer,DNP,V_Rating,I_Rating,P_Rating")


class ToolError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class StepResult:
    argv: list[str]
    exit_code: Optional[int]
    timed_out: bool
    duration_s: float
    stdout_tail: str = ""
    stderr_tail: str = ""


@dataclass
class Output:
    path: str
    data: bytes
    step: int
    exit_code: Optional[int]


@dataclass
class RunResult:
    status: str                                    # SUCCEEDED | FAILED | TIMEOUT
    tool: str
    tool_version: Optional[str]
    steps: list[StepResult] = field(default_factory=list)
    outputs: list[Output] = field(default_factory=list)
    error: Optional[str] = None
    limits: dict = field(default_factory=dict)


# ----------------------------------------------------------------------------------------------
# input hygiene
# ----------------------------------------------------------------------------------------------
def safe_name(name: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(name.replace("\\", "/")))[:128].lstrip(".")
    if not base or base in (".", ".."):
        raise ToolError("INPUT_NAME_INVALID", f"{name!r} is not a usable file name")
    return base


def _ext(name: str) -> str:
    return Path(name.lower()).suffix


_PATHLIKE = re.compile(r"""(?:^|[\s"'=({,])(?:/[A-Za-z0-9_.~]|~/|\.\.[/\\]|[A-Za-z]:[\\/])""")


def check_netlist(name: str, text: str, allowed_includes: set[str], corner_params: set[str]) -> list[str]:
    """Static policy for untrusted SPICE input. Returns problems; empty means the file may run."""
    problems: list[str] = []
    lines = text.splitlines()
    first = next((ln for ln in lines if ln.strip()), "")
    if "ng_script" in first.lower():
        problems.append(f"{name}: *ng_script control files are not allowed")
    for no, raw in enumerate(lines, 1):
        ln = raw.strip()
        low = ln.lower()
        if not ln or ln.startswith("*") or ln.startswith(";"):
            continue
        body = low.split(";", 1)[0].split("$ ", 1)[0]
        where = f"{name}:{no}"
        if body.startswith((".control", ".endc")):
            problems.append(f"{where}: .control blocks are not allowed (they can execute shell commands)")
            continue
        if body.startswith((".include", ".inc ", ".lib")):
            toks = ln.split()
            target = toks[1].strip("'\"") if len(toks) > 1 else ""
            if body.startswith(".lib") and len(toks) == 2 and "." not in target:
                continue                                  # section marker inside a .lib file
            if target not in allowed_includes:
                problems.append(f"{where}: include of {target!r} is not a file of this job")
            continue
        if _PATHLIKE.search(body):
            problems.append(f"{where}: absolute, home or parent paths are not allowed")
        if body.startswith(".param"):
            for p in corner_params:
                if re.search(rf"(?<![a-z0-9_]){re.escape(p.lower())}\s*=", body):
                    problems.append(f"{where}: the netlist must not define corner parameter {p!r}; N3 sets it "
                                    "per corner")
        if body.startswith(".temp") or (body.startswith(".option") and re.search(r"\btemp\s*=", body)):
            problems.append(f"{where}: the netlist must not set the temperature; N3 sets it per corner")
    return problems


def corner_wrapper(corner: Corner, top: str) -> str:
    out = [f"* ATHENA N3 worst-case corner {corner.name}"]
    if corner.params:
        out.append(".param " + " ".join(f"{k}={v:.12g}" for k, v in sorted(corner.params.items())))
    out += [f".temp {corner.temp_c:.6g}", f".include {top}", ".end", ""]
    return "\n".join(out)


# ----------------------------------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------------------------------
class ToolRunner:
    def __init__(self, settings: N3Settings) -> None:
        self.s = settings
        if settings.workroot:
            os.makedirs(settings.workroot, mode=0o700, exist_ok=True)
        self.paths = {"kicad-cli": shutil.which(settings.kicad_cli), "ngspice": shutil.which(settings.ngspice)}
        self._prlimit = shutil.which("prlimit")
        self._unshare = self._probe_unshare() if settings.network_isolation != "off" else None
        self._versions: dict[str, tuple[float, Optional[str]]] = {}
        self._lock = threading.Lock()

    # -- environment ------------------------------------------------------------------------
    def _probe_unshare(self) -> Optional[str]:
        exe = shutil.which("unshare")
        if not exe:
            return None
        try:
            ok = subprocess.run([exe, "-rn", "true"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5, env={"PATH": "/usr/bin:/bin"}).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        return exe if ok else None

    def describe(self) -> dict:
        return {"tools": {t: {"path": p, "available": p is not None, "version": self.version(t) if p else None}
                          for t, p in self.paths.items()},
                "sandbox": self.limits()}

    def limits(self) -> dict:
        return {"timeout_s": self.s.job_timeout_s, "cpu_seconds": self.s.cpu_seconds, "memory_mb": self.s.memory_mb,
                "max_file_mb": self.s.max_file_mb, "max_open_files": self.s.max_open_files,
                "max_output_mb": self.s.max_output_mb, "max_output_files": self.s.max_output_files,
                "rlimits_via": "prlimit" if self._prlimit else "setrlimit",
                "network_namespace": bool(self._unshare), "network_isolation": self.s.network_isolation,
                "wrapper": list(self.s.sandbox_prefix), "shell": False, "env": ["PATH", "HOME", "LANG", "TMPDIR"]}

    def _wrap(self, argv: list[str]) -> list[str]:
        mb = 1024 * 1024
        pre: list[str] = []
        if self._prlimit:
            pre += [self._prlimit, f"--cpu={self.s.cpu_seconds}", f"--as={self.s.memory_mb * mb}",
                    f"--fsize={self.s.max_file_mb * mb}", f"--nofile={self.s.max_open_files}", "--"]
        if self._unshare:
            pre += [self._unshare, "-rn", "--"]
        return pre + list(self.s.sandbox_prefix) + argv

    def _preexec(self) -> None:                          # pragma: no cover - only without prlimit
        import resource
        mb = 1024 * 1024
        resource.setrlimit(resource.RLIMIT_CPU, (self.s.cpu_seconds, self.s.cpu_seconds))
        resource.setrlimit(resource.RLIMIT_AS, (self.s.memory_mb * mb, self.s.memory_mb * mb))
        resource.setrlimit(resource.RLIMIT_FSIZE, (self.s.max_file_mb * mb, self.s.max_file_mb * mb))
        resource.setrlimit(resource.RLIMIT_NOFILE, (self.s.max_open_files, self.s.max_open_files))

    def _exec(self, argv: list[str], workdir: Path, timeout: float, idx: int) -> StepResult:
        env = {"PATH": "/usr/bin:/bin", "HOME": str(workdir), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
               "TMPDIR": str(workdir / ".tmp"), "XDG_CONFIG_HOME": str(workdir / ".config"),
               "XDG_CACHE_HOME": str(workdir / ".cache")}
        (workdir / ".tmp").mkdir(exist_ok=True)
        out_p, err_p = workdir / f".step{idx}.stdout", workdir / f".step{idx}.stderr"
        t0 = time.monotonic()
        timed_out, code = False, None
        with open(out_p, "wb") as out, open(err_p, "wb") as err:
            try:
                proc = subprocess.Popen(self._wrap(argv), cwd=workdir, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                        stderr=err, start_new_session=True, close_fds=True,
                                        preexec_fn=None if self._prlimit else self._preexec)
            except OSError as exc:
                return StepResult(argv, None, False, 0.0, "", f"could not start: {exc}")
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                code = proc.wait()
        tail = lambda p: p.read_bytes()[-4000:].decode("utf-8", "replace")  # noqa: E731
        return StepResult(argv, code, timed_out, round(time.monotonic() - t0, 3), tail(out_p), tail(err_p))

    def version(self, tool: str) -> Optional[str]:
        exe = self.paths.get(tool)
        if exe is None:
            return None
        mtime = os.stat(exe).st_mtime
        with self._lock:
            cached = self._versions.get(tool)
            if cached and cached[0] == mtime:
                return cached[1]
        argv = [exe, "version"] if tool == "kicad-cli" else [exe, "--version"]
        work = Path(tempfile.mkdtemp(prefix="n3ver-", dir=self.s.workroot))
        try:
            r = self._exec(argv, work, 30, 0)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        text = r.stdout_tail + "\n" + r.stderr_tail
        m = re.search(r"ngspice-(\d+(?:\.\d+)*)", text) if tool == "ngspice" else \
            re.search(r"(\d+\.\d+(?:\.\d+)?(?:[-+~][\w.]+)?)", text)
        v = m.group(1) if m and r.exit_code == 0 else None
        with self._lock:
            self._versions[tool] = (mtime, v)
        return v

    # -- jobs -------------------------------------------------------------------------------
    def plan(self, operation: str, names: list[str], corners: list[Corner]) -> tuple[list[list[str]], dict]:
        """Validate inputs and return the argv list (relative to the work dir) without running anything."""
        if operation not in OPERATIONS:
            raise ToolError("OPERATION_NOT_ALLOWED", f"{operation} is not an allowed operation")
        spec = OPERATIONS[operation]
        exe = self.paths.get(spec["tool"])
        if exe is None:
            raise ToolError("TOOL_UNAVAILABLE", f"{spec['tool']} is not installed or not on the configured path")
        if self.s.network_isolation == "required" and not self._unshare:
            raise ToolError("SANDBOX_UNAVAILABLE", "network isolation is required but unshare -rn is unavailable")
        for n in names:
            if _ext(n) not in spec["inputs"]:
                raise ToolError("INPUT_NOT_ALLOWED", f"{n} is not an allowed input for {operation}")
        primary = [n for n in names if _ext(n) in spec["primary"]]
        if not primary:
            raise ToolError("PRIMARY_INPUT_MISSING", f"{operation} needs a {'/'.join(sorted(spec['primary']))} input")
        top = primary[0]
        stem = Path(top).stem
        k = [exe]
        if operation == "sch_erc":
            return [k + ["sch", "erc", "--format", "json", "--severity-all", "--output", "erc.json", top]], {}
        if operation == "pcb_drc":
            return [k + ["pcb", "drc", "--format", "json", "--severity-all", "--schematic-parity", "--output",
                         "drc.json", top]], {}
        if operation == "pcb_export_gerbers":
            return [k + ["pcb", "export", "gerbers", "--output", "gerbers/", top]], {"dirs": ["gerbers"]}
        if operation == "pcb_export_drill":
            return [k + ["pcb", "export", "drill", "--output", "drill/", top]], {"dirs": ["drill"]}
        if operation == "pcb_export_odb":
            return [k + ["pcb", "export", "odb", "--compression", "zip", "--output", f"{stem}-odb.zip", top]], {}
        if operation == "pcb_export_step":
            return [k + ["pcb", "export", "step", "--output", f"{stem}.step", top]], {}
        if operation == "sch_export_bom":
            return [k + ["sch", "export", "bom", "--output", "bom.csv", "--fields", BOM_FIELDS[0], "--labels",
                         BOM_FIELDS[1], "--group-by", "Value,Footprint,MPN", top]], {}
        # spice_corners
        if not corners:
            raise ToolError("CORNERS_REQUIRED", "spice_corners needs at least one corner")
        if len(corners) > self.s.max_corners:
            raise ToolError("TOO_MANY_CORNERS", f"at most {self.s.max_corners} corners per job")
        if len({c.name for c in corners}) != len(corners):
            raise ToolError("DUPLICATE_CORNER", "corner names must be unique")
        wrappers = {f"corner_{c.name}.cir": corner_wrapper(c, top) for c in corners}
        argv = [k + ["-b", "-o", f"corner_{c.name}.log", f"corner_{c.name}.cir"]
                for c in corners]
        return argv, {"wrappers": wrappers}

    def preflight(self, operation: str, inputs: list[tuple[str, bytes]], corners: list[Corner]
                  ) -> tuple[list[str], list[list[str]], dict]:
        """Everything that can be refused before a job is queued: names, allowlist, netlist policy."""
        names = [safe_name(n) for n, _ in inputs]
        if len(set(names)) != len(names):
            raise ToolError("DUPLICATE_INPUT", "two inputs map to the same file name")
        if operation == "spice_corners":
            params = {k for c in corners for k in c.params}
            problems = [p for (_, d), sn in zip(inputs, names) for p in
                        check_netlist(sn, d.decode("utf-8", "replace"), set(names) - {sn}, params)]
            if problems:
                raise ToolError("NETLIST_POLICY", "; ".join(problems[:20]))
        steps, extra = self.plan(operation, names, corners)
        return names, steps, extra

    def run(self, operation: str, inputs: list[tuple[str, bytes]], corners: Optional[list[Corner]] = None) -> RunResult:
        corners = corners or []
        names, steps, extra = self.preflight(operation, inputs, corners)
        spec = OPERATIONS[operation]
        tool = spec["tool"]
        version = self.version(tool)
        major = int(re.match(r"(\d+)", version).group(1)) if version and re.match(r"\d+", version) else None
        result = RunResult("FAILED", tool, version, limits=self.limits())
        if major is None:
            result.error = f"could not determine the {tool} version"
            return result
        if major < spec["min_major"]:
            result.error = (f"{tool} {version} cannot run {operation}; version {spec['min_major']}+ is required "
                            "(kicad-cli sch erc / pcb drc need KiCad 8, ODB++ export needs KiCad 9)")
            return result
        work = Path(tempfile.mkdtemp(prefix="n3job-", dir=self.s.workroot))
        os.chmod(work, 0o700)
        try:
            for n, (_, data) in zip(names, inputs):
                (work / n).write_bytes(data)
            for d in extra.get("dirs", []):
                (work / d).mkdir()
            for n, text in extra.get("wrappers", {}).items():
                (work / n).write_text(text)
            deadline = time.monotonic() + self.s.job_timeout_s
            for i, argv in enumerate(steps):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    result.status, result.error = "TIMEOUT", f"job exceeded {self.s.job_timeout_s}s"
                    break
                r = self._exec(argv, work, remaining, i)
                result.steps.append(r)
                if r.timed_out:
                    result.status, result.error = "TIMEOUT", f"step {i} exceeded the {self.s.job_timeout_s}s limit"
                    break
                if r.exit_code is None:
                    result.error = r.stderr_tail
                    break
            else:
                result.status = "SUCCEEDED"
            skip = set(names) | set(extra.get("wrappers", {}))
            result.outputs = self._collect(work, operation, skip, result)
            if result.status == "SUCCEEDED" and not result.outputs:
                result.status = "FAILED"
                result.error = (f"{tool} produced no {operation} output (exit {result.steps[-1].exit_code}): "
                                f"{(result.steps[-1].stderr_tail or result.steps[-1].stdout_tail)[-500:]}")
        finally:
            if not self.s.keep_workdirs:
                shutil.rmtree(work, ignore_errors=True)
        return result

    def _expected(self, operation: str, rel: str) -> bool:
        p = Path(rel)
        ext = p.suffix.lower()
        if operation == "sch_erc":
            return rel == "erc.json"
        if operation == "pcb_drc":
            return rel == "drc.json"
        if operation == "pcb_export_gerbers":
            return p.parent.name == "gerbers" and len(p.parts) == 2 and bool(GERBER_RE.match(ext))
        if operation == "pcb_export_drill":
            return p.parent.name == "drill" and len(p.parts) == 2 and ext in (".drl", ".xln", ".gbr")
        if operation == "pcb_export_odb":
            return len(p.parts) == 1 and rel.endswith("-odb.zip")
        if operation == "pcb_export_step":
            return len(p.parts) == 1 and ext in (".step", ".stp")
        if operation == "sch_export_bom":
            return rel == "bom.csv"
        return len(p.parts) == 1 and rel.startswith("corner_") and ext in (".log", ".raw")

    def _collect(self, work: Path, operation: str, skip: set[str], result: RunResult) -> list[Output]:
        root = work.resolve()
        out, total = [], 0
        cap_total, cap_n = self.s.max_output_mb * 1024 * 1024, self.s.max_output_files
        step_of = {}
        for i, s in enumerate(result.steps):
            for tok in s.argv:
                step_of.setdefault(tok, i)
        for dirpath, dirnames, filenames in os.walk(work, followlinks=False):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for fn in sorted(filenames):
                full = Path(dirpath) / fn
                rel = full.relative_to(work).as_posix()
                if rel in skip or fn.startswith(".") or not self._expected(operation, rel):
                    continue
                st = os.lstat(full)
                if not stat.S_ISREG(st.st_mode) or not full.resolve().is_relative_to(root):
                    continue
                if len(out) >= cap_n or total + st.st_size > cap_total:
                    result.error = (result.error or "") + f" output cap reached at {rel}"
                    return out
                total += st.st_size
                idx = step_of.get(rel, 0)
                code = result.steps[idx].exit_code if idx < len(result.steps) else None
                out.append(Output(rel, full.read_bytes(), idx, code))
        return out


# ----------------------------------------------------------------------------------------------
# asynchronous worker
# ----------------------------------------------------------------------------------------------
class JobWorker:
    """Runs job callables on a bounded thread pool (or inline). The callable owns persistence of
    its own status transitions, so a request returns as soon as the job is queued."""

    def __init__(self, max_workers: int, inline: bool = False) -> None:
        self.inline = inline
        self._pool = None if inline else ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="n3-job")
        self._futures: dict[str, Future] = {}
        self._lock = threading.Lock()

    def submit(self, job_id: str, fn: Callable[[], None]) -> None:
        if self._pool is None:
            fn()
            return
        fut = self._pool.submit(fn)
        with self._lock:
            self._futures[job_id] = fut

    def wait(self, job_id: str, timeout: Optional[float] = None) -> bool:
        with self._lock:
            fut = self._futures.get(job_id)
        if fut is None:
            return True
        fut.result(timeout=timeout)
        return True

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=True)

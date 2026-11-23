"""Shallow-clone / release-tarball fetcher (KiCad libraries, OpenROAD, CircuitNet, DeepPCB, CycloneDX,
PDEBench, Fusion 360 Gallery, SKY130, EPFL benchmarks).

* ``mode: clone`` — ``git clone --depth 1 --single-branch --branch <ref>`` over HTTPS only
  (``protocol.allow=never`` + ``protocol.https.allow=always``), TLS verification forced on,
  symlinks checked out as plain files, optional sparse checkout. ``.git`` is removed and the
  commit id is recorded in the version tag (``<ref>+g<commit12>``).
* ``mode: tarball`` — downloads the release archive over HTTPS and extracts it with the
  traversal/symlink/zip-bomb guards from ``base.safe_extract``.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

from ..policy_gates import PolicyViolation
from .base import BaseFetcher, FetchError, safe_extract


class GitFetcher(BaseFetcher):
    access = ("git",)
    fetch_method = "git_clone"

    def __init__(self, *args, runner: Callable = subprocess.run, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.runner = runner

    def plan(self) -> dict:
        g = self.spec.git
        return {**super().plan(), "repo": g.repo, "ref": g.ref, "mode": g.mode, "sparse_paths": g.sparse_paths,
                "command": self.clone_command(Path("<staging>/clone")) if g.mode == "clone" else None}

    # -- clone ---------------------------------------------------------------------------------
    def clone_command(self, dest: Path) -> list[str]:
        g = self.spec.git
        cmd = ["git", "-c", "http.sslVerify=true", "-c", "protocol.allow=never", "-c", "protocol.https.allow=always",
               "-c", "core.symlinks=false", "-c", "advice.detachedHead=false", "clone", "--depth", "1",
               "--single-branch", "--no-tags", "--branch", g.ref]
        if g.sparse_paths:
            cmd += ["--filter=blob:none", "--sparse"]
        return cmd + [g.repo, str(dest)]

    def _git_env(self) -> dict:
        env = {k: v for k, v in os.environ.items() if k != "GIT_SSL_NO_VERIFY"}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="/bin/false", GCM_INTERACTIVE="never")
        return env

    def _run(self, cmd: list[str]) -> subprocess.CompletedProcess:
        try:
            res = self.runner(cmd, capture_output=True, text=True, timeout=3600, env=self._git_env(), check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise FetchError(f"{self.spec.source_id}: {' '.join(cmd[:3])}…: {exc}") from exc
        if res.returncode != 0:
            raise FetchError(f"{self.spec.source_id}: git failed ({res.returncode}): {(res.stderr or '').strip()[-500:]}")
        return res

    def _acquire(self, payload: Path) -> dict:
        g = self.spec.git
        self.check_url(g.repo)
        if g.mode == "tarball":
            return self._tarball(payload)
        work = payload.parent / "clone"
        self._run(self.clone_command(work))
        if g.sparse_paths:
            self._run(["git", "-C", str(work), "sparse-checkout", "set", *g.sparse_paths])
        commit = self._run(["git", "-C", str(work), "rev-parse", "HEAD"]).stdout.strip()
        if len(commit) < 12:
            raise FetchError(f"{self.spec.source_id}: could not read the cloned commit id")
        shutil.rmtree(work / ".git", ignore_errors=True)
        for p in work.rglob("*"):
            if p.is_symlink():
                p.unlink()
        for child in work.iterdir():
            shutil.move(str(child), str(payload / child.name))
        shutil.rmtree(work, ignore_errors=True)
        self.transport_log.update(tls_verified=True, final_host=urlparse(g.repo).hostname, commit=commit)
        return {"version_tag": f"{g.ref}+g{commit[:12]}", "fetch_method": "git_clone"}

    # -- tarball -------------------------------------------------------------------------------
    def tarball_url(self) -> str:
        g = self.spec.git
        if g.tarball_url:
            return g.tarball_url
        u = urlparse(g.repo)
        path = u.path.removesuffix(".git").strip("/")
        if u.hostname == "github.com":
            return f"https://github.com/{path}/archive/refs/tags/{g.ref}.tar.gz"
        if u.hostname == "gitlab.com":
            name = path.rsplit("/", 1)[-1]
            return f"https://gitlab.com/{path}/-/archive/{g.ref}/{name}-{g.ref}.tar.gz"
        raise PolicyViolation("HC-11", f"no tarball URL known for {g.repo}; set git.tarball_url", self.spec.source_id)

    def _tarball(self, payload: Path) -> dict:
        url = self.tarball_url()
        archive = payload.parent / "release.tar.gz"
        self.request("GET", url, stream_to=archive, expected_mime=["application/gzip", "application/x-tar"])
        extracted = payload.parent / "extracted"
        safe_extract(archive, extracted, self.max_bytes)
        roots = [p for p in extracted.iterdir()]
        src = roots[0] if len(roots) == 1 and roots[0].is_dir() else extracted
        for child in src.iterdir():
            shutil.move(str(child), str(payload / child.name))
        return {"version_tag": self.spec.git.ref, "fetch_method": "git_tarball"}

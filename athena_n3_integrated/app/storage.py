"""Object storage abstraction with write-once semantics.

Buckets:
  quarantine/  every upload and every submitted payload lands here first (immutable, SHA-256 keyed)
  released/    copies of blobs whose artifact reached PASS (immutable)

URIs are ``n0://<bucket>/<tenant>/<sha256>``. An object is never overwritten: writing the same
key with identical bytes is a no-op, different bytes raise ``ImmutableObjectError``.
Backends: ``LocalObjectStore`` (filesystem, files chmod 0444) and ``S3ObjectStore`` (conditional
``If-None-Match: *`` puts plus a SHA-256 checksum; enable S3 Object Lock on the bucket for WORM).
"""
from __future__ import annotations

import base64
import hashlib
import os
import re
import uuid
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Optional

BUCKETS = ("quarantine", "released")
_KEY = re.compile(r"^[A-Za-z0-9._:\-]+/[0-9a-f]{64}$")


class StorageError(Exception):
    pass


class ImmutableObjectError(StorageError):
    pass


def make_uri(bucket: str, key: str) -> str:
    return f"n0://{bucket}/{key}"


def parse_uri(uri: str) -> tuple[str, str]:
    m = re.match(r"^n0://(quarantine|released)/(.+)$", uri)
    if not m or not _KEY.match(m.group(2)):
        raise StorageError(f"not an N0 object URI: {uri}")
    return m.group(1), m.group(2)


class ObjectStore(ABC):
    backend = "abstract"

    @staticmethod
    def key_for(tenant_id: str, sha256: str) -> str:
        key = f"{tenant_id}/{sha256}"
        if not _KEY.match(key):
            raise StorageError(f"invalid object key {key!r}")
        return key

    @abstractmethod
    def put_immutable(self, bucket: str, key: str, data: bytes) -> str: ...

    @abstractmethod
    def get(self, bucket: str, key: str) -> bytes: ...

    @abstractmethod
    def exists(self, bucket: str, key: str) -> bool: ...

    def copy(self, src_bucket: str, key: str, dst_bucket: str) -> str:
        return self.put_immutable(dst_bucket, key, self.get(src_bucket, key))

    @abstractmethod
    def health(self) -> dict: ...


class LocalObjectStore(ObjectStore):
    backend = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        for b in BUCKETS:
            (self.root / b).mkdir(parents=True, exist_ok=True)

    def _path(self, bucket: str, key: str) -> Path:
        if bucket not in BUCKETS or not _KEY.match(key):
            raise StorageError(f"invalid object location {bucket}/{key}")
        p = (self.root / bucket / key).resolve()
        if not p.is_relative_to(self.root / bucket):
            raise StorageError("object path escapes its bucket")
        return p

    def put_immutable(self, bucket: str, key: str, data: bytes) -> str:
        p = self._path(bucket, key)
        if p.exists():
            if hashlib.sha256(p.read_bytes()).digest() != hashlib.sha256(data).digest():
                raise ImmutableObjectError(f"{bucket}/{key} already exists with different content")
            return make_uri(bucket, key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(f".{p.name}.{uuid.uuid4().hex[:8]}")
        with open(tmp, "xb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o444)
        try:
            os.link(tmp, p)                      # atomic, fails if another writer won the race
        except FileExistsError:
            tmp.unlink(missing_ok=True)
            return self.put_immutable(bucket, key, data)
        tmp.unlink(missing_ok=True)
        return make_uri(bucket, key)

    def get(self, bucket: str, key: str) -> bytes:
        p = self._path(bucket, key)
        if not p.exists():
            raise StorageError(f"{bucket}/{key} not found")
        return p.read_bytes()

    def exists(self, bucket: str, key: str) -> bool:
        return self._path(bucket, key).exists()

    def health(self) -> dict:
        probe = self.root / "quarantine" / f".health-{uuid.uuid4().hex[:6]}"
        try:
            probe.write_bytes(b"ok")
            probe.unlink()
            return {"ok": True, "backend": self.backend, "root": str(self.root)}
        except OSError as exc:
            return {"ok": False, "backend": self.backend, "error": str(exc)}


class S3ObjectStore(ObjectStore):  # pragma: no cover - needs an S3 endpoint
    backend = "s3"

    def __init__(self, bucket: str, prefix: str = "athena-n0", client=None) -> None:
        if client is None:
            import boto3
            client = boto3.client("s3")
        self.client, self.bucket, self.prefix = client, bucket, prefix.strip("/")

    def _k(self, bucket: str, key: str) -> str:
        if bucket not in BUCKETS or not _KEY.match(key):
            raise StorageError(f"invalid object location {bucket}/{key}")
        return f"{self.prefix}/{bucket}/{key}"

    def put_immutable(self, bucket: str, key: str, data: bytes) -> str:
        from botocore.exceptions import ClientError
        checksum = base64.b64encode(hashlib.sha256(data).digest()).decode()
        try:
            self.client.put_object(Bucket=self.bucket, Key=self._k(bucket, key), Body=data, IfNoneMatch="*",
                                   ChecksumSHA256=checksum)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("PreconditionFailed", "ConditionalRequestConflict"):
                if self.get(bucket, key) != data:
                    raise ImmutableObjectError(f"{bucket}/{key} already exists with different content")
            else:
                raise StorageError(str(exc)) from exc
        return make_uri(bucket, key)

    def get(self, bucket: str, key: str) -> bytes:
        return self.client.get_object(Bucket=self.bucket, Key=self._k(bucket, key))["Body"].read()

    def exists(self, bucket: str, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            self.client.head_object(Bucket=self.bucket, Key=self._k(bucket, key))
            return True
        except ClientError:
            return False

    def health(self) -> dict:
        try:
            self.client.head_bucket(Bucket=self.bucket)
            return {"ok": True, "backend": self.backend, "bucket": self.bucket}
        except Exception as exc:
            return {"ok": False, "backend": self.backend, "error": str(exc)}


def make_store(settings) -> ObjectStore:
    if settings.storage_backend == "s3":
        if not settings.s3_bucket:
            raise StorageError("N0_S3_BUCKET is required for the s3 backend")
        return S3ObjectStore(settings.s3_bucket, settings.s3_prefix)
    return LocalObjectStore(settings.storage_root)

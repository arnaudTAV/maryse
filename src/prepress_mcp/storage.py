"""Per-tenant filesystem isolation for report artifacts.

Every tenant gets its own subtree under ``storage_root``. Tenant IDs and job
IDs are strictly validated so a caller can never traverse outside their own
directory (no ``..``, no separators, no absolute paths).
"""
from __future__ import annotations

import re
import uuid
from pathlib import Path

_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class StorageError(RuntimeError):
    pass


def _safe(component: str, kind: str) -> str:
    if not _SAFE.match(component):
        raise StorageError(f"unsafe {kind}: {component!r}")
    return component


def new_job_id() -> str:
    return uuid.uuid4().hex


def tenant_root(storage_root: Path, tenant_id: str) -> Path:
    return storage_root / _safe(tenant_id, "tenant_id")


def report_dir(storage_root: Path, tenant_id: str, job_id: str, *, create: bool = True) -> Path:
    d = tenant_root(storage_root, tenant_id) / "reports" / _safe(job_id, "job_id")
    # Defence in depth: the resolved path must stay within the tenant root.
    troot = tenant_root(storage_root, tenant_id).resolve()
    if not str(d.resolve()).startswith(str(troot)):
        raise StorageError("path escapes tenant root")
    if create:
        d.mkdir(parents=True, exist_ok=True)
    return d

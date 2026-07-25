"""Short-lived, HMAC-signed links for report downloads."""
from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode


def _message(tenant_id: str, job_id: str, filename: str, expires: int) -> bytes:
    return f"{tenant_id}\n{job_id}\n{filename}\n{expires}".encode("utf-8")


def _signature(secret: str, tenant_id: str, job_id: str, filename: str, expires: int) -> str:
    return hmac.new(
        secret.encode("utf-8"), _message(tenant_id, job_id, filename, expires), hashlib.sha256
    ).hexdigest()


def build_signed_url(
    base_url: str,
    secret: str,
    tenant_id: str,
    job_id: str,
    filename: str,
    *,
    ttl_s: int,
) -> str:
    if ttl_s <= 0:
        raise ValueError("report URL TTL must be positive")
    expires = int(time.time()) + ttl_s
    sig = _signature(secret, tenant_id, job_id, filename, expires)
    path = f"/reports/{tenant_id}/{job_id}/{filename}"
    return f"{base_url.rstrip('/')}{path}?{urlencode({'expires': expires, 'sig': sig})}"


def verify_signed_url(
    secret: str,
    tenant_id: str,
    job_id: str,
    filename: str,
    expires: str | None,
    sig: str | None,
) -> bool:
    if not expires or not sig:
        return False
    try:
        expiry = int(expires)
    except ValueError:
        return False
    if expiry < int(time.time()):
        return False
    expected = _signature(secret, tenant_id, job_id, filename, expiry)
    return hmac.compare_digest(expected, sig)

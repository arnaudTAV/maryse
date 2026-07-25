"""Safe download of a caller-supplied signed HTTPS URL.

The tool receives an opaque URL, so this module is the trust boundary. It
enforces: HTTPS only, no credentials in the URL, an SSRF guard that resolves
the host and blocks private/loopback/link-local ranges, a hard byte cap
enforced while streaming, and a request timeout.
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx


class FetchError(RuntimeError):
    pass


@dataclass
class FetchResult:
    content: bytes
    content_type: str | None
    final_url: str


def _assert_public_host(host: str, allow_private: bool) -> None:
    if allow_private:
        return
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as e:
        raise FetchError(f"cannot resolve host: {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise FetchError(f"host resolves to non-public address: {ip}")


def validate_url(url: str, *, allow_private: bool = False) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise FetchError("only https URLs are accepted")
    if not parts.hostname:
        raise FetchError("URL has no host")
    if parts.username or parts.password:
        raise FetchError("credentials in URL are not allowed")
    _assert_public_host(parts.hostname, allow_private)
    return url


def fetch_pdf(
    url: str,
    *,
    max_bytes: int,
    timeout_s: float,
    allow_private: bool = False,
) -> FetchResult:
    validate_url(url, allow_private=allow_private)
    chunks: list[bytes] = []
    total = 0
    try:
        with httpx.Client(timeout=timeout_s, follow_redirects=False) as client:
            with client.stream("GET", url) as resp:
                if resp.status_code != 200:
                    raise FetchError(f"source returned HTTP {resp.status_code}")
                ctype = resp.headers.get("content-type", "").split(";")[0].strip() or None
                declared = resp.headers.get("content-length")
                if declared and int(declared) > max_bytes:
                    raise FetchError(f"source too large: {declared} bytes > {max_bytes}")
                for chunk in resp.iter_bytes(chunk_size=65536):
                    total += len(chunk)
                    if total > max_bytes:
                        raise FetchError(f"source exceeded {max_bytes} bytes while streaming")
                    chunks.append(chunk)
                final_url = str(resp.url)
    except httpx.HTTPError as e:
        raise FetchError(f"download failed: {e}") from e

    content = b"".join(chunks)
    if not content.startswith(b"%PDF-"):
        raise FetchError("fetched content is not a PDF (missing %PDF- header)")
    return FetchResult(content=content, content_type=ctype, final_url=final_url)

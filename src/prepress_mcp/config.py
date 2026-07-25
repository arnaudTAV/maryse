"""Runtime settings and tenant registry loading.

Settings come from environment variables; the tenant registry is a TOML file
mapping bearer tokens to tenant IDs. Everything is loaded once at startup and
cached, so config errors surface immediately rather than on the first request.
"""
from __future__ import annotations

import os
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # backport for 3.10
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when settings or the tenant registry are invalid."""


@dataclass(frozen=True)
class Tenant:
    tenant_id: str
    name: str
    token: str
    dpi_threshold: int | None = None  # per-tenant override of Settings default


@dataclass(frozen=True)
class Settings:
    storage_root: Path
    tenants_file: Path
    default_dpi_threshold: int
    max_pdf_bytes: int
    fetch_timeout_s: float
    allow_private_hosts: bool
    public_base_url: str | None  # e.g. https://prepress.example.com; used to build report URLs
    report_signing_secret: str | None
    report_url_ttl_s: int
    mount_path: str

    @staticmethod
    def from_env() -> "Settings":
        return Settings(
            storage_root=Path(os.getenv("PREPRESS_STORAGE_ROOT", "/srv/prepress/tenants")),
            tenants_file=Path(os.getenv("PREPRESS_TENANTS_FILE", "tenants.toml")),
            default_dpi_threshold=int(os.getenv("PREPRESS_DPI_THRESHOLD", "300")),
            max_pdf_bytes=int(os.getenv("PREPRESS_MAX_PDF_BYTES", str(200 * 1024 * 1024))),
            fetch_timeout_s=float(os.getenv("PREPRESS_FETCH_TIMEOUT_S", "30")),
            allow_private_hosts=os.getenv("PREPRESS_ALLOW_PRIVATE_HOSTS", "0") == "1",
            public_base_url=os.getenv("PREPRESS_PUBLIC_BASE_URL") or None,
            report_signing_secret=os.getenv("PREPRESS_REPORT_SIGNING_SECRET") or None,
            report_url_ttl_s=int(os.getenv("PREPRESS_REPORT_URL_TTL_S", "3600")),
            mount_path=os.getenv("PREPRESS_MOUNT_PATH", "/mcp"),
        )


@dataclass(frozen=True)
class TenantRegistry:
    """Bearer-token -> Tenant lookup. Tokens are matched in constant time."""

    _by_token: dict[str, Tenant] = field(default_factory=dict)
    _by_id: dict[str, Tenant] = field(default_factory=dict)

    @staticmethod
    def load(path: Path) -> "TenantRegistry":
        if not path.exists():
            raise ConfigError(f"tenants file not found: {path}")
        data = tomllib.loads(path.read_text("utf-8"))
        tenants = data.get("tenants", {})
        if not tenants:
            raise ConfigError(f"no [tenants.*] entries in {path}")
        by_token: dict[str, Tenant] = {}
        by_id: dict[str, Tenant] = {}
        for tid, cfg in tenants.items():
            token = cfg.get("token")
            if not token or not isinstance(token, str):
                raise ConfigError(f"tenant '{tid}' missing string 'token'")
            if token in by_token:
                raise ConfigError(f"duplicate token for tenants '{by_token[token].tenant_id}' and '{tid}'")
            t = Tenant(
                tenant_id=tid,
                name=cfg.get("name", tid),
                token=token,
                dpi_threshold=cfg.get("dpi_threshold"),
            )
            by_token[token] = t
            by_id[tid] = t
        return TenantRegistry(_by_token=by_token, _by_id=by_id)

    def resolve(self, token: str) -> Tenant | None:
        import hmac

        # Constant-time compare against each known token to avoid leaking which
        # prefix matched via timing. Registries are small, so this is cheap.
        for known, tenant in self._by_token.items():
            if hmac.compare_digest(known, token):
                return tenant
        return None

    def get(self, tenant_id: str) -> Tenant | None:
        return self._by_id.get(tenant_id)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


@lru_cache(maxsize=1)
def get_registry() -> TenantRegistry:
    return TenantRegistry.load(get_settings().tenants_file)

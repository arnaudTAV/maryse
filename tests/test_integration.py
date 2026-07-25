"""End-to-end: ASGI auth gating + the tool function with a stubbed fetch."""
from __future__ import annotations

import secrets

import pytest
from starlette.testclient import TestClient

import prepress_mcp.config as config
import prepress_mcp.fetch as fetch_mod
import prepress_mcp.server as server
from prepress_mcp.auth import _current_tenant
from prepress_mcp.config import Tenant


@pytest.fixture
def env(tmp_path, monkeypatch):
    token = secrets.token_urlsafe(16)
    tfile = tmp_path / "tenants.toml"
    tfile.write_text(f'[tenants.acme]\nname="ACME"\ntoken="{token}"\ndpi_threshold=300\n')
    monkeypatch.setenv("PREPRESS_TENANTS_FILE", str(tfile))
    monkeypatch.setenv("PREPRESS_STORAGE_ROOT", str(tmp_path / "store"))
    # Clear cached settings/registry so env takes effect.
    config.get_settings.cache_clear()
    config.get_registry.cache_clear()
    return {"token": token, "root": tmp_path / "store"}


def test_auth_gating(env):
    app = server.build_app()
    with TestClient(app) as client:
        assert client.get("/healthz").text == "ok"
        # MCP endpoint requires a bearer token.
        assert client.get("/mcp").status_code == 401
        assert client.post("/mcp", json={}).status_code == 401
        # Report route is protected too.
        assert client.get("/reports/acme/x/report.json").status_code == 401


def test_tool_end_to_end(env, mixed_pdf_bytes, monkeypatch):
    # Stub the network fetch so the SSRF guard and real HTTP are bypassed.
    monkeypatch.setattr(
        fetch_mod,
        "fetch_pdf",
        lambda url, **kw: fetch_mod.FetchResult(mixed_pdf_bytes, "application/pdf", url),
    )
    monkeypatch.setattr(server, "fetch_pdf", fetch_mod.fetch_pdf)

    tenant = config.get_registry().get("acme")
    reset = _current_tenant.set(tenant)
    try:
        report = server.preflight_pdf_images_res("https://signed.example.com/a.pdf")
    finally:
        _current_tenant.reset(reset)

    assert report["status"] == "fail"
    assert report["summary"]["flagged"] == 1
    # Artifacts landed under the tenant's isolated directory.
    job = report["job_id"]
    rdir = env["root"] / "acme" / "reports" / job
    assert (rdir / "annotated.pdf").exists()
    assert (rdir / "report.json").exists()


def test_lowres_tool_end_to_end(env, mixed_pdf_bytes, monkeypatch):
    monkeypatch.setattr(
        fetch_mod,
        "fetch_pdf",
        lambda url, **kw: fetch_mod.FetchResult(mixed_pdf_bytes, "application/pdf", url),
    )
    monkeypatch.setattr(server, "fetch_pdf", fetch_mod.fetch_pdf)

    tenant = config.get_registry().get("acme")
    reset = _current_tenant.set(tenant)
    try:
        report = server.preflight_pdf_images_res_at_target_size(
            "https://signed.example.com/a.pdf", target_width_mm=210, target_height_mm=297, dpi_threshold=250
        )
    finally:
        _current_tenant.reset(reset)

    assert report["status"] == "fail"
    job = report["job_id"]
    rdir = env["root"] / "acme" / "reports" / job
    assert (rdir / "lowres_report.html").exists()
    assert (rdir / "lowres_report.json").exists()
    assert "data:image/png;base64," in (rdir / "lowres_report.html").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Classic REST endpoint: POST /api/v1/lowres-report
# ---------------------------------------------------------------------------

def test_rest_endpoint_requires_auth(env):
    app = server.build_app()
    with TestClient(app) as client:
        resp = client.post("/api/v1/lowres-report", json={"source_pdf_url": "https://x/y.pdf"})
        assert resp.status_code == 401


def test_rest_endpoint_multipart_upload(env, mixed_pdf_bytes):
    app = server.build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/lowres-report",
            headers={"Authorization": f"Bearer {env['token']}"},
            files={"file": ("test.pdf", mixed_pdf_bytes, "application/pdf")},
            data={"target_width_mm": "210", "target_height_mm": "297", "dpi_threshold": "250"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "fail"
        assert body["summary"]["flagged"] == 1
        assert "html_report_path" in body  # no PREPRESS_PUBLIC_BASE_URL set in env fixture


def test_rest_endpoint_rejects_non_pdf_upload(env):
    app = server.build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/lowres-report",
            headers={"Authorization": f"Bearer {env['token']}"},
            files={"file": ("test.pdf", b"not a pdf", "application/pdf")},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "bad_request"


def test_rest_endpoint_json_url_mode(env, mixed_pdf_bytes, monkeypatch):
    monkeypatch.setattr(
        fetch_mod,
        "fetch_pdf",
        lambda url, **kw: fetch_mod.FetchResult(mixed_pdf_bytes, "application/pdf", url),
    )
    monkeypatch.setattr(server, "fetch_pdf", fetch_mod.fetch_pdf)

    app = server.build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/lowres-report",
            headers={"Authorization": f"Bearer {env['token']}"},
            json={
                "source_pdf_url": "https://signed.example.com/a.pdf",
                "target_width_mm": 148,
                "target_height_mm": 210,
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["target_mm"]["width"] == 148.0


def test_rest_endpoint_no_file_or_url_is_400(env):
    app = server.build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/lowres-report",
            headers={"Authorization": f"Bearer {env['token']}"},
            json={"target_width_mm": 148, "target_height_mm": 210},
        )
        assert resp.status_code == 400


def test_rest_endpoint_unsupported_content_type_is_415(env):
    app = server.build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/lowres-report",
            headers={"Authorization": f"Bearer {env['token']}", "content-type": "text/plain"},
            content=b"whatever",
        )
        assert resp.status_code == 415


def test_rest_endpoint_bad_threshold_is_400(env, mixed_pdf_bytes):
    app = server.build_app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/v1/lowres-report",
            headers={"Authorization": f"Bearer {env['token']}"},
            files={"file": ("test.pdf", mixed_pdf_bytes, "application/pdf")},
            data={"dpi_threshold": "-5"},
        )
        assert resp.status_code == 400

"""FastMCP Streamable-HTTP server exposing the prepress image-resolution check.

One tool, ``preflight_pdf_images_res``, accepts a signed HTTPS URL to a source
PDF, runs the resolution preflight, writes a JSON report and a red-framed PDF
into the calling tenant's isolated report directory, and returns the report.

Artifacts are downloadable via an authenticated ``GET /reports/...`` route that
enforces the same bearer token and refuses cross-tenant access.

``POST /api/v1/lowres-report`` exposes the low-res-at-target-size check as a
plain REST endpoint (multipart upload or JSON URL body) for callers that are
not MCP clients, reusing the same bearer-token auth and tenant isolation.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse

from fastmcp import FastMCP

from . import storage
from .auth import AuthError, TenantAuthMiddleware, current_tenant
from .config import get_registry, get_settings
from .fetch import FetchError, fetch_pdf
from .lowres_report import TargetSizeError, run_lowres_report
from .preflight import run_preflight
from .signed_urls import build_signed_url, verify_signed_url

mcp: FastMCP = FastMCP(
    name="prepress-mcp",
    instructions=(
        "Professional prepress checks over PDF files. Call "
        "`preflight_pdf_images_res` with a signed HTTPS URL to a source PDF to "
        "verify that every placed image meets a minimum print resolution. The "
        "tool returns a structured report and a URL to a copy of the PDF with "
        "low-resolution images framed in red."
    ),
)


def _report_url(settings, tenant_id: str, job_id: str, filename: str) -> str | None:
    """Return a short-lived browser-safe report URL when public hosting is enabled."""
    if not settings.public_base_url:
        return None
    if not settings.report_signing_secret:
        raise RuntimeError("PREPRESS_REPORT_SIGNING_SECRET must be configured for public report URLs")
    return build_signed_url(
        settings.public_base_url,
        settings.report_signing_secret,
        tenant_id,
        job_id,
        filename,
        ttl_s=settings.report_url_ttl_s,
    )


def _as_mm(value: object, field: str) -> float:
    """Parse one required millimetre dimension from an external request."""
    if value is None or value == "":
        raise ValueError(f"{field} is required")
    try:
        return float(value)
    except (TypeError, ValueError) as e:
        raise ValueError(f"{field} must be a number in millimetres") from e


def _validated_target_mm(width_mm: float | None, height_mm: float | None) -> tuple[float, float]:
    """Return a finite, strictly-positive finished size in millimetres."""
    if width_mm is None or height_mm is None:
        raise ValueError("target_width_mm and target_height_mm are required")
    if not math.isfinite(width_mm) or not math.isfinite(height_mm):
        raise ValueError("target dimensions must be finite numbers")
    if width_mm <= 0 or height_mm <= 0:
        raise ValueError("target dimensions must be positive")
    return width_mm, height_mm


@mcp.tool
def preflight_pdf_images_res(
    source_pdf_url: str,
    dpi_threshold: int | None = None,
) -> dict:
    """Check the effective print resolution of every image in a PDF.

    Args:
        source_pdf_url: A signed HTTPS URL to the source PDF to inspect.
        dpi_threshold: Minimum acceptable effective resolution in dots per inch.
            Defaults to the tenant's configured threshold (typically 300 dpi).

    Returns:
        A structured report: overall pass/fail, per-image effective DPI, the
        list of images below threshold, and a link to the annotated PDF.
    """
    settings = get_settings()
    try:
        tenant = current_tenant()
    except AuthError as e:  # pragma: no cover - middleware normally guarantees this
        raise RuntimeError("unauthenticated request reached tool") from e

    threshold = dpi_threshold or tenant.dpi_threshold or settings.default_dpi_threshold
    if threshold <= 0:
        raise ValueError("dpi_threshold must be a positive integer")

    try:
        fetched = fetch_pdf(
            source_pdf_url,
            max_bytes=settings.max_pdf_bytes,
            timeout_s=settings.fetch_timeout_s,
            allow_private=settings.allow_private_hosts,
        )
    except FetchError as e:
        raise ValueError(f"could not fetch source PDF: {e}") from e

    job_id = storage.new_job_id()
    rdir = storage.report_dir(settings.storage_root, tenant.tenant_id, job_id)
    annotated = rdir / "annotated.pdf"

    report = run_preflight(
        fetched.content,
        threshold=threshold,
        annotated_out=annotated,
        job_id=job_id,
        source_url=fetched.final_url,
    )

    (rdir / "report.json").write_text(json.dumps(report, indent=2), "utf-8")

    if settings.public_base_url:
        report["report_url"] = _report_url(settings, tenant.tenant_id, job_id, "report.json")
        report["annotated_pdf_url"] = _report_url(settings, tenant.tenant_id, job_id, "annotated.pdf")
    else:
        report["annotated_pdf_path"] = str(annotated)

    return report


@mcp.tool
def preflight_pdf_images_res_at_target_size(
    source_pdf_url: str,
    target_width_mm: float,
    target_height_mm: float,
    dpi_threshold: int = 250,
) -> dict:
    """Report images that would be low-resolution after a homothetic resize.

    Many PDFs are authored at one size (e.g. A4) and imposed down to a
    smaller one (e.g. A5) at print time; an image can be acceptable at the
    authored size yet fall below print quality once scaled down. This tool
    recomputes each image's effective DPI as it would be after a uniform
    resize from the current trim size to the requested dimensions, and returns a
    lightweight HTML report with a thumbnail crop and page number for every
    image that would still fall below `dpi_threshold`.

    Args:
        source_pdf_url: A signed HTTPS URL to the source PDF to inspect.
        target_width_mm: Finished width, in millimetres.
        target_height_mm: Finished height, in millimetres.
        dpi_threshold: Minimum acceptable effective resolution, in dots per
            inch, at the target size. Defaults to 250 dpi.

    Returns:
        A structured report plus a link to a self-contained HTML page listing
        every flagged image with its thumbnail and page number.
    """
    settings = get_settings()
    try:
        tenant = current_tenant()
    except AuthError as e:  # pragma: no cover - middleware normally guarantees this
        raise RuntimeError("unauthenticated request reached tool") from e

    if dpi_threshold <= 0:
        raise ValueError("dpi_threshold must be a positive integer")
    target = _validated_target_mm(target_width_mm, target_height_mm)

    try:
        fetched = fetch_pdf(
            source_pdf_url,
            max_bytes=settings.max_pdf_bytes,
            timeout_s=settings.fetch_timeout_s,
            allow_private=settings.allow_private_hosts,
        )
    except FetchError as e:
        raise ValueError(f"could not fetch source PDF: {e}") from e

    job_id = storage.new_job_id()
    rdir = storage.report_dir(settings.storage_root, tenant.tenant_id, job_id)
    out_html = rdir / "lowres_report.html"

    try:
        report = run_lowres_report(
            fetched.content,
            target=target,
            threshold=dpi_threshold,
            out_html=out_html,
            job_id=job_id,
            source_url=fetched.final_url,
        )
    except TargetSizeError as e:
        raise ValueError(str(e)) from e

    (rdir / "lowres_report.json").write_text(json.dumps(report, indent=2), "utf-8")

    if settings.public_base_url:
        report["report_url"] = _report_url(settings, tenant.tenant_id, job_id, "lowres_report.json")
        report["html_report_url"] = _report_url(settings, tenant.tenant_id, job_id, "lowres_report.html")
    else:
        report["html_report_path"] = str(out_html)

    return report


@mcp.custom_route("/api/v1/lowres-report", methods=["POST"])
async def api_lowres_report(request: Request):
    """Classic REST endpoint for the low-res-at-target-size check.

    Mirrors the `preflight_pdf_images_res_at_target_size` MCP tool for callers
    that are not MCP clients (internal apps, scripts, curl/Postman). Accepts
    either a direct file upload or a signed URL, and always answers with a
    JSON body + an HTTP status code (no exceptions bubble up as 500s for
    ordinary input mistakes).

    multipart/form-data:
        file            - the PDF binary (required unless `source_pdf_url` given)
        target_width_mm - finished width in millimetres (required)
        target_height_mm - finished height in millimetres (required)
        dpi_threshold   - integer, default 250
        source_pdf_url  - alternative to `file`: a signed HTTPS URL

    application/json:
        {"source_pdf_url": "https://...", "target_width_mm": 148,
         "target_height_mm": 210, "dpi_threshold": 250}
    """
    settings = get_settings()
    try:
        tenant = current_tenant()
    except AuthError:
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    content_type = request.headers.get("content-type", "")
    pdf_bytes: bytes | None = None
    source_url: str | None = None
    source_name: str | None = None
    target_width_mm: float | None = None
    target_height_mm: float | None = None
    dpi_threshold = 250

    try:
        if content_type.startswith("multipart/form-data"):
            form = await request.form()
            target_width_mm = _as_mm(form.get("target_width_mm"), "target_width_mm")
            target_height_mm = _as_mm(form.get("target_height_mm"), "target_height_mm")
            dpi_threshold = int(form.get("dpi_threshold") or dpi_threshold)
            upload = form.get("file")
            if upload is not None and hasattr(upload, "read"):
                source_name = getattr(upload, "filename", None)
                pdf_bytes = await upload.read()
            elif form.get("source_pdf_url"):
                source_url = str(form["source_pdf_url"])
        elif content_type.startswith("application/json"):
            body = await request.json()
            source_url = body.get("source_pdf_url")
            target_width_mm = _as_mm(body.get("target_width_mm"), "target_width_mm")
            target_height_mm = _as_mm(body.get("target_height_mm"), "target_height_mm")
            dpi_threshold = int(body.get("dpi_threshold", dpi_threshold))
        else:
            return JSONResponse(
                {
                    "error": "unsupported_content_type",
                    "detail": "use multipart/form-data (file upload) or application/json (source_pdf_url)",
                },
                status_code=415,
            )
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": "bad_request", "detail": str(e)}, status_code=400)

    if pdf_bytes is None and not source_url:
        return JSONResponse(
            {"error": "bad_request", "detail": "provide a 'file' upload or a 'source_pdf_url'"},
            status_code=400,
        )
    if dpi_threshold <= 0:
        return JSONResponse(
            {"error": "bad_request", "detail": "dpi_threshold must be a positive integer"}, status_code=400
        )
    try:
        target = _validated_target_mm(target_width_mm, target_height_mm)
    except ValueError as e:
        return JSONResponse({"error": "bad_request", "detail": str(e)}, status_code=400)

    if pdf_bytes is not None:
        if len(pdf_bytes) > settings.max_pdf_bytes:
            return JSONResponse(
                {"error": "payload_too_large", "detail": f"file exceeds {settings.max_pdf_bytes} bytes"},
                status_code=413,
            )
        if not pdf_bytes.startswith(b"%PDF-"):
            return JSONResponse(
                {"error": "bad_request", "detail": "uploaded content is not a PDF"}, status_code=400
            )
    else:
        try:
            fetched = fetch_pdf(
                source_url,
                max_bytes=settings.max_pdf_bytes,
                timeout_s=settings.fetch_timeout_s,
                allow_private=settings.allow_private_hosts,
            )
        except FetchError as e:
            return JSONResponse({"error": "fetch_failed", "detail": str(e)}, status_code=400)
        pdf_bytes = fetched.content
        source_url = fetched.final_url

    job_id = storage.new_job_id()
    rdir = storage.report_dir(settings.storage_root, tenant.tenant_id, job_id)
    out_html = rdir / "lowres_report.html"

    try:
        report = run_lowres_report(
            pdf_bytes,
            target=target,
            threshold=dpi_threshold,
            out_html=out_html,
            job_id=job_id,
            source_url=source_url,
            source_name=source_name,
        )
    except TargetSizeError as e:
        return JSONResponse({"error": "bad_request", "detail": str(e)}, status_code=400)

    (rdir / "lowres_report.json").write_text(json.dumps(report, indent=2), "utf-8")

    if settings.public_base_url:
        report["report_url"] = _report_url(settings, tenant.tenant_id, job_id, "lowres_report.json")
        report["html_report_url"] = _report_url(settings, tenant.tenant_id, job_id, "lowres_report.html")
    else:
        report["html_report_path"] = str(out_html)

    return JSONResponse(report, status_code=200)


@mcp.custom_route("/reports/{tenant_id}/{job_id}/{filename}", methods=["GET"])
async def download_report(request: Request):
    """Serve a report artifact to its tenant or through a short-lived signed link."""
    settings = get_settings()
    tenant_id = request.path_params["tenant_id"]
    job_id = request.path_params["job_id"]
    filename = request.path_params["filename"]
    allowed = {"report.json", "annotated.pdf", "lowres_report.json", "lowres_report.html"}
    if filename not in allowed:
        return JSONResponse({"error": "not_found"}, status_code=404)

    expires = request.query_params.get("expires")
    signature = request.query_params.get("sig")
    signed = bool(expires or signature)
    if signed:
        if not settings.report_signing_secret or not verify_signed_url(
            settings.report_signing_secret, tenant_id, job_id, filename, expires, signature
        ):
            return JSONResponse({"error": "invalid_or_expired_signed_url"}, status_code=403)
    else:
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        if not token:
            return JSONResponse({"error": "missing_bearer_token"}, status_code=401)
        tenant = get_registry().resolve(token)
        if tenant is None:
            return JSONResponse({"error": "invalid_token"}, status_code=403)
        if tenant_id != tenant.tenant_id:
            return JSONResponse({"error": "forbidden"}, status_code=403)

    try:
        rdir = storage.report_dir(
            settings.storage_root, tenant_id, job_id, create=False
        )
    except storage.StorageError:
        return JSONResponse({"error": "not_found"}, status_code=404)

    target = rdir / filename
    if not target.exists():
        return JSONResponse({"error": "not_found"}, status_code=404)
    if filename.endswith(".pdf"):
        media = "application/pdf"
    elif filename.endswith(".html"):
        media = "text/html"
    else:
        media = "application/json"
    return FileResponse(str(target), media_type=media, filename=filename)


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(_request: Request):
    return PlainTextResponse("ok")


def build_app():
    """Return the ASGI app with tenant auth wired in."""
    settings = get_settings()
    registry = get_registry()
    auth = Middleware(
        TenantAuthMiddleware,
        registry=registry,
        protected_prefixes=[settings.mount_path, "/api"],
    )
    return mcp.http_app(path=settings.mount_path, middleware=[auth])


def main() -> None:
    import uvicorn

    settings = get_settings()
    # Validate config early (raises on bad tenants file).
    get_registry()
    settings.storage_root.mkdir(parents=True, exist_ok=True)
    uvicorn.run(
        build_app(),
        host="127.0.0.1",
        port=int(__import__("os").getenv("PREPRESS_PORT", "8080")),
    )


app = None  # lazily built by ASGI servers via `application` factory below


def application(scope, receive, send):  # ASGI entrypoint: `uvicorn prepress_mcp.server:application`
    global app
    if app is None:
        app = build_app()
    return app(scope, receive, send)


if __name__ == "__main__":
    main()

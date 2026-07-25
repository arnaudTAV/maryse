# prepress-mcp

A self-hosted, multi-tenant server exposing professional prepress checks —
starting with **image resolution** — both as **MCP tools** (for MCP clients
like Claude) and as a **classic REST endpoint** (for plain HTTP callers:
internal apps, scripts, curl/Postman). Point it at a PDF and it returns a
structured JSON report plus a copy of the PDF (or a lightweight HTML page)
flagging every low-resolution image.

Built on FastMCP v3 (Streamable HTTP), PyMuPDF, and an open-source stack only.

## What the check does

For every *placed* raster image it computes the **effective DPI** — the image's
pixel dimensions divided by the physical size at which it is placed on the page:

```
effective_dpi = image_pixels / (placement_size_pt / 72)
```

This is what matters for print: a 4000-px photo scaled to fill an A2 poster can
still be under 150 dpi. Images whose effective DPI (limiting axis) falls below
the threshold — per tenant, default **300 dpi** — are reported and framed.

## Architecture

```
client ──HTTPS──> Caddy (auto-TLS) ──> uvicorn ──> FastMCP app
                                                     │
                          bearer token ─> TenantAuthMiddleware ─> tenant_id
                                                     │
                       preflight_pdf_images_res(source_pdf_url, dpi_threshold?)
                             │ fetch (SSRF-guarded) │ PyMuPDF analyse+annotate
                                                     ▼
              /srv/prepress/tenants/<tid>/reports/<job_id>/{report.json,annotated.pdf}
```

- **Transport:** FastMCP Streamable HTTP, mounted at `/mcp`.
- **Auth / multi-tenancy:** `Authorization: Bearer <token>` → tenant, resolved
  in constant time from `tenants.toml`. Unauthenticated requests get `401`.
- **Isolation:** report artifacts live under a per-tenant directory; tenant and
  job IDs are strictly validated (no path traversal). Downloads via
  `GET /reports/<tid>/<job>/<file>` are scoped to the authenticated tenant.
- **Fetch trust boundary:** HTTPS-only, no URL credentials, SSRF guard that
  resolves the host and blocks private/loopback/link-local ranges, a streamed
  byte cap, and a `%PDF-` sniff.

## The MCP tools

**`preflight_pdf_images_res(source_pdf_url, dpi_threshold=None)`** — every
placed image's effective DPI at the document's current size; returns a copy
of the PDF with low-resolution images framed in red.

```json
{
  "tool": "preflight_pdf_images_res",
  "job_id": "…",
  "status": "fail",
  "dpi_threshold": 300,
  "summary": {"pages": 1, "placements": 2, "flagged": 1, "distinct_images": 2},
  "flagged": [{"page": 1, "effective_dpi": 25.0, "pixel_width": 50, "bbox": [...], "ok": false}],
  "placements": [ ... ],
  "annotated_pdf_url": "https://prepress.example.com/reports/acme/<job>/annotated.pdf"
}
```

**`preflight_pdf_images_res_at_target_size(source_pdf_url, target_width_mm, target_height_mm, dpi_threshold=250)`**
— recomputes effective DPI as it would be after a homothetic resize from each
page's trim box to the requested finished width and height in millimetres.
Useful for any custom product format. Returns a
self-contained HTML report (thumbnail + page number per flagged image,
ordered by page) instead of another full-size PDF.

(`annotated_pdf_url`/`html_report_url`/`report_url` appear when
`PREPRESS_PUBLIC_BASE_URL` is set; otherwise a local file path is returned.)

## Classic REST endpoint

For callers that aren't MCP clients, the target-size check is also available
as plain HTTP — same bearer-token auth, same tenant isolation:

```
POST /api/v1/lowres-report
Authorization: Bearer <token>
```

Either upload the file directly (`multipart/form-data`, field `file`, plus
required `target_width_mm` / `target_height_mm` form fields and an optional
`dpi_threshold` field), or send a JSON body:

```bash
curl -X POST https://prepress.example.com/api/v1/lowres-report \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"source_pdf_url": "https://…/file.pdf", "target_width_mm": 148, "target_height_mm": 210, "dpi_threshold": 250}'
```

Ordinary mistakes (bad content-type, missing file/URL, non-PDF upload,
invalid threshold) come back as `4xx` JSON errors, not `500`s.

## Configuration (env vars)

| Variable | Default | Purpose |
|---|---|---|
| `PREPRESS_TENANTS_FILE` | `tenants.toml` | Bearer-token registry |
| `PREPRESS_STORAGE_ROOT` | `/srv/prepress/tenants` | Report artifact root |
| `PREPRESS_DPI_THRESHOLD` | `300` | Global default threshold |
| `PREPRESS_MAX_PDF_BYTES` | `209715200` | Hard download cap (200 MB) |
| `PREPRESS_FETCH_TIMEOUT_S` | `30` | Source download timeout |
| `PREPRESS_ALLOW_PRIVATE_HOSTS` | `0` | Set `1` only for local testing |
| `PREPRESS_PUBLIC_BASE_URL` | — | Public origin for report URLs |
| `PREPRESS_MOUNT_PATH` | `/mcp` | MCP endpoint path |
| `PREPRESS_PORT` | `8080` | Bind port |

## Run locally

```bash
pip install -r requirements.txt            # Python 3.11+
cp tenants.toml.example tenants.toml        # then paste real random tokens
python -c "import secrets; print(secrets.token_urlsafe(32))"
uvicorn prepress_mcp.server:application --host 127.0.0.1 --port 8080
```

## Deploy

- **`DEPLOY_HOSTINGER.md`** — step-by-step guide for a Hostinger (or any
  Ubuntu) VPS: Docker install, firewall, `docker compose up`, testing both the
  MCP endpoint and the REST endpoint, and the one-line upgrade to TLS once a
  domain is pointed at the box.
- `docker-compose.yml` + `Dockerfile` — the app container plus Caddy in front
  of it; the app is never exposed directly, only Caddy is internet-facing.
- `Caddyfile.ip` — bare-IP HTTP reverse proxy (no domain yet).
- `Caddyfile` — same, but keyed by domain name for automatic Let's Encrypt TLS.
- `systemd/prepress-mcp.service` — alternative non-Docker deploy: hardened
  systemd unit (read-only config, PrivateTmp) if you'd rather run uvicorn
  directly on the host.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

Covers the DPI math, flagging/thresholds, SSRF/URL validation, path-traversal
isolation, token resolution, ASGI auth gating (401s), and a full tool run that
writes an annotated PDF into an isolated tenant directory.

## Notes & limits

- Effective DPI uses the axis-aligned placement box; rotated images are measured
  conservatively (reported DPI ≤ true DPI), which is safe for flagging.
- `poppler` (`pdfinfo`/`pdfimages`) is installed for cross-checking and future
  checks; the resolution measurement itself is PyMuPDF-only.
- One check today (`image resolution`) by design; the tool layer is structured
  so additional `@mcp.tool` checks slot in without touching auth or storage.

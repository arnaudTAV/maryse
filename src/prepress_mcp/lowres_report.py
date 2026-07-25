"""Low-resolution image report at a target output size.

Prepress files often carry crop marks / bleed in the media box, so the true
"page size" for scaling purposes is the trim box, not the media box. This
module recomputes every placement's effective DPI as it would be *after* a
homothetic (uniform, aspect-preserving) resize from the current trim size to a
caller-supplied target size — e.g. checking whether images authored for A4
will still hold up once the document is imposed down to A5 — flags every
placement that still falls under ``threshold`` dpi at that target size, and
renders a self-contained HTML report with a thumbnail crop of each offending
image plus its page number.

The report is deliberately HTML rather than another annotated PDF: it is a
fraction of the size of re-embedding full-resolution source pages, and it
opens instantly in any browser without the large-PDF file-locking / security
handler issues that annotated production PDFs can trigger in desktop readers.
"""
from __future__ import annotations

import base64
import html as html_lib
import os
from dataclasses import asdict, dataclass
from pathlib import Path

import fitz  # PyMuPDF

from .preflight import analyze

MM_PER_PT = 25.4 / 72.0
PT_PER_MM = 72.0 / 25.4

# Common target formats, portrait, in millimetres. Callers can also pass an
# explicit (width_mm, height_mm) tuple for anything not listed here.
PRESETS_MM: dict[str, tuple[float, float]] = {
    "A3": (297.0, 420.0),
    "A4": (210.0, 297.0),
    "A5": (148.0, 210.0),
    "A6": (105.0, 148.0),
    "LETTER": (215.9, 279.4),
}


class TargetSizeError(ValueError):
    pass


def resolve_target_mm(target: str | tuple[float, float]) -> tuple[float, float]:
    """Accept a preset name (``"A5"``) or an explicit ``(width_mm, height_mm)``."""
    if isinstance(target, str):
        key = target.strip().upper()
        if key not in PRESETS_MM:
            raise TargetSizeError(f"unknown target format {target!r}; known: {sorted(PRESETS_MM)}")
        return PRESETS_MM[key]
    try:
        w, h = target
    except (TypeError, ValueError) as e:
        raise TargetSizeError("target must be a preset name or an (width_mm, height_mm) pair") from e
    if w <= 0 or h <= 0:
        raise TargetSizeError("target dimensions must be positive")
    return float(w), float(h)


@dataclass
class ScaledPlacement:
    page: int                      # 1-based
    xref: int
    bbox: list[float]              # placement rect on the CURRENT page, in points
    pixel_width: int
    pixel_height: int
    current_effective_dpi: float   # effective dpi at the document's current size
    target_effective_dpi: float    # effective dpi after homothetic resize to target
    scale_factor: float            # target / current (​<1 = shrink, dpi increases)
    colorspace: str | None
    ok: bool
    thumbnail_png_b64: str = ""    # populated only for flagged (not-ok) placements


def _page_basis_size(page: fitz.Page) -> tuple[float, float]:
    """Reference size for scaling: the trim box if present, else the media box.

    Crop-marked prepress PDFs inset the trim box from the media box; the media
    box (which includes the marks) is not the size the piece is actually
    printed/folded/bound at, so scaling against it would understate the real
    resolution gain from a reduction.
    """
    tb = page.trimbox
    if tb.width > 0 and tb.height > 0:
        return tb.width, tb.height
    return page.rect.width, page.rect.height


def _thumbnail_b64(page: fitz.Page, bbox: list[float], box_px: int = 220) -> str:
    """Render a crop of the page at the placement's bbox as a small PNG thumbnail."""
    rect = fitz.Rect(*bbox)
    w, h = max(rect.width, 1e-3), max(rect.height, 1e-3)
    zoom = max(min(box_px / w, box_px / h, 12.0), 0.05)
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=rect)
    return base64.b64encode(pix.tobytes("png")).decode("ascii")


def analyze_at_target(
    doc: fitz.Document,
    target: str | tuple[float, float],
    *,
    threshold: int = 250,
    with_thumbnails: bool = True,
) -> list[ScaledPlacement]:
    """Recompute every placement's effective DPI at a target output size.

    Scaling is homothetic and derived per-page from that page's trim box, so a
    document with mixed page sizes (e.g. a cover + inner pages) is handled
    correctly. If the target aspect ratio doesn't match a page's trim aspect
    ratio, the more conservative (smaller) of the two axis scale factors is
    used, since that under-states rather than over-states the resulting DPI.
    """
    target_w_mm, target_h_mm = resolve_target_mm(target)
    target_w_pt, target_h_pt = target_w_mm * PT_PER_MM, target_h_mm * PT_PER_MM

    base_placements = analyze(doc, threshold=0)  # threshold recomputed below at target size
    out: list[ScaledPlacement] = []
    for pl in base_placements:
        page = doc[pl.page - 1]
        cur_w, cur_h = _page_basis_size(page)
        scale = min(target_w_pt / cur_w, target_h_pt / cur_h)
        target_dpi = pl.effective_dpi / scale
        out.append(
            ScaledPlacement(
                page=pl.page,
                xref=pl.xref,
                bbox=pl.bbox,
                pixel_width=pl.pixel_width,
                pixel_height=pl.pixel_height,
                current_effective_dpi=pl.effective_dpi,
                target_effective_dpi=round(target_dpi, 1),
                scale_factor=round(scale, 4),
                colorspace=pl.colorspace,
                ok=target_dpi >= threshold,
            )
        )
    if with_thumbnails:
        for sp in out:
            if not sp.ok:
                sp.thumbnail_png_b64 = _thumbnail_b64(doc[sp.page - 1], sp.bbox)
    return out


_CSS = """
:root{--bad:#c0392b;--bad-bg:#fdecea;--ink:#1c1c1c;--muted:#666;--line:#e4e4e4}
*{box-sizing:border-box}
body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:var(--ink);
     margin:0;padding:32px;background:#fafafa}
h1{font-size:20px;margin:0 0 4px}
.meta{color:var(--muted);font-size:13px;margin-bottom:24px;line-height:1.6}
.meta b{color:var(--ink)}
.badge{display:inline-block;padding:2px 10px;border-radius:999px;font-size:12px;
       font-weight:600;background:var(--bad-bg);color:var(--bad);margin-left:6px}
table{border-collapse:collapse;width:100%;background:#fff;box-shadow:0 1px 3px rgba(0,0,0,.08)}
th,td{padding:10px 12px;border-bottom:1px solid var(--line);text-align:left;font-size:13px;vertical-align:middle}
th{background:#f3f3f3;font-weight:600;color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.04em}
td.thumb img{max-width:180px;max-height:140px;display:block;border:1px solid var(--line)}
td.dpi{font-variant-numeric:tabular-nums;font-weight:600}
td.dpi.bad{color:var(--bad)}
td.num{color:var(--muted);width:32px}
tr:hover{background:#fbfbfb}
.empty{padding:40px;text-align:center;color:var(--muted);background:#fff}
"""


def build_html_report(
    placements: list[ScaledPlacement],
    *,
    target_w_mm: float,
    target_h_mm: float,
    threshold: int,
    page_count: int,
    source_name: str | None,
    out_html: Path,
    target_label: str,
) -> None:
    flagged = sorted((p for p in placements if not p.ok), key=lambda p: (p.page, p.target_effective_dpi))

    if flagged:
        rows = "\n".join(
            f"""<tr>
  <td class="num">{i}</td>
  <td class="thumb"><img src="data:image/png;base64,{p.thumbnail_png_b64}" alt="page {p.page}"></td>
  <td>Page {p.page}</td>
  <td class="dpi bad">{p.target_effective_dpi:.0f}&nbsp;dpi</td>
</tr>"""
            for i, p in enumerate(flagged, 1)
        )
        dpi_header = html_lib.escape(f"DPI ({target_label})")
        table = f"""<table>
<thead><tr>
  <th>#</th><th>Vignette</th><th>Page</th>
  <th>{dpi_header}</th>
</tr></thead>
<tbody>{rows}</tbody>
</table>"""
    else:
        table = '<div class="empty">Aucune image sous le seuil au format cible — tout est bon.</div>'

    src = html_lib.escape(source_name) if source_name else "(fichier fourni)"
    doc_html = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<title>Rapport basse résolution — {target_w_mm:.0f}×{target_h_mm:.0f} mm</title>
<style>{_CSS}</style></head>
<body>
<h1>Rapport images basse résolution <span class="badge">{len(flagged)} image(s)</span></h1>
<div class="meta">
  Fichier&nbsp;: <b>{src}</b><br>
  Format cible&nbsp;: <b>{target_w_mm:.0f}&nbsp;&times;&nbsp;{target_h_mm:.0f}&nbsp;mm</b>
  (réduction homothétique depuis le trim box de chaque page)<br>
  Seuil&nbsp;: <b>{threshold}&nbsp;dpi</b> au format cible &middot;
  Pages&nbsp;: <b>{page_count}</b> &middot;
  Images évaluées&nbsp;: <b>{len(placements)}</b>
</div>
{table}
</body></html>"""

    out_html.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_html.with_name(out_html.name + ".tmp")
    tmp.write_text(doc_html, encoding="utf-8")
    os.replace(tmp, out_html)


def run_lowres_report(
    pdf_bytes: bytes,
    target: str | tuple[float, float],
    *,
    threshold: int = 250,
    out_html: Path,
    job_id: str,
    source_url: str | None = None,
    source_name: str | None = None,
) -> dict:
    """Run the target-size low-res check and write the HTML report. Returns JSON."""
    target_w_mm, target_h_mm = resolve_target_mm(target)
    # Prefer the caller's preset name ("A3") in the report; fall back to the
    # explicit mm size when a custom (width, height) tuple was passed.
    target_label = target.strip().upper() if isinstance(target, str) else f"{target_w_mm:.0f}×{target_h_mm:.0f} mm"
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        placements = analyze_at_target(doc, target, threshold=threshold, with_thumbnails=True)
        page_count = doc.page_count
        build_html_report(
            placements,
            target_w_mm=target_w_mm,
            target_h_mm=target_h_mm,
            threshold=threshold,
            page_count=page_count,
            source_name=source_name,
            out_html=out_html,
            target_label=target_label,
        )
    finally:
        doc.close()

    flagged = [p for p in placements if not p.ok]
    return {
        "tool": "lowres_report_at_target_size",
        "job_id": job_id,
        "source_url": source_url,
        "source_name": source_name,
        "status": "fail" if flagged else "pass",
        "target_mm": {"width": target_w_mm, "height": target_h_mm},
        "dpi_threshold": threshold,
        "summary": {
            "pages": page_count,
            "placements": len(placements),
            "flagged": len(flagged),
        },
        "flagged": [
            {k: v for k, v in asdict(p).items() if k != "thumbnail_png_b64"} for p in flagged
        ],
        "html_report": out_html.name,
    }

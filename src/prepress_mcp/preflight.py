"""Image-resolution preflight check + red-frame annotation.

For every placed raster image in the PDF we compute its *effective* resolution:
the pixel dimensions of the image divided by the physical size at which it is
placed on the page. An image can be high-resolution as a file yet render at a
low effective DPI if it is scaled up, which is what actually matters for print.

Effective DPI = image_pixels / (placement_size_in_points / 72).

Images whose effective DPI falls below the threshold are reported and framed in
red on a copy of the PDF.
"""
from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from pathlib import Path

import fitz  # PyMuPDF

RED = (1, 0, 0)
FRAME_WIDTH = 1.5


@dataclass
class Placement:
    page: int              # 1-based
    xref: int
    bbox: list[float]      # [x0, y0, x1, y1] in PDF points
    pixel_width: int
    pixel_height: int
    effective_dpi_x: float
    effective_dpi_y: float
    effective_dpi: float   # limiting (min) axis
    colorspace: str | None
    ok: bool


def _round(x: float) -> float:
    return float(round(x, 1))


def analyze(doc: fitz.Document, threshold: int) -> list[Placement]:
    """Measure the effective DPI of every placed raster image XObject.

    We enumerate the real image XObjects in each page's resources (deduplicated
    by xref) and locate every placement rectangle for each. This matches what a
    RIP actually rasterises and agrees with poppler's ``pdfimages -list``. It
    deliberately ignores PyMuPDF's ``xref == 0`` display-list entries, which are
    transparency-flattened proxies that would otherwise double-count an image.

    Known limitation: inline images (``BI``/``ID``/``EI`` in a content stream)
    are not XObjects and are not measured; these are rare and typically tiny.
    """
    results: list[Placement] = []
    for pindex in range(doc.page_count):
        page = doc[pindex]
        # xref -> (pixel_width, pixel_height, colorspace-name)
        images: dict[int, tuple[int, int, str | None]] = {}
        for im in page.get_images(full=True):
            xref = im[0]
            images.setdefault(xref, (int(im[2]), int(im[3]), im[5] or None))
        for xref, (px_w, px_h, cs) in images.items():
            if px_w <= 0 or px_h <= 0:
                continue
            for rect in page.get_image_rects(xref, transform=False):
                w_pts, h_pts = abs(rect.width), abs(rect.height)
                if w_pts <= 0 or h_pts <= 0:
                    continue
                dpi_x = px_w / (w_pts / 72.0)
                dpi_y = px_h / (h_pts / 72.0)
                eff = min(dpi_x, dpi_y)
                results.append(
                    Placement(
                        page=pindex + 1,
                        xref=xref,
                        bbox=[_round(rect.x0), _round(rect.y0), _round(rect.x1), _round(rect.y1)],
                        pixel_width=px_w,
                        pixel_height=px_h,
                        effective_dpi_x=_round(dpi_x),
                        effective_dpi_y=_round(dpi_y),
                        effective_dpi=_round(eff),
                        colorspace=cs,
                        ok=eff >= threshold,
                    )
                )
    return results


def annotate(doc: fitz.Document, flagged: list[Placement], out_path: Path) -> None:
    """Draw a red frame + DPI label around each flagged placement, then save."""
    for pl in flagged:
        page = doc[pl.page - 1]
        rect = fitz.Rect(*pl.bbox)
        page.draw_rect(rect, color=RED, width=FRAME_WIDTH)
        label = f"{int(pl.effective_dpi)} dpi"
        # Place the label just inside the top-left corner of the frame.
        page.insert_text(
            fitz.Point(rect.x0 + 2, max(rect.y0 + 9, 9)),
            label,
            fontsize=7,
            color=RED,
        )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_save(doc, out_path)


def _atomic_save(doc: fitz.Document, out_path: Path) -> None:
    """Write the PDF safely so a reader can always open the result.

    - Strip any inherited security handler (``encryption=PDF_ENCRYPT_NONE``) so
      source files carrying an owner-password/permission dict cannot make the
      output raise "Access denied" in Acrobat.
    - Write to a sibling ``*.tmp`` then ``os.replace`` onto the final name, so a
      reader (or a copy step) never sees a half-written multi-MB file.
    - Best-effort make the file group/other readable; ignored on filesystems
      that forbid chmod.
    """
    tmp = out_path.with_name(out_path.name + ".tmp")
    doc.save(str(tmp), garbage=3, deflate=True, encryption=fitz.PDF_ENCRYPT_NONE)
    try:
        os.chmod(tmp, 0o644)
    except OSError:
        pass
    os.replace(tmp, out_path)


def run_preflight(
    pdf_bytes: bytes,
    *,
    threshold: int,
    annotated_out: Path,
    job_id: str,
    source_url: str | None = None,
) -> dict:
    """Run the check and write the annotated PDF. Returns a structured report."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    try:
        placements = analyze(doc, threshold)
        flagged = [p for p in placements if not p.ok]
        annotate(doc, flagged, annotated_out)
        page_count = doc.page_count
    finally:
        doc.close()

    return {
        "tool": "preflight_pdf_images_res",
        "job_id": job_id,
        "source_url": source_url,
        "status": "fail" if flagged else "pass",
        "dpi_threshold": threshold,
        "summary": {
            "pages": page_count,
            "placements": len(placements),
            "flagged": len(flagged),
            "distinct_images": len({p.xref for p in placements}),
        },
        "flagged": [asdict(p) for p in flagged],
        "placements": [asdict(p) for p in placements],
        "annotated_pdf": annotated_out.name,
    }

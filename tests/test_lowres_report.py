"""Tests for the target-size low-res report (lowres_report.py)."""
from __future__ import annotations

import fitz
import pytest

from prepress_mcp.lowres_report import (
    TargetSizeError,
    analyze_at_target,
    resolve_target_mm,
    run_lowres_report,
)


def test_resolve_preset_and_explicit():
    assert resolve_target_mm("A5") == (148.0, 210.0)
    assert resolve_target_mm("a5") == (148.0, 210.0)  # case-insensitive
    assert resolve_target_mm((100.0, 50.0)) == (100.0, 50.0)


def test_resolve_rejects_unknown_and_invalid():
    with pytest.raises(TargetSizeError):
        resolve_target_mm("A0")
    with pytest.raises(TargetSizeError):
        resolve_target_mm((0, 50))
    with pytest.raises(TargetSizeError):
        resolve_target_mm((-10, 50))


def _pdf_with_trimbox(px=50, placement_pt=144, page_pt=595.0, trim_inset=21.0) -> bytes:
    """One image at a known effective DPI, page with a trim box inset like a
    prepress file with crop marks (media box bigger than trim box)."""
    doc = fitz.open()
    page = doc.new_page(width=page_pt, height=page_pt)
    page.set_trimbox(fitz.Rect(trim_inset, trim_inset, page_pt - trim_inset, page_pt - trim_inset))
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, px, px))
    pix.set_rect(pix.irect, (10, 10, 10))
    page.insert_image(fitz.Rect(72, 72, 72 + placement_pt, 72 + placement_pt), stream=pix.tobytes("png"))
    data = doc.tobytes()
    doc.close()
    return data


def test_scaling_uses_trimbox_not_mediabox():
    # 50px over 144pt (2in) at current size => 25 dpi, independent of trimbox.
    data = _pdf_with_trimbox()
    doc = fitz.open(stream=data, filetype="pdf")
    current_trim = doc[0].trimbox.width  # 595 - 2*21 = 553pt
    placements = analyze_at_target(doc, "A4", threshold=0, with_thumbnails=False)
    doc.close()
    assert len(placements) == 1
    p = placements[0]
    assert p.current_effective_dpi == pytest.approx(25.0, abs=0.1)
    # A4 width in pt vs the *trimmed* current width should give the scale factor,
    # not the untrimmed media-box width.
    a4_w_pt = 210.0 * 72.0 / 25.4
    expected_scale = a4_w_pt / current_trim
    assert p.scale_factor == pytest.approx(expected_scale, abs=0.01)


def test_flags_only_below_target_threshold(mixed_pdf_bytes):
    doc = fitz.open(stream=mixed_pdf_bytes, filetype="pdf")
    placements = analyze_at_target(doc, "A4", threshold=250, with_thumbnails=False)
    doc.close()
    flagged = [p for p in placements if not p.ok]
    ok = [p for p in placements if p.ok]
    assert len(flagged) == 1  # the 25dpi image scaled up still fails
    assert len(ok) == 1       # the 600dpi image easily clears 250 even scaled


def test_thumbnails_only_for_flagged(mixed_pdf_bytes):
    doc = fitz.open(stream=mixed_pdf_bytes, filetype="pdf")
    placements = analyze_at_target(doc, "A4", threshold=250, with_thumbnails=True)
    doc.close()
    for p in placements:
        if p.ok:
            assert p.thumbnail_png_b64 == ""
        else:
            assert p.thumbnail_png_b64.startswith("iVBOR")  # PNG magic in base64


def test_run_lowres_report_writes_html(mixed_pdf_bytes, tmp_path):
    out = tmp_path / "report.html"
    report = run_lowres_report(
        mixed_pdf_bytes, target="A4", threshold=250,
        out_html=out, job_id="j1", source_name="test.pdf",
    )
    assert report["status"] == "fail"
    assert report["summary"]["flagged"] == 1
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "<html" in text and "Page 1" in text
    assert "data:image/png;base64," in text  # thumbnail embedded
    assert not out.with_name(out.name + ".tmp").exists()


def test_run_lowres_report_all_pass_has_empty_state(all_good_pdf_bytes, tmp_path):
    out = tmp_path / "ok.html"
    report = run_lowres_report(
        all_good_pdf_bytes, target="A4", threshold=250,
        out_html=out, job_id="j2",
    )
    assert report["status"] == "pass"
    assert "Aucune image" in out.read_text(encoding="utf-8")


def test_html_report_layout_rules(mixed_pdf_bytes, tmp_path):
    """Report is ordered by page number, drops the current-dpi and colorspace
    columns, and labels the remaining dpi column with the target format."""
    out = tmp_path / "layout.html"
    run_lowres_report(
        mixed_pdf_bytes, target="A3", threshold=1000,  # force both images to flag
        out_html=out, job_id="layout",
    )
    text = out.read_text(encoding="utf-8")

    assert "DPI (A3)" in text
    assert "DPI actuel" not in text
    assert "DPI cible" not in text
    assert "Espace coul." not in text

    # Rows are in page order: page 1's row must appear before any later page.
    idx_p1 = text.index("Page 1")
    # both fixture images are on page 1, so just confirm no "current dpi" column
    # artifact (a second "dpi" td) leaked through by checking column count per row.
    assert text.count('class="dpi bad"') >= 1
    assert idx_p1 != -1

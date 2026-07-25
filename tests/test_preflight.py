from pathlib import Path

import fitz
import pytest

from prepress_mcp.preflight import analyze, run_preflight


def test_effective_dpi_math(mixed_pdf_bytes):
    doc = fitz.open(stream=mixed_pdf_bytes, filetype="pdf")
    placements = analyze(doc, threshold=300)
    doc.close()
    dpis = sorted(round(p.effective_dpi) for p in placements)
    assert dpis == [25, 600]


def test_flagging_and_status(mixed_pdf_bytes, tmp_path):
    out = tmp_path / "annotated.pdf"
    report = run_preflight(
        mixed_pdf_bytes, threshold=300, annotated_out=out, job_id="job1"
    )
    assert report["status"] == "fail"
    assert report["summary"]["flagged"] == 1
    assert report["summary"]["placements"] == 2
    assert report["flagged"][0]["effective_dpi"] == 25.0
    assert out.exists() and out.read_bytes().startswith(b"%PDF-")


def test_all_good_passes(all_good_pdf_bytes, tmp_path):
    out = tmp_path / "a.pdf"
    report = run_preflight(
        all_good_pdf_bytes, threshold=300, annotated_out=out, job_id="job2"
    )
    assert report["status"] == "pass"
    assert report["summary"]["flagged"] == 0


def test_threshold_is_respected(mixed_pdf_bytes, tmp_path):
    # At threshold 20 dpi, even the 25 dpi image passes.
    report = run_preflight(
        mixed_pdf_bytes, threshold=20, annotated_out=tmp_path / "b.pdf", job_id="j"
    )
    assert report["status"] == "pass"


def test_output_is_unencrypted_and_atomic(mixed_pdf_bytes, tmp_path):
    import fitz

    out = tmp_path / "hardened.pdf"
    run_preflight(mixed_pdf_bytes, threshold=300, annotated_out=out, job_id="h")
    # No leftover temp file, and the result opens with no security handler.
    assert not (tmp_path / "hardened.pdf.tmp").exists()
    d = fitz.open(out)
    assert d.is_encrypted is False
    assert d.page_count == 1
    d.close()

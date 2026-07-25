"""Shared fixtures: programmatically generated reference PDFs.

We build PDFs with images of known pixel dimensions placed at known physical
sizes, so the expected effective DPI is exact and deterministic.
"""
from __future__ import annotations

import fitz
import pytest


def _png(px_w: int, px_h: int, color=(200, 40, 40)) -> bytes:
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, px_w, px_h))
    pix.set_rect(pix.irect, color)
    return pix.tobytes("png")


def _place(page, rect_pts, px_w, px_h):
    page.insert_image(fitz.Rect(*rect_pts), stream=_png(px_w, px_h))


@pytest.fixture
def mixed_pdf_bytes() -> bytes:
    """One A4 page with a low-res (25 dpi) and a high-res (600 dpi) image."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4 in points
    # 50 px over 2 inches (144 pt) -> 25 dpi (below any sane threshold)
    _place(page, (72, 72, 72 + 144, 72 + 144), 50, 50)
    # 600 px over 1 inch (72 pt) -> 600 dpi (well above threshold)
    _place(page, (72, 400, 72 + 72, 400 + 72), 600, 600)
    data = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def all_good_pdf_bytes() -> bytes:
    """Single high-res image -> should pass at 300 dpi."""
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    _place(page, (72, 72, 72 + 72, 72 + 72), 400, 400)  # 400 dpi
    data = doc.tobytes()
    doc.close()
    return data

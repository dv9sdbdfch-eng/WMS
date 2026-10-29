"""Bin labels as a PDF: QR code + Code 128 barcode + large bin code, ready for any printer.

Two layouts:

* ``A4_SHEET`` - 2 x 7 labels of 99.1 x 38.1 mm (standard A4 label sheets, e.g. Avery L7163).
* ``LABEL_100x50`` - one 100 x 50 mm label per page (thermal label printers).

The scanned value is ``Bin.scan_code`` (the bin barcode, or its full code when it has none), which
``LocationService.find_bin`` resolves - the same lookup the manual search uses.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from reportlab.graphics import renderPDF
from reportlab.graphics.barcode import code128, qr
from reportlab.graphics.shapes import Drawing
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas


@dataclass
class BinLabel:
    code: str  # short code printed large, e.g. "B04"
    location: str  # path, e.g. "MAIN / A / R03 / S02"
    scan_value: str  # what the barcodes contain


@dataclass(frozen=True)
class Layout:
    page: tuple[float, float]
    label_w: float
    label_h: float
    cols: int
    rows: int
    left: float
    top: float
    gap_x: float = 0.0


A4_SHEET = Layout(A4, 99.1 * mm, 38.1 * mm, 2, 7, 4.65 * mm, 15.15 * mm, 2.5 * mm)
LABEL_100x50 = Layout((100 * mm, 50 * mm), 100 * mm, 50 * mm, 1, 1, 0, 0)
LAYOUTS = {"A4_SHEET": A4_SHEET, "LABEL_100x50": LABEL_100x50}


def _fit_font(c: canvas.Canvas, text: str, font: str, max_w: float, start: float) -> float:
    size = start
    while size > 6 and c.stringWidth(text, font, size) > max_w:
        size -= 1
    return size


def _draw(c: canvas.Canvas, lab: BinLabel, x: float, y: float, w: float, h: float) -> None:
    pad = 3 * mm
    qr_size = h - 2 * pad
    widget = qr.QrCodeWidget(lab.scan_value)
    bx0, by0, bx1, by1 = widget.getBounds()
    d = Drawing(qr_size, qr_size, transform=[qr_size / (bx1 - bx0), 0, 0, qr_size / (by1 - by0), 0, 0])
    d.add(widget)
    renderPDF.draw(d, c, x + pad, y + pad)
    tx = x + pad + qr_size + 3 * mm
    tw = x + w - pad - tx
    size = _fit_font(c, lab.code, "Helvetica-Bold", tw, 30)
    c.setFont("Helvetica-Bold", size)
    c.drawString(tx, y + h - pad - size * 0.8, lab.code)
    c.setFont("Helvetica", _fit_font(c, lab.location, "Helvetica", tw, 9))
    c.drawString(tx, y + h - pad - size * 0.8 - 11, lab.location)
    bar = code128.Code128(lab.scan_value, barHeight=h * 0.3, barWidth=0.33 * mm, quiet=False)
    if bar.width > tw:  # long codes: narrower bars so the barcode stays inside the label
        bar = code128.Code128(lab.scan_value, barHeight=h * 0.3, barWidth=0.33 * mm * tw / bar.width, quiet=False)
    bar.drawOn(c, tx, y + pad + 8)
    c.setFont("Helvetica", 7)
    c.drawString(tx, y + pad, lab.scan_value[:60])


def bin_labels_pdf(labels: list[BinLabel], path: Path, layout: str = "A4_SHEET") -> Path:
    lay = LAYOUTS[layout]
    c = canvas.Canvas(str(path), pagesize=lay.page)
    c.setTitle("Bin labels")
    per_page = lay.cols * lay.rows
    for i, lab in enumerate(labels):
        if i and i % per_page == 0:
            c.showPage()
        slot = i % per_page
        col, row = slot % lay.cols, slot // lay.cols
        x = lay.left + col * (lay.label_w + lay.gap_x)
        y = lay.page[1] - lay.top - (row + 1) * lay.label_h
        _draw(c, lab, x, y, lay.label_w, lay.label_h)
    c.save()
    return path

"""Dispatch: finished products leave the warehouse on a Delivery Note (v1.22).

Finished products are accounted for separately from materials:

* they are made by Production (Finished Products screen) and leave by **Dispatch**, never by
  Goods Out (which issues materials to projects);
* every dispatch gets its own number, DEL-000001, which is the number of the **Delivery Note**
  printed for the driver: who it goes to, delivery address, contact, the items and their
  quantities, and boxes for the receiver's name, signature and date (proof of delivery on paper);
* a dispatch to a project does not count as material issued to it (Net Issued / Remaining are
  about materials only);
* no courier or carrier booking (Ihor, 09.11.2026): the vehicle / driver is plain text.

A dispatch posted by mistake is reversed like every other document: the items go back to the
bins they left and the Delivery Note is marked REVERSED.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from sqlalchemy import or_, select
from sqlalchemy.orm import selectinload

from wms import APP_NAME
from wms.core.enums import (
    CLOSED_SITE_STATUSES,
    DocumentStatus,
    EntityType,
    FinishedStatus,
    ItemType,
    MovementType,
    SiteStatus,
)
from wms.core.exceptions import InsufficientStockError, NotFoundError, ValidationError
from wms.core.numbers import ZERO, fmt_date, fmt_qty, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import Bin, Dispatch, DispatchLine, FinishedProductInfo, Product, Site, StockLevel, StockMovement
from wms.services import audit
from wms.services.base import BaseService, check_client_token, clean_str, like_pattern
from wms.services.goods_in import doc_datetime, validate_doc_date
from wms.services.goods_out import allocate
from wms.services.numbering import next_number
from wms.services.stock_engine import StockEngine


@dataclass
class DispatchLineInput:
    product_id: int
    quantity: Decimal | str
    bin_id: int | None = None  # None = pick automatically (default bin first, then the fullest)
    notes: str | None = None


def _total_in_stock(s, product_id: int) -> Decimal:
    q = s.scalars(select(StockLevel.quantity).where(StockLevel.product_id == product_id))
    return sum(q, ZERO)


class DispatchService(BaseService):
    def post(self, header: dict, lines: list[DispatchLineInput]) -> Dispatch:
        """Send finished products out. All lines post or none do. Returns the dispatch (its number
        is the Delivery Note number)."""
        self.require(P.GOODS_OUT_POST)
        when_date = validate_doc_date(header.get("dispatch_date"), "dispatch_date")
        if not lines:
            raise ValidationError("Add at least one finished product.")
        with self.db.session() as s:
            check_client_token(s, Dispatch, header.get("client_token"), "dispatch_number",
                               EntityType.DISPATCH.value, "dispatch")
            site = s.get(Site, header.get("site_id")) if header.get("site_id") else None
            if site is not None and site.status in CLOSED_SITE_STATUSES:
                raise ValidationError(f"Project {site.project_number} is {SiteStatus(site.status).label.lower()} - "
                                      f"reopen it before dispatching to it.", "site_id")
            customer = clean_str(header.get("customer"), max_len=200)
            if site is None and not customer:
                raise ValidationError("Say who the goods are going to: a customer or a project.", "customer")
            d = Dispatch(
                dispatch_number=next_number(s, "DISPATCH"), dispatch_date=when_date,
                site_id=site.id if site else None,
                site_number_snapshot=site.project_number if site else None,
                site_name_snapshot=site.name if site else None,
                customer=customer or (site.customer if site else None),
                delivery_address=clean_str(header.get("delivery_address")),
                contact=clean_str(header.get("contact"), max_len=200),
                vehicle=clean_str(header.get("vehicle"), max_len=120),
                reference=clean_str(header.get("reference"), max_len=80),
                notes=clean_str(header.get("notes")),
                dispatched_by=clean_str(header.get("dispatched_by"), max_len=120) or self.user.full_name,
                user_id=self.user.id, status=DocumentStatus.POSTED.value,
                client_token=header.get("client_token") or None)
            s.add(d)
            s.flush()
            engine = StockEngine(s, self.ctx)
            when = doc_datetime(when_date)
            line_no = 0
            touched: set[int] = set()
            for i, ln in enumerate(lines, start=1):
                product = s.get(Product, ln.product_id)
                if product is None:
                    raise ValidationError(f"Line {i}: product not found.")
                if product.item_type != ItemType.FINISHED.value:
                    raise ValidationError(f"Line {i}: {product.internal_code} is a material, not a finished product. "
                                          f"Materials go to projects with Goods Out.")
                qty = to_qty(ln.quantity, f"line {i} quantity")
                if qty <= ZERO:
                    raise ValidationError(f"Line {i} ({product.internal_code}): quantity must be greater than zero.")
                if not product.unit.allow_decimal and qty != qty.to_integral_value():
                    raise ValidationError(f"Line {i} ({product.internal_code}): whole numbers only.")
                if ln.bin_id:
                    b = s.get(Bin, ln.bin_id)
                    if b is None:
                        raise ValidationError(f"Line {i}: bin not found.")
                    if not b.is_available:
                        raise ValidationError(f"Line {i}: bin {b.full_code} is a quarantine bin.")
                    picks = [(b, qty)]
                else:
                    picks = allocate(s, product, qty)  # never below zero: finished goods are counted items
                for b, take in picks:
                    mv = engine.post(
                        movement_type=MovementType.DISPATCH, product=product, quantity=take, from_bin=b,
                        occurred_at=when, site_id=site.id if site else None, doc_type=EntityType.DISPATCH.value,
                        doc_id=d.id, doc_number=d.dispatch_number, reference=d.reference,
                        notes=clean_str(ln.notes))
                    line_no += 1
                    d.lines.append(DispatchLine(line_no=line_no, product_id=product.id, quantity=take, bin_id=b.id,
                                                notes=clean_str(ln.notes), movement_id=mv.id))
                touched.add(product.id)
            s.flush()
            self._update_statuses(s, touched)
            audit.record(s, self.ctx, "DISPATCH_POSTED", EntityType.DISPATCH.value, d.id, d.dispatch_number,
                         f"{line_no} line(s) dispatched to {d.deliver_to}.",
                         new={"deliver_to": d.deliver_to, "address": d.delivery_address, "vehicle": d.vehicle,
                              "lines": [{"product_id": x.product_id, "qty": x.quantity, "bin_id": x.bin_id}
                                        for x in d.lines]})
            did = d.id
        return self.get(did)

    @staticmethod
    def _update_statuses(s, product_ids) -> None:
        """Nothing left in stock -> Dispatched; stock back (reversal) -> Ready again."""
        for pid in product_ids:
            fin = s.get(FinishedProductInfo, pid)
            left = _total_in_stock(s, pid)
            if fin is None:
                if left <= ZERO:
                    s.add(FinishedProductInfo(product_id=pid, status=FinishedStatus.DISPATCHED.value))
                continue
            if left <= ZERO and fin.status != FinishedStatus.DISPATCHED.value:
                fin.status = FinishedStatus.DISPATCHED.value
            elif left > ZERO and fin.status == FinishedStatus.DISPATCHED.value:
                fin.status = FinishedStatus.READY.value

    def reverse(self, dispatch_id: int, reason: str, reason_code: str = "OTHER") -> Dispatch:
        """Cancel a dispatch posted by mistake (the goods never left)."""
        self.require(P.GOODS_OUT_REVERSE)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            d = s.get(Dispatch, dispatch_id)
            if d is None:
                raise NotFoundError("Dispatch not found.")
            if d.status != DocumentStatus.POSTED.value:
                raise ValidationError("This dispatch has already been reversed.")
            engine = StockEngine(s, self.ctx)
            for line in d.lines:
                engine.reverse(s.get(StockMovement, line.movement_id), f"{d.dispatch_number} reversed: {reason}",
                               reason_code=reason_code)
            d.status, d.reversed_at, d.reversed_by_id, d.reversal_reason = (
                DocumentStatus.REVERSED.value, utcnow(), self.user.id, reason)
            s.flush()
            self._update_statuses(s, {x.product_id for x in d.lines})
            audit.record(s, self.ctx, "DISPATCH_REVERSED", EntityType.DISPATCH.value, d.id, d.dispatch_number,
                         reason, old={"status": "POSTED"}, new={"status": "REVERSED"})
        return self.get(dispatch_id)

    def get(self, dispatch_id: int) -> Dispatch:
        with self.db.read_session() as s:
            d = s.get(Dispatch, dispatch_id)
            if d is None:
                raise NotFoundError("Dispatch not found.")
            _ = [(x.product.unit, x.bin) for x in d.lines]
            return d

    def search(self, text: str | None = None, site_id: int | None = None, date_from: date | None = None,
               date_to: date | None = None, status: str | None = None, limit: int | None = None) -> list[Dispatch]:
        q = (select(Dispatch).options(selectinload(Dispatch.lines))
             .order_by(Dispatch.dispatch_date.desc(), Dispatch.id.desc()))
        if limit:
            q = q.limit(limit)
        if site_id:
            q = q.where(Dispatch.site_id == site_id)
        if date_from:
            q = q.where(Dispatch.dispatch_date >= date_from)
        if date_to:
            q = q.where(Dispatch.dispatch_date <= date_to)
        if status:
            q = q.where(Dispatch.status == status)
        if text and text.strip():
            like = like_pattern(text)
            q = q.where(or_(
                *(c.ilike(like, escape="\\") for c in (
                    Dispatch.dispatch_number, Dispatch.customer, Dispatch.reference, Dispatch.delivery_address,
                    Dispatch.contact, Dispatch.vehicle, Dispatch.site_number_snapshot, Dispatch.site_name_snapshot,
                    Dispatch.notes)),
                Dispatch.id.in_(select(DispatchLine.dispatch_id).join(Product).where(
                    or_(Product.internal_code.ilike(like, escape="\\"), Product.name.ilike(like, escape="\\")))),
            ))
        with self.db.read_session() as s:
            return list(s.scalars(q).unique())

    def stock_options(self) -> list[tuple[str, int]]:
        """Finished products that are in stock now: (label, id) for the Dispatch form."""
        from sqlalchemy import func
        with self.db.read_session() as s:
            rows = s.execute(
                select(Product.id, Product.internal_code, Product.name, func.sum(StockLevel.quantity))
                .join(StockLevel, StockLevel.product_id == Product.id).join(Bin, Bin.id == StockLevel.bin_id)
                .where(Product.item_type == ItemType.FINISHED.value, Product.is_active.is_(True),
                       Bin.is_available.is_(True), StockLevel.quantity > ZERO)
                .group_by(Product.id, Product.internal_code, Product.name).order_by(Product.internal_code)).all()
        return [(f"{code} - {name}   (in stock {fmt_qty(q)})", pid) for pid, code, name, q in rows]

    # -- the Delivery Note --------------------------------------------------------------------
    def delivery_note_pdf(self, dispatch_id: int, path: Path, company: str = "") -> Path:
        """The printed Delivery Note (A4): sender, deliver to, items, and signature boxes."""
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

        d = self.get(dispatch_id)
        path = Path(path)
        doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                                topMargin=14 * mm, bottomMargin=16 * mm, title=f"Delivery Note {d.dispatch_number}",
                                author=APP_NAME)
        st = getSampleStyleSheet()
        small = st["BodyText"].clone("small", fontSize=9, leading=11)
        bold = small.clone("bold", fontName="Helvetica-Bold")

        def esc(t) -> str:
            return str(t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("\n", "<br/>")

        title = "DELIVERY NOTE" + ("  -  REVERSED" if d.status == DocumentStatus.REVERSED.value else "")
        story = [Paragraph(esc(company) or APP_NAME, st["Heading2"]), Paragraph(title, st["Title"])]
        info = [[Paragraph("<b>Delivery Note No.</b>", small), Paragraph(esc(d.dispatch_number), bold),
                 Paragraph("<b>Date</b>", small), Paragraph(fmt_date(d.dispatch_date), small)],
                [Paragraph("<b>Deliver to</b>", small), Paragraph(esc(d.customer or d.site_label), small),
                 Paragraph("<b>Project</b>", small), Paragraph(esc(d.site_label), small)],
                [Paragraph("<b>Delivery address</b>", small), Paragraph(esc(d.delivery_address), small),
                 Paragraph("<b>Contact</b>", small), Paragraph(esc(d.contact), small)],
                [Paragraph("<b>Order / job ref</b>", small), Paragraph(esc(d.reference), small),
                 Paragraph("<b>Vehicle / driver</b>", small), Paragraph(esc(d.vehicle), small)]]
        t = Table(info, colWidths=[32 * mm, 63 * mm, 28 * mm, 57 * mm])
        t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C8CDD3")),
                               ("VALIGN", (0, 0), (-1, -1), "TOP"),
                               ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#F3F5F8")),
                               ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#F3F5F8"))]))
        story += [t, Spacer(1, 6 * mm)]
        head = colors.HexColor("#1F3A5F")
        items = [[Paragraph(f"<font color='white'><b>{h}</b></font>", small)
                  for h in ("No.", "Code", "Description", "Quantity", "Unit", "Received")]]
        merged: dict[int, list] = {}  # one line per product, however many bins it was picked from
        for x in d.lines:
            row = merged.setdefault(x.product_id, [x.product_code, x.product_name, ZERO, x.product.unit.code])
            row[2] += x.quantity
        for n, (code, name, qty, unit) in enumerate(merged.values(), 1):
            items.append([Paragraph(str(n), small), Paragraph(esc(code), small), Paragraph(esc(name), small),
                          Paragraph(fmt_qty(qty), small), Paragraph(esc(unit), small), Paragraph("", small)])
        it = Table(items, colWidths=[10 * mm, 30 * mm, 80 * mm, 22 * mm, 16 * mm, 22 * mm], repeatRows=1)
        it.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), head),
                                ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#C8CDD3")),
                                ("ALIGN", (3, 1), (3, -1), "RIGHT"), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story += [it, Spacer(1, 4 * mm)]
        if d.notes:
            story += [Paragraph(f"<b>Notes:</b> {esc(d.notes)}", small), Spacer(1, 4 * mm)]
        story.append(Paragraph("Please check the goods on delivery. Note any damage or shortage below before "
                               "signing.", small))
        story.append(Spacer(1, 4 * mm))
        sign = [[Paragraph("<b>Dispatched by</b>", small), Paragraph(esc(d.dispatched_by), small),
                 Paragraph("<b>Received by (print name)</b>", small), ""],
                [Paragraph("<b>Signature</b>", small), "", Paragraph("<b>Signature</b>", small), ""],
                [Paragraph("<b>Date</b>", small), Paragraph(fmt_date(d.dispatch_date), small),
                 Paragraph("<b>Date / time</b>", small), ""],
                [Paragraph("<b>Damage / shortage</b>", small), "", "", ""]]
        sg = Table(sign, colWidths=[32 * mm, 58 * mm, 40 * mm, 50 * mm], rowHeights=[9 * mm, 14 * mm, 9 * mm, 18 * mm])
        sg.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#8A94A3")),
                                ("SPAN", (1, 3), (3, 3)), ("VALIGN", (0, 0), (-1, -1), "TOP")]))
        story.append(sg)

        def footer(canvas, docx):
            canvas.saveState()
            canvas.setFont("Helvetica", 7)
            canvas.drawString(15 * mm, 8 * mm, f"{APP_NAME} - Delivery Note {d.dispatch_number} - printed "
                                               f"{datetime.now():%d/%m/%Y %H:%M}")
            canvas.drawRightString(A4[0] - 15 * mm, 8 * mm, f"Page {docx.page}")
            canvas.restoreState()

        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        return path


__all__ = ["DispatchLineInput", "DispatchService", "InsufficientStockError"]

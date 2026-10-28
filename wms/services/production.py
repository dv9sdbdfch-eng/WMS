"""Record production: raw materials consumed -> finished product added."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from wms.core.enums import DocumentStatus, EntityType, ItemType, MovementType
from wms.core.exceptions import InsufficientStockError, NotFoundError, ValidationError
from wms.core.numbers import ZERO, to_qty, utcnow
from wms.core.permissions import P
from wms.db.models import Bin, FinishedProductInfo, Product, ProductionLine, ProductionOrder, Site, StockMovement
from wms.services import audit
from wms.services.base import BaseService, clean_str
from wms.services.goods_in import doc_datetime, validate_doc_date
from wms.services.goods_out import allocate
from wms.services.numbering import next_number
from wms.services.stock_engine import StockEngine


@dataclass
class MaterialInput:
    product_id: int
    quantity: Decimal | str
    bin_id: int | None = None


class ProductionService(BaseService):
    def post(self, header: dict, materials: list[MaterialInput], override_reason: str | None = None
             ) -> ProductionOrder:
        self.require(P.PRODUCTION_POST)
        pdate = validate_doc_date(header.get("production_date"), "production_date")
        with self.db.session() as s:
            fp = s.get(Product, header.get("finished_product_id")) if header.get("finished_product_id") else None
            if fp is None or fp.item_type != ItemType.FINISHED.value:
                raise ValidationError("Choose the finished product that was made.", "finished_product_id")
            out_bin = s.get(Bin, header.get("output_bin_id") or fp.default_bin_id)
            if out_bin is None:
                raise ValidationError("Choose the bin where the finished product is stored.", "output_bin_id")
            qty = to_qty(header.get("quantity"), "quantity")
            if qty <= ZERO:
                raise ValidationError("Quantity produced must be greater than zero.", "quantity")
            site = s.get(Site, header["site_id"]) if header.get("site_id") else None
            order = ProductionOrder(
                order_number=next_number(s, "PRODUCTION"), finished_product_id=fp.id, quantity=qty,
                output_bin_id=out_bin.id, site_id=site.id if site else None, production_date=pdate,
                notes=clean_str(header.get("notes")), user_id=self.user.id, status=DocumentStatus.POSTED.value,
            )
            s.add(order)
            s.flush()
            engine = StockEngine(s, self.ctx)
            when = doc_datetime(pdate)
            common = dict(occurred_at=when, site_id=order.site_id, doc_type=EntityType.PRODUCTION.value,
                          doc_id=order.id, doc_number=order.order_number)
            line_no = 0
            for i, m in enumerate(materials, start=1):
                mat = s.get(Product, m.product_id)
                if mat is None:
                    raise ValidationError(f"Material line {i}: product not found.")
                if mat.id == fp.id:
                    raise ValidationError("A product cannot be consumed to make itself.")
                mqty = to_qty(m.quantity, f"material line {i} quantity")
                if m.bin_id:
                    picks = [(s.get(Bin, m.bin_id), mqty)]
                else:
                    try:
                        picks = allocate(s, mat, mqty)
                    except InsufficientStockError:
                        if not override_reason or not mat.default_bin_id:
                            raise
                        picks = [(s.get(Bin, mat.default_bin_id), mqty)]
                for b, take in picks:
                    mv = engine.post(movement_type=MovementType.PRODUCTION_CONSUME, product=mat, quantity=take,
                                     from_bin=b, override_reason=override_reason,
                                     notes=f"Used for {fp.internal_code}", **common)
                    line_no += 1
                    order.lines.append(ProductionLine(line_no=line_no, product_id=mat.id, quantity=take,
                                                      bin_id=b.id, movement_id=mv.id))
            out_mv = engine.post(movement_type=MovementType.PRODUCTION_OUTPUT, product=fp, quantity=qty,
                                 to_bin=out_bin, notes=clean_str(header.get("notes")), **common)
            order.output_movement_id = out_mv.id
            fin = fp.finished_info or FinishedProductInfo(product_id=fp.id)
            fin.production_date = pdate
            if site and not fin.site_id:
                fin.site_id = site.id
            fp.finished_info = fin
            audit.record(s, self.ctx, "PRODUCTION_POSTED", EntityType.PRODUCTION.value, order.id,
                         f"{order.order_number} {fp.internal_code}",
                         f"{qty.normalize():f} {fp.unit.code} produced from {line_no} material line(s).")
            order_id = order.id
        return self.get(order_id)

    def reverse(self, order_id: int, reason: str, override_reason: str | None = None) -> ProductionOrder:
        self.require(P.PRODUCTION_POST)
        self.require(P.STOCK_ADJUST)
        reason = clean_str(reason, "reason", required=True)
        with self.db.session() as s:
            order = s.get(ProductionOrder, order_id)
            if order is None:
                raise NotFoundError("Production record not found.")
            if order.status != DocumentStatus.POSTED.value:
                raise ValidationError("This production record has already been reversed.")
            engine = StockEngine(s, self.ctx)
            engine.reverse(s.get(StockMovement, order.output_movement_id), f"{order.order_number} reversed: {reason}",
                           override_reason=override_reason)
            for line in order.lines:
                engine.reverse(s.get(StockMovement, line.movement_id), f"{order.order_number} reversed: {reason}")
            order.status = DocumentStatus.REVERSED.value
            order.reversed_at = utcnow()
            order.reversed_by_id = self.user.id
            order.reversal_reason = reason
            audit.record(s, self.ctx, "PRODUCTION_REVERSED", EntityType.PRODUCTION.value, order.id,
                         order.order_number, reason)
        return self.get(order_id)

    def get(self, order_id: int) -> ProductionOrder:
        with self.db.read_session() as s:
            order = s.get(ProductionOrder, order_id)
            if order is None:
                raise NotFoundError("Production record not found.")
            _ = [(l.product, l.bin) for l in order.lines]
            return order

    def list(self, product_id: int | None = None, date_from: date | None = None, date_to: date | None = None
             ) -> list[ProductionOrder]:
        q = (select(ProductionOrder).options(selectinload(ProductionOrder.lines))
             .order_by(ProductionOrder.production_date.desc(), ProductionOrder.id.desc()))
        if product_id:
            q = q.where(ProductionOrder.finished_product_id == product_id)
        if date_from:
            q = q.where(ProductionOrder.production_date >= date_from)
        if date_to:
            q = q.where(ProductionOrder.production_date <= date_to)
        with self.db.read_session() as s:
            return list(s.scalars(q).unique())

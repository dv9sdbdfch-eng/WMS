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
from wms.services.base import BaseService, check_client_token, clean_str
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
    def post(self, header: dict, materials: list[MaterialInput], override_reason: str | None = None,
             other_projects_reason: str | None = None) -> ProductionOrder:
        """Record production. v1.35 (Ihor 21.11.2026): materials for a project come from its own stock
        first, then general stock. Using another project's stock is blocked; a Store Manager
        (``reservations.edit``) can allow it with ``other_projects_reason`` (as for Goods Out)."""
        self.require(P.PRODUCTION_POST)
        pdate = validate_doc_date(header.get("production_date"), "production_date")
        with self.db.session() as s:
            check_client_token(s, ProductionOrder, header.get("client_token"), "order_number",
                               EntityType.PRODUCTION.value, "production record")
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
            order_number = next_number(s, "PRODUCTION")  # locks the counter first, as Goods Out does
            from wms.services.reservations import from_other_projects, other_projects_message
            totals: dict[int, Decimal] = {}
            for m in materials:
                if m.product_id and m.quantity not in (None, ""):
                    q = to_qty(m.quantity, "quantity")
                    if q > ZERO:
                        totals[m.product_id] = totals.get(m.product_id, ZERO) + q
            from_others = from_other_projects(s, site.id if site else None, totals)
            other_reason = clean_str(other_projects_reason)
            if from_others and not other_reason:
                raise ValidationError(other_projects_message(s, totals, from_others, "This production"),
                                      "other_projects")
            if from_others:
                self.require(P.RESERVATIONS_EDIT)
            order = ProductionOrder(
                order_number=order_number, finished_product_id=fp.id, quantity=qty,
                output_bin_id=out_bin.id, site_id=site.id if site else None, production_date=pdate,
                site_number_snapshot=site.project_number if site else None,
                site_name_snapshot=site.name if site else None,
                notes=clean_str(header.get("notes")), user_id=self.user.id, status=DocumentStatus.POSTED.value,
                client_token=header.get("client_token") or None,
            )
            s.add(order)
            s.flush()
            engine = StockEngine(s, self.ctx)
            when = doc_datetime(pdate)
            common = dict(occurred_at=when, site_id=order.site_id, doc_type=EntityType.PRODUCTION.value,
                          doc_id=order.id, doc_number=order.order_number)
            line_no = 0
            # v1.35: production for a project uses the project's own stock first, as Goods Out does
            from wms.services.reservations import Issuing, refresh_statuses
            issuing = Issuing(s, site.id) if site else None
            for i, m in enumerate(materials, start=1):
                mat = s.get(Product, m.product_id)
                if mat is None:
                    raise ValidationError(f"Material line {i}: product not found.")
                if mat.id == fp.id:
                    raise ValidationError("A product cannot be consumed to make itself.")
                mqty = to_qty(m.quantity, f"material line {i} quantity")
                if m.bin_id:
                    b = s.get(Bin, m.bin_id)
                    if b is None:
                        raise ValidationError(f"Material line {i}: bin not found.")
                    picks = [(b, mqty)]
                else:
                    try:
                        picks = allocate(s, mat, mqty)
                    except InsufficientStockError:
                        if not override_reason or not mat.default_bin_id:
                            raise
                        picks = allocate(s, mat, mqty, shortfall_bin=s.get(Bin, mat.default_bin_id))
                for b, pick in picks:
                    parts = issuing.split(mat.id, pick) if issuing else [(None, pick)]
                    for reservation_id, take in parts:
                        mv = engine.post(movement_type=MovementType.PRODUCTION_CONSUME, product=mat, quantity=take,
                                         from_bin=b, override_reason=override_reason,
                                         notes=f"Used for {fp.internal_code}", **common)
                        line_no += 1
                        order.lines.append(ProductionLine(line_no=line_no, product_id=mat.id, quantity=take,
                                                          bin_id=b.id, movement_id=mv.id,
                                                          reservation_id=reservation_id))
            out_mv = engine.post(movement_type=MovementType.PRODUCTION_OUTPUT, product=fp, quantity=qty,
                                 to_bin=out_bin, notes=clean_str(header.get("notes")), **common)
            order.output_movement_id = out_mv.id
            if issuing:
                s.flush()
                refresh_statuses(s, self.ctx, issuing.touched, f"Used in production {order.order_number}.")
            if from_others:
                from wms.services.reservations import take_from_other_projects
                s.flush()
                moved = take_from_other_projects(s, self.ctx, site, from_others, order.order_number, other_reason)
                audit.record(s, self.ctx, "PROJECT_STOCK_TAKEN", EntityType.PRODUCTION.value, order.id,
                             order.order_number, f"Used other projects' stock: {other_reason}", new={"moved": moved})
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

    def reverse(self, order_id: int, reason: str, override_reason: str | None = None,
                reason_code: str = "OTHER") -> ProductionOrder:
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
                           override_reason=override_reason, reason_code=reason_code)
            for line in order.lines:
                engine.reverse(s.get(StockMovement, line.movement_id), f"{order.order_number} reversed: {reason}",
                               reason_code=reason_code)
            order.status = DocumentStatus.REVERSED.value
            order.reversed_at = utcnow()
            order.reversed_by_id = self.user.id
            order.reversal_reason = reason
            from wms.services.reservations import give_moved_stock_back, moved_by, refresh_statuses
            refresh_statuses(s, self.ctx, sorted({l.reservation_id for l in order.lines if l.reservation_id}),
                             f"{order.order_number} reversed.")
            give_moved_stock_back(s, self.ctx, moved_by(s, EntityType.PRODUCTION.value, order.id), order.order_number)
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

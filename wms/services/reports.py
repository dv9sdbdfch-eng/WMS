"""Reports. Each report returns typed columns and rows so the screen and every
exporter (Excel, CSV, PDF) format them consistently."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from wms.core.enums import DocumentStatus, FinishedStatus, ItemType, MovementType, ReturnCondition, ReturnType
from wms.core.numbers import ZERO
from wms.core.permissions import P
from wms.db.models import (
    Bin,
    DeliveryNote,
    DeliveryNoteLine,
    GoodsIssue,
    GoodsIssueLine,
    Product,
    StockLevel,
    StockReturn,
    Supplier,
)
from wms.services.base import BaseService, prefix_pattern
from wms.services.products import ProductService
from wms.services.sites import SITE_MOVEMENTS
from wms.services.stock_ops import StockOpsService

TEXT, QTY, MONEY, DATE, DATETIME, INT = "text", "qty", "money", "date", "datetime", "int"


@dataclass
class Column:
    title: str
    kind: str = TEXT
    total: bool = False  # show a total in the footer


@dataclass
class ReportFilters:
    date_from: date | None = None
    date_to: date | None = None
    product_id: int | None = None
    supplier_id: int | None = None
    site_id: int | None = None
    location_id: int | None = None
    item_type: str | None = None
    text: str | None = None


@dataclass
class ReportResult:
    title: str
    columns: list[Column]
    rows: list[list]
    filters_text: str = ""

    def totals(self) -> list:
        out = []
        for i, c in enumerate(self.columns):
            if c.total:
                out.append(sum((r[i] or ZERO for r in self.rows), ZERO))
            else:
                out.append(None)
        return out


@dataclass
class ReportDef:
    key: str
    title: str
    description: str
    filters: set[str] = field(default_factory=set)
    run: Callable[["ReportService", ReportFilters], ReportResult] | None = None


def _in_range(d: date | None, f: ReportFilters) -> bool:
    if d is None:
        return True
    if f.date_from and d < f.date_from:
        return False
    if f.date_to and d > f.date_to:
        return False
    return True


def _line_value(line, status: str) -> Decimal | None:
    """Value of a document line; reversed documents count as nothing so totals match stock."""
    if status != DocumentStatus.POSTED.value:
        return None
    return ((line.unit_cost or ZERO) * line.quantity).quantize(Decimal("0.01"))


def _local_date(dt) -> date:
    return dt.astimezone().date()


class ReportService(BaseService):
    def definitions(self) -> list[ReportDef]:
        return REPORTS

    def run(self, key: str, filters: ReportFilters) -> ReportResult:
        self.require(P.REPORTS_VIEW)
        rdef = next(r for r in REPORTS if r.key == key)
        result = rdef.run(self, filters)
        result.filters_text = self._describe(filters, rdef.filters)
        return result

    def _describe(self, f: ReportFilters, allowed: set[str]) -> str:
        from wms.core.numbers import fmt_date
        parts = []
        if "dates" in allowed and (f.date_from or f.date_to):
            parts.append(f"Dates: {fmt_date(f.date_from) or 'start'} to {fmt_date(f.date_to) or 'today'}")
        with self.db.read_session() as s:
            if f.product_id and "product" in allowed:
                p = s.get(Product, f.product_id)
                parts.append(f"Product: {p.internal_code}")
            if f.supplier_id and "supplier" in allowed:
                parts.append(f"Supplier: {s.get(Supplier, f.supplier_id).name}")
            if f.site_id and "site" in allowed:
                from wms.db.models import Site
                parts.append(f"Site: {s.get(Site, f.site_id).display}")
            if f.location_id and "location" in allowed:
                from wms.db.models import Location
                parts.append(f"Location: {s.get(Location, f.location_id).path}")
        if f.item_type and "item_type" in allowed:
            parts.append(f"Type: {ItemType(f.item_type).label}")
        if f.text and "text" in allowed:
            parts.append(f"Search: '{f.text}'")
        return "; ".join(parts)

    # -- individual reports -------------------------------------------------
    def current_stock(self, f: ReportFilters) -> ReportResult:
        rows = ProductService(self.ctx).list(text=f.text, item_types=[f.item_type] if f.item_type else None,
                                             supplier_id=f.supplier_id, location_id=f.location_id)
        cols = [Column("Code"), Column("Name"), Column("Type"), Column("Category"), Column("Unit"),
                Column("On Hand", QTY), Column("Available", QTY), Column("Min Level", QTY),
                Column("Unit Cost", MONEY), Column("Stock Value", MONEY, True), Column("Default Bin"),
                Column("Low Stock")]
        data = [[r.product.internal_code, r.product.name, ItemType(r.product.item_type).label,
                 r.product.category.full_name if r.product.category else "", r.product.unit.code,
                 r.on_hand, r.available, r.product.min_stock, r.product.unit_cost, r.stock_value,
                 r.product.default_bin.full_code if r.product.default_bin else "", "LOW" if r.is_low else ""]
                for r in rows]
        return ReportResult("Current Stock", cols, data)

    def goods_in(self, f: ReportFilters) -> ReportResult:
        q = (select(DeliveryNoteLine, DeliveryNote).join(DeliveryNote)
             .order_by(DeliveryNote.delivery_date.desc(), DeliveryNote.id.desc(), DeliveryNoteLine.line_no))
        if f.supplier_id:
            q = q.where(DeliveryNote.supplier_id == f.supplier_id)
        if f.product_id:
            q = q.where(DeliveryNoteLine.product_id == f.product_id)
        if f.date_from:
            q = q.where(DeliveryNote.delivery_date >= f.date_from)
        if f.date_to:
            q = q.where(DeliveryNote.delivery_date <= f.date_to)
        cols = [Column("Date", DATE), Column("Delivery Note"), Column("Supplier"), Column("Code"), Column("Product"),
                Column("Quantity", QTY), Column("Unit"), Column("Bin"), Column("Unit Cost", MONEY),
                Column("Value", MONEY, True), Column("Status"), Column("Received By")]
        with self.db.read_session() as s:
            data = [[dn.delivery_date, dn.dn_number, dn.supplier_label, l.product_code, l.product_name,
                     l.quantity, l.product.unit.code, l.bin_code, l.unit_cost,
                     _line_value(l, dn.status), dn.status.title(),
                     dn.received_by.full_name] for l, dn in s.execute(q)]
        return ReportResult("Goods In", cols, data)

    def goods_out(self, f: ReportFilters) -> ReportResult:
        q = (select(GoodsIssueLine, GoodsIssue).join(GoodsIssue)
             .order_by(GoodsIssue.issue_date.desc(), GoodsIssue.id.desc(), GoodsIssueLine.line_no))
        if f.site_id:
            q = q.where(GoodsIssue.site_id == f.site_id)
        if f.product_id:
            q = q.where(GoodsIssueLine.product_id == f.product_id)
        if f.date_from:
            q = q.where(GoodsIssue.issue_date >= f.date_from)
        if f.date_to:
            q = q.where(GoodsIssue.issue_date <= f.date_to)
        cols = [Column("Date", DATE), Column("Goods Out No."), Column("Site / Project"), Column("Reference"),
                Column("Code"), Column("Product"), Column("Quantity", QTY), Column("Unit"), Column("From Bin"),
                Column("Value", MONEY, True), Column("Requested By"), Column("Issued By"), Column("Status")]
        with self.db.read_session() as s:
            data = [[gi.issue_date, gi.issue_number, gi.site_label, gi.reference or "", l.product_code,
                     l.product_name, l.quantity, l.product.unit.code, l.bin_code,
                     _line_value(l, gi.status), gi.requested_by or "",
                     gi.issued_by, gi.status.title()] for l, gi in s.execute(q)]
        return ReportResult("Goods Out", cols, data)

    def movements(self, f: ReportFilters) -> ReportResult:
        rows = StockOpsService(self.ctx).movements(text=f.text, product_id=f.product_id, site_id=f.site_id,
                                                   supplier_id=f.supplier_id, date_from=f.date_from,
                                                   date_to=f.date_to, limit=None)
        cols = [Column("Date / Time", DATETIME), Column("Type"), Column("Code"), Column("Product"),
                Column("Quantity", QTY), Column("Unit"), Column("From"), Column("To"), Column("Site / Project"),
                Column("Supplier"), Column("Document"), Column("Reference"), Column("User"), Column("Notes")]
        # Names as recorded at the time: bins, projects and products removed or renamed since still show.
        data = [[m.occurred_at, MovementType(m.movement_type).label, m.product_code, m.product_name,
                 m.quantity, m.product.unit.code, m.from_bin_code, m.to_bin_code, m.site_label,
                 m.supplier_label, m.doc_number or "", m.reference or "", m.user_label, m.notes or ""]
                for m in rows]
        return ReportResult("Stock Movement History", cols, data)

    def stock_by_site(self, f: ReportFilters) -> ReportResult:
        rows = StockOpsService(self.ctx).movements(site_id=f.site_id, product_id=f.product_id,
                                                   movement_types=list(SITE_MOVEMENTS), date_from=f.date_from,
                                                   date_to=f.date_to, limit=None)
        agg: dict[tuple[int, int], list] = {}
        for m in rows:
            if m.site_id is None:
                continue
            key = (m.site_id, m.product_id)
            r = agg.setdefault(key, [m.site_label, m.product_code, m.product_name, ZERO, ZERO,
                                     m.product.unit.code, ZERO])
            sign = 1 if m.from_bin_id else -1
            cost = m.unit_cost or ZERO
            if m.movement_type == MovementType.GOODS_OUT.value:
                r[3] += sign * m.quantity
                r[6] += sign * m.quantity * cost
            else:
                r[4] -= sign * m.quantity
                r[6] += sign * m.quantity * cost
        data = []
        for r in sorted(agg.values(), key=lambda x: (x[0], x[1])):
            data.append([r[0], r[1], r[2], r[3], r[4], r[3] - r[4], r[5], r[6].quantize(Decimal("0.01"))])
        cols = [Column("Site / Project"), Column("Code"), Column("Product"), Column("Issued", QTY),
                Column("Returned", QTY), Column("Net Used", QTY), Column("Unit"), Column("Net Value", MONEY, True)]
        return ReportResult("Stock by Site", cols, data)

    def project_history(self, f: ReportFilters) -> ReportResult:
        """Every material movement to and from a site: what, how much, when, which document, who."""
        rows = StockOpsService(self.ctx).movements(site_id=f.site_id, product_id=f.product_id,
                                                   movement_types=list(SITE_MOVEMENTS), date_from=f.date_from,
                                                   date_to=f.date_to, limit=None)
        cols = [Column("Date", DATETIME), Column("Site / Project"), Column("Document"), Column("Type"),
                Column("Code"), Column("Material"), Column("Issued", QTY, True), Column("Returned", QTY, True),
                Column("Unit"), Column("Bin"), Column("Reference"), Column("User"), Column("Notes")]
        data = []
        for m in sorted(rows, key=lambda m: (m.site_label, m.occurred_at, m.id)):
            out = m.from_bin_id is not None  # stock left the store -> to site
            qty = m.quantity if out else -m.quantity
            is_issue = m.movement_type == MovementType.GOODS_OUT.value
            label = MovementType(m.movement_type).label + (" (reversal)" if m.reversal_of_id else "")
            data.append([m.occurred_at, m.site_label, m.doc_number or "", label,
                         m.product_code, m.product_name, qty if is_issue else None,
                         None if is_issue else -qty, m.product.unit.code,
                         m.from_bin_code or m.to_bin_code, m.reference or "", m.user_label, m.notes or ""])
        return ReportResult("Project Material History", cols, data)

    def _levels(self, f: ReportFilters) -> list[StockLevel]:
        # Explicit ON clauses: Product also links to Bin through default_bin_id, and an implicit
        # join(Product) picks that path - every level was repeated once per product defaulting to the bin.
        q = (select(StockLevel)
             .join(Bin, Bin.id == StockLevel.bin_id)
             .join(Product, Product.id == StockLevel.product_id)
             .where(StockLevel.quantity != ZERO)
             .order_by(Bin.full_code, Product.internal_code)
             .options(selectinload(StockLevel.bin).joinedload(Bin.location)))
        if f.product_id:
            q = q.where(StockLevel.product_id == f.product_id)
        if f.item_type:
            q = q.where(Product.item_type == f.item_type)
        if f.location_id:
            from wms.db.models import Location
            with self.db.read_session() as s:
                path = s.get(Location, f.location_id).path
            q = q.where(Bin.full_code.like(prefix_pattern(path), escape="\\"))
        with self.db.read_session() as s:
            return list(s.scalars(q))

    def stock_by_location(self, f: ReportFilters) -> ReportResult:
        agg: dict[tuple[str, int], list] = {}
        for lvl in self._levels(f):
            loc = lvl.bin.location.path
            r = agg.setdefault((loc, lvl.product_id), [loc, lvl.product.internal_code, lvl.product.name, ZERO,
                                                       lvl.product.unit.code, lvl.product.unit_cost or ZERO])
            r[3] += lvl.quantity
        data = [[r[0], r[1], r[2], r[3], r[4], (r[3] * r[5]).quantize(Decimal("0.01"))]
                for r in sorted(agg.values(), key=lambda x: (x[0], x[1]))]
        cols = [Column("Location"), Column("Code"), Column("Product"), Column("Quantity", QTY), Column("Unit"),
                Column("Value", MONEY, True)]
        return ReportResult("Stock by Location", cols, data)

    def stock_by_bin(self, f: ReportFilters) -> ReportResult:
        data = [[l.bin.full_code, "Yes" if l.bin.is_available else "No (quarantine)", l.product.internal_code,
                 l.product.name, l.quantity, l.product.unit.code,
                 (l.quantity * (l.product.unit_cost or ZERO)).quantize(Decimal("0.01"))] for l in self._levels(f)]
        cols = [Column("Bin"), Column("Available"), Column("Code"), Column("Product"), Column("Quantity", QTY),
                Column("Unit"), Column("Value", MONEY, True)]
        return ReportResult("Stock by Bin", cols, data)

    def low_stock(self, f: ReportFilters) -> ReportResult:
        rows = ProductService(self.ctx).list(low_only=True, supplier_id=f.supplier_id,
                                             item_types=[f.item_type] if f.item_type else None)
        rows.sort(key=lambda r: (r.product.supplier.name if r.product.supplier else "~", r.product.internal_code))
        cols = [Column("Supplier"), Column("Supplier Code"), Column("Code"), Column("Name"), Column("Status"),
                Column("Available", QTY), Column("On Hand", QTY), Column("Min Level", QTY), Column("Reorder Qty", QTY),
                Column("Suggested Order", QTY), Column("Unit"), Column("Unit Cost", MONEY),
                Column("Order Value", MONEY, True), Column("Default Bin")]
        data = [[r.product.supplier.name if r.product.supplier else "", r.product.supplier_code or "",
                 r.product.internal_code, r.product.name, r.status.label, r.available, r.on_hand,
                 r.product.min_stock, r.product.reorder_qty, r.shortfall, r.product.unit.code, r.product.unit_cost,
                 (r.shortfall * (r.product.unit_cost or ZERO)).quantize(Decimal("0.01")),
                 r.product.default_bin.full_code if r.product.default_bin else ""] for r in rows]
        return ReportResult("Low Stock", cols, data)

    def finished_products(self, f: ReportFilters) -> ReportResult:
        rows = ProductService(self.ctx).list(text=f.text, item_types=[ItemType.FINISHED.value])
        cols = [Column("Code"), Column("Name"), Column("Status"), Column("Production Date", DATE),
                Column("Site / Project"), Column("Customer"), Column("On Hand", QTY), Column("Unit"),
                Column("Value", MONEY, True), Column("Default Bin")]
        data = []
        for r in rows:
            fi = r.product.finished_info
            if f.site_id and (fi is None or fi.site_id != f.site_id):
                continue
            if (f.date_from or f.date_to) and not (fi and fi.production_date and _in_range(fi.production_date, f)):
                continue
            data.append([r.product.internal_code, r.product.name, FinishedStatus(fi.status).label if fi else "",
                         fi.production_date if fi else None, fi.site.display if fi and fi.site else "",
                         fi.customer if fi else "", r.on_hand, r.product.unit.code, r.stock_value,
                         r.product.default_bin.full_code if r.product.default_bin else ""])
        return ReportResult("Finished Products", cols, data)

    def supplier_deliveries(self, f: ReportFilters) -> ReportResult:
        q = (select(Supplier.id, Supplier.name, DeliveryNote.id, DeliveryNote.delivery_date,
                    DeliveryNoteLine.quantity, DeliveryNoteLine.unit_cost)
             .join(DeliveryNote, DeliveryNote.supplier_id == Supplier.id)
             .join(DeliveryNoteLine, DeliveryNoteLine.delivery_note_id == DeliveryNote.id, isouter=True)
             .where(DeliveryNote.status == DocumentStatus.POSTED.value))
        if f.supplier_id:
            q = q.where(DeliveryNote.supplier_id == f.supplier_id)
        if f.date_from:
            q = q.where(DeliveryNote.delivery_date >= f.date_from)
        if f.date_to:
            q = q.where(DeliveryNote.delivery_date <= f.date_to)
        agg: dict[int, list] = {}
        notes: dict[int, set] = {}
        with self.db.read_session() as s:
            for sup_id, name, dn_id, ddate, qty, cost in s.execute(q):
                r = agg.setdefault(sup_id, [name, 0, 0, ZERO, ddate])
                notes.setdefault(sup_id, set()).add(dn_id)
                if qty is not None:
                    r[2] += 1
                    r[3] += (cost or ZERO) * qty
                r[4] = max(r[4], ddate)
        for sup_id, r in agg.items():
            r[1] = len(notes[sup_id])
        data = [[r[0], r[1], r[2], r[3].quantize(Decimal("0.01")), r[4]] for r in sorted(agg.values())]
        cols = [Column("Supplier"), Column("Deliveries", INT, True), Column("Lines", INT, True),
                Column("Total Value", MONEY, True), Column("Last Delivery", DATE)]
        return ReportResult("Supplier Deliveries", cols, data)

    def returns(self, f: ReportFilters) -> ReportResult:
        rets = StockOpsService(self.ctx).returns(text=f.text, date_from=f.date_from, date_to=f.date_to, limit=None)
        cols = [Column("Date", DATE), Column("Return No."), Column("Type"), Column("Site / Supplier"), Column("Code"),
                Column("Product"), Column("Quantity", QTY), Column("Unit"), Column("Condition"), Column("Bin"),
                Column("Reference"), Column("Reason"), Column("Status")]
        data = []
        with self.db.read_session() as s:
            for r in rets:
                r = s.get(StockReturn, r.id)
                if f.site_id and r.site_id != f.site_id:
                    continue
                if f.supplier_id and r.supplier_id != f.supplier_id:
                    continue
                party = r.party_label
                for l in r.lines:
                    if f.product_id and l.product_id != f.product_id:
                        continue
                    data.append([r.return_date, r.return_number, ReturnType(r.return_type).label, party,
                                 l.product_code, l.product_name, l.quantity, l.product.unit.code,
                                 ReturnCondition(l.condition).label, l.bin_code, r.reference or "",
                                 r.reason or "", r.status.title()])
        return ReportResult("Returns", cols, data)

    def adjustments(self, f: ReportFilters) -> ReportResult:
        rows = StockOpsService(self.ctx).adjustments(text=f.text, date_from=f.date_from, date_to=f.date_to,
                                                     limit=None)
        from wms.core.enums import AdjustmentReason
        cols = [Column("Date / Time", DATETIME), Column("Adjustment No."), Column("Code"), Column("Product"),
                Column("Bin"), Column("Previous Qty", QTY), Column("New Qty", QTY), Column("Difference", QTY),
                Column("Unit"), Column("Reason"), Column("Details"), Column("User")]
        data = [[a.created_at, a.adjustment_number, a.product_code, a.product_name, a.bin_code,
                 a.previous_qty, a.new_qty, a.difference, a.product.unit.code, AdjustmentReason(a.reason_code).label,
                 a.reason, a.user.full_name] for a in rows if not f.product_id or a.product_id == f.product_id]
        return ReportResult("Stock Adjustments", cols, data)


REPORTS: list[ReportDef] = [
    ReportDef("current_stock", "Current Stock", "Every product with quantity on hand, available and value.",
              {"text", "item_type", "supplier", "location"}, ReportService.current_stock),
    ReportDef("goods_in", "Goods In", "Every line received, by delivery note.",
              {"dates", "supplier", "product"}, ReportService.goods_in),
    ReportDef("goods_out", "Goods Out", "Every line issued to sites / projects.",
              {"dates", "site", "product"}, ReportService.goods_out),
    ReportDef("movements", "Stock Movement History", "The full movement ledger.",
              {"dates", "product", "site", "supplier", "text"}, ReportService.movements),
    ReportDef("stock_by_site", "Stock by Site", "Materials issued to and returned from each site.",
              {"dates", "site", "product"}, ReportService.stock_by_site),
    ReportDef("project_history", "Project Material History",
              "Every issue and return for a site / project, with document, reference and user.",
              {"dates", "site", "product"}, ReportService.project_history),
    ReportDef("stock_by_location", "Stock by Location", "Current stock grouped by warehouse location.",
              {"location", "product", "item_type"}, ReportService.stock_by_location),
    ReportDef("stock_by_bin", "Stock by Bin", "Current stock in every bin.",
              {"location", "product", "item_type"}, ReportService.stock_by_bin),
    ReportDef("low_stock", "Low Stock / Reorder List",
              "Out-of-stock and low products by supplier, with suggested order quantity and value.",
              {"supplier", "item_type"}, ReportService.low_stock),
    ReportDef("finished_products", "Finished Products", "Finished products with status and stock.",
              {"dates", "site", "text"}, ReportService.finished_products),
    ReportDef("supplier_deliveries", "Supplier Deliveries", "Number and value of deliveries per supplier.",
              {"dates", "supplier"}, ReportService.supplier_deliveries),
    ReportDef("returns", "Returns", "Returns from sites and to suppliers.",
              {"dates", "site", "supplier", "product", "text"}, ReportService.returns),
    ReportDef("adjustments", "Stock Adjustments", "Every stock adjustment with reason and user.",
              {"dates", "product", "text"}, ReportService.adjustments),
]

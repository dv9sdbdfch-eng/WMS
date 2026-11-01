"""Reorder Required: products whose Available stock is at or below their reorder level / minimum."""

from __future__ import annotations

from wms.core.enums import StockStatus
from wms.core.permissions import P
from wms.ui.pages.base import Page
from wms.ui.pages.products import product_columns
from wms.ui.widgets.common import QTY, Col, DataTable, button, confirm, h, info, label, run


class LowStockPage(Page):
    title = "Reorder Required"
    subtitle = ("Products whose Available stock (On Hand minus Reserved, quarantine not counted) is at or below "
                "their reorder level or minimum")

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.PURCHASING_EDIT):
            self.add_action(button("Create draft orders...", self.draft_orders, primary=True))
        if app.can(P.REPORTS_EXPORT):
            self.add_action(button("Reorder list (Excel / PDF)...", self.export))
        cols = product_columns()
        at = next(i for i, c in enumerate(cols) if c.title == "Unit")
        cols[at:at] = [
            # Already on open purchase orders: check before ordering again.
            Col("On Order", lambda r: r.incoming, QTY, 85),
        ]
        at = next(i for i, c in enumerate(cols) if c.title == "Min") + 1
        cols[at:at] = [
            Col("Reorder Level", lambda r: r.product.reorder_level, QTY, 95),
            Col("Reorder Qty", lambda r: r.product.reorder_qty, QTY, 90),
            Col("Suggested", lambda r: r.shortfall, QTY, 90),
        ]
        self.table = DataTable(cols)
        self.table.set_row_style(lambda r: "low" if r.status in (StockStatus.OUT, StockStatus.LOW) else None)
        self.table.activated_item.connect(self.open)
        self.body.addWidget(self.table, 1)
        self.count = label("", "Muted", wrap=True)
        footer = h(self.count, button("Copy list", self.table.copy_all))
        footer.setStretch(0, 1)  # the count text takes the free width and wraps when it is long
        self.body.addLayout(footer)
        self.body.addWidget(label(
            "Suggested = reorder level (or minimum) + reorder quantity - available - on order. "
            "Draft orders are never sent automatically: open them under Purchase Orders, check and mark as ordered.",
            "Muted", wrap=True))

    def refresh(self):
        rows = self.app.services.products.low_stock()
        rows.sort(key=lambda r: (r.status is not StockStatus.OUT,
                                 r.product.supplier.name if r.product.supplier else "~", r.product.internal_code))
        self.table.set_items(rows)
        out = sum(1 for r in rows if r.status is StockStatus.OUT)
        ordered = sum(1 for r in rows if r.incoming > 0)
        self.count.setText(f"{len(rows)} product(s) need reordering ({out} out of stock, {ordered} already on order). "
                           f"Grouped by supplier - double-click a product to open it."
                           if rows else "Nothing needs reordering.")

    def open(self, row):
        if row is not None:
            self.app.open_entity("PRODUCT", row.product.id, self)
            self.app.changed()

    def draft_orders(self):
        """Draft purchase orders, one per preferred supplier, for everything on the list."""
        rows = self.table.items()
        if not rows or not confirm(self, "Create draft orders",
                                   f"Create draft purchase orders for the {len(rows)} product(s) on this list?\n\n"
                                   "One draft per supplier, with the suggested quantities. Nothing is sent to "
                                   "suppliers - you check each draft and mark it as ordered yourself."):
            return
        done = run(self, self.app.services.purchasing.draft_from_reorder, [r.product.id for r in rows])
        if done is None:
            return
        drafts, skipped = done
        lines = [f"{po.po_number} - {po.supplier_label}" for po in drafts]
        text = ("Draft purchase orders created (not sent):\n" + "\n".join(lines)) if drafts \
            else "No draft orders were created."
        if skipped:
            text += "\n\nNot included:\n" + "\n".join(skipped)
        info(self, "Draft orders", text)
        self.app.changed()
        self.refresh()

    def export(self):
        self.main_window.go("reports")
        page = self.main_window.page("reports")
        if page is not None:
            page.select("low_stock")


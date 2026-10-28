from __future__ import annotations

from PySide6.QtWidgets import QGridLayout, QHBoxLayout, QVBoxLayout

from wms.core.enums import MovementType
from wms.core.numbers import fmt_money
from wms.core.permissions import P
from wms.ui.pages.base import Page
from wms.ui.widgets.common import DATE, DATETIME, QTY, Card, Col, DataTable, button, label


class DashboardPage(Page):
    title = "Dashboard"
    subtitle = "Today at a glance"

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        if app.can(P.GOODS_IN_POST):
            self.add_action(button("New Goods In", lambda: self.main_window.go("goods_in"), primary=True))
        if app.can(P.GOODS_OUT_POST):
            self.add_action(button("New Goods Out", lambda: self.main_window.go("goods_out"), primary=True))
        cards = QHBoxLayout()
        self.c_products = Card("Products")
        self.c_instock = Card("Products in stock")
        self.c_value = Card("Stock value")
        self.c_in = Card("Deliveries today")
        self.c_out = Card("Goods Out today")
        self.c_low = Card("Low stock")
        for c in (self.c_products, self.c_instock, self.c_value, self.c_in, self.c_out, self.c_low):
            cards.addWidget(c)
        self.body.addLayout(cards)
        self.c_value.setVisible(app.can(P.REPORTS_VIEW))

        grid = QGridLayout()
        grid.setSpacing(14)
        self.t_low = DataTable([Col("Code", lambda r: r.product.internal_code, width=90),
                                Col("Product", lambda r: r.product.name, width=150),
                                Col("On Hand", lambda r: r.on_hand, QTY, 70),
                                Col("Min", lambda r: r.product.min_stock, QTY, 70)])
        self.t_low.activated_item.connect(lambda r: self._open("PRODUCT", r.product.id))
        self.t_dn = DataTable([Col("Date", lambda d: d.delivery_date, DATE, 90),
                               Col("Delivery Note", lambda d: d.dn_number, width=110),
                               Col("Supplier", lambda d: d.supplier.name, width=130),
                               Col("Status", lambda d: d.status.title())])
        self.t_dn.activated_item.connect(lambda d: self._open("DELIVERY_NOTE", d.id))
        self.t_go = DataTable([Col("Date", lambda g: g.issue_date, DATE, 90),
                               Col("Number", lambda g: g.issue_number, width=90),
                               Col("Site", lambda g: g.site.display, width=150),
                               Col("Ref", lambda g: g.reference or "")])
        self.t_go.activated_item.connect(lambda g: self._open("GOODS_ISSUE", g.id))
        self.t_mv = DataTable([Col("When", lambda m: m.occurred_at, DATETIME, 120),
                               Col("Type", lambda m: MovementType(m.movement_type).label, width=150),
                               Col("Product", lambda m: f"{m.product.internal_code} - {m.product.name}", width=260),
                               Col("Qty", lambda m: m.quantity, QTY, 70),
                               Col("By", lambda m: m.user.full_name)])
        self.t_sites = DataTable([Col("Project", lambda s: s.project_number, width=100),
                                  Col("Site", lambda s: s.name, width=170),
                                  Col("Customer", lambda s: s.customer or "")])
        self.t_sites.activated_item.connect(lambda s: self._open("SITE", s.id))

        def block(title, table):
            v = QVBoxLayout()
            v.addWidget(label(title, "SectionTitle"))
            v.addWidget(table)
            return v

        grid.addLayout(block("Low stock", self.t_low), 0, 0)
        grid.addLayout(block("Recent deliveries", self.t_dn), 0, 1)
        grid.addLayout(block("Recent Goods Out", self.t_go), 0, 2)
        grid.addLayout(block("Recent stock movements", self.t_mv), 1, 0, 1, 2)
        grid.addLayout(block("Active sites / projects", self.t_sites), 1, 2)
        self.body.addLayout(grid, 1)

    def _open(self, kind, obj_id):
        self.app.open_entity(kind, obj_id, self)
        self.app.changed()

    def refresh(self):
        d = self.app.services.dashboard.load()
        self.c_products.set_value(str(d.total_products))
        self.c_instock.set_value(str(d.products_in_stock))
        self.c_value.set_value(fmt_money(d.stock_value))
        self.c_in.set_value(str(d.received_today))
        self.c_out.set_value(str(d.issued_today))
        self.c_low.set_value(str(len(d.low_stock)), warn=bool(d.low_stock))
        self.t_low.set_items(d.low_stock)
        self.t_dn.set_items(d.recent_deliveries)
        self.t_go.set_items(d.recent_issues)
        self.t_mv.set_items(d.recent_movements)
        self.t_sites.set_items(d.active_sites)

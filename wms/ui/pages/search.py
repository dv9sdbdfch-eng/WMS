"""Search: the one place to find a product, bin, project, supplier, delivery note or transaction.

Workflow: type (or scan) -> results -> filter -> open / act. Typing searches after a short pause;
Enter (what a barcode scanner sends) searches at once. Results come from the database already
filtered, sorted and counted, so the count is always the true number of matches.
"""

from __future__ import annotations

from PySide6.QtCore import QStringListModel, Qt, QTimer
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QCompleter,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QPushButton,
    QScrollArea,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import EntityType
from wms.core.exceptions import WMSError
from wms.core.permissions import P
from wms.services.search import (
    ARCHIVE_CHOICES,
    ATTACHMENT_CHOICES,
    DATE_PRESETS,
    LOCATION_LEVELS,
    MOVEMENT_GROUPS,
    PROJECT_STATUS_CHOICES,
    QUICK_FILTERS,
    STOCK_STATUSES,
    SearchCriteria,
    movement_reference,
    movement_route,
    signed_qty,
)
from wms.ui.pages.base import Page
from wms.ui.widgets.common import (
    DATETIME,
    QTY,
    Col,
    DataTable,
    DecimalEdit,
    OptionalDateEdit,
    PickCombo,
    SearchBox,
    button,
    confirm,
    info,
    label,
    run,
    show_error,
)


def product_result_columns() -> list[Col]:
    return [
        Col("Code", lambda r: r.product.internal_code, width=120, sort="code"),
        Col("Product", lambda r: r.product.name, width=240, sort="name"),
        Col("Description", lambda r: (r.product.description or "").replace("\n", " "), width=200,
            sort="description"),
        Col("Qty", lambda r: r.on_hand, QTY, 80, sort="qty"),
        Col("Unit", lambda r: r.product.unit.code, width=55, sort="unit"),
        Col("Location", lambda r: r.location_text, width=200, sort="location"),
        Col("Bin", lambda r: r.bin_text, width=100, sort="bin"),
        Col("Supplier", lambda r: r.product.supplier.name if r.product.supplier else "", width=140,
            sort="supplier"),
        Col("Status", _status_text, width=110, sort="status"),
        Col("Brand", lambda r: r.product.brand or "", width=100, sort="brand"),
    ]


def _status_text(r) -> str:
    if not r.product.is_active:
        return "Archived"
    return {"OK": "In Stock", "LOW": "Low Stock", "REORDER": "Reorder", "OUT": "Out of Stock"}.get(
        r.status.value, "In Stock" if r.on_hand > 0 else "Out of Stock")


def movement_result_columns() -> list[Col]:
    return [
        Col("Date / Time", lambda m: m.occurred_at, DATETIME, 130, sort="date"),
        Col("Type", lambda m: m.type_label,
            width=150, sort="type"),
        Col("Code", lambda m: m.product_code, width=110, sort="code"),
        Col("Product", lambda m: m.product_name, width=200, sort="product"),
        Col("Qty", signed_qty, QTY, 80, sort="qty"),
        Col("Unit", lambda m: m.product.unit.code, width=50),
        Col("From", lambda m: movement_route(m)[0], width=190, sort="bin"),
        Col("To", lambda m: movement_route(m)[1], width=190),
        Col("Project", lambda m: m.site_label, width=160, sort="project"),
        Col("Supplier", lambda m: m.supplier_label, width=130, sort="supplier"),
        Col("Reference", movement_reference, width=130, sort="reference"),
        Col("User", lambda m: m.user_label, width=110, sort="user"),
    ]


class ResultView(QWidget):
    """A result table with a count line and a clear "No matching records found" state."""

    def __init__(self, columns: list[Col], empty_hint: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.count = label("", "SectionTitle")
        lay.addWidget(self.count)
        self.stack = QStackedWidget()
        self.table = DataTable(columns, server_sort=True)
        self.empty = label(f"No matching records found\n\n{empty_hint}", "Muted", wrap=True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setStyleSheet("font-size: 12pt; padding: 40px;")
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty)
        lay.addWidget(self.stack, 1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(6)
        lay.addLayout(self.actions)

    def show_result(self, result, noun: str) -> None:
        self.table.set_items(result.rows, keep_selection=False)
        self.count.setText(result.summary(noun))
        self.stack.setCurrentWidget(self.table if result.total else self.empty)
        if result.rows:
            self.table.selectRow(0)


class FilterPanel(QScrollArea):
    """Advanced Filters. Every change re-runs the search straight away (text boxes after a short pause)."""

    def __init__(self, page: SearchPage):
        super().__init__()
        self.page = page
        self.setWidgetResizable(True)
        self.setMinimumWidth(360)
        self.setMaximumWidth(440)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        self._loading = False

        def group(title: str) -> QFormLayout:
            box = QGroupBox(title)
            form = QFormLayout(box)
            form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
            lay.addWidget(box)
            return form

        def text(placeholder: str = "") -> SearchBox:
            e = SearchBox(placeholder)
            e.search.connect(self._changed)
            return e

        def combo(choices: dict[str, str], any_text: str = "Any") -> QComboBox:
            c = QComboBox()
            c.addItem(any_text, None)
            for k, v in choices.items():
                c.addItem(v, k)
            c.currentIndexChanged.connect(self._changed)
            return c

        def picker(placeholder: str) -> PickCombo:
            p = PickCombo(placeholder)
            p.picked.connect(self._changed)
            return p

        f = group("Product")
        self.product_code = text("contains...")
        self.barcode = text()
        self.name = text()
        self.description = text()
        self.category = picker("Any category")
        self.brand = text()
        self.archive = combo(ARCHIVE_CHOICES, "Active (default)")
        f.addRow("Product code", self.product_code)
        f.addRow("Barcode", self.barcode)
        f.addRow("Product name", self.name)
        f.addRow("Description", self.description)
        f.addRow("Category", self.category)
        f.addRow("Brand", self.brand)
        f.addRow("Active / Archived", self.archive)

        f = group("Stock")
        self.stock_status = combo(STOCK_STATUSES)
        self.qty_gt = DecimalEdit()
        self.qty_lt = DecimalEdit()
        for e in (self.qty_gt, self.qty_lt):
            e.setPlaceholderText("any")
            e.editingFinished.connect(self._changed)
        f.addRow("Stock status", self.stock_status)
        f.addRow("Quantity greater than", self.qty_gt)
        f.addRow("Quantity less than", self.qty_lt)

        f = group("Location")
        self.locations: dict[str, PickCombo] = {}
        for attr, kind in LOCATION_LEVELS:
            self.locations[attr] = picker(f"Any {kind.label.lower()}")
            f.addRow(kind.label, self.locations[attr])
        self.bin = picker("Any bin")
        f.addRow("Bin", self.bin)

        f = group("Supplier")
        self.supplier = picker("Any supplier")
        self.supplier_code = text("contains...")
        f.addRow("Supplier", self.supplier)
        f.addRow("Supplier product code", self.supplier_code)

        f = group("Project")
        self.site = picker("Any site / project")
        self.project_number = text()
        self.customer = text()
        self.project_manager = text()
        self.project_status = combo(PROJECT_STATUS_CHOICES)
        f.addRow("Site / Project", self.site)
        f.addRow("Project number", self.project_number)
        f.addRow("Customer", self.customer)
        f.addRow("Project manager", self.project_manager)
        f.addRow("Active / Closed", self.project_status)

        box = QGroupBox("Transactions")
        tl = QVBoxLayout(box)
        self.types: dict[str, QCheckBox] = {}
        for key, (text_, _types) in MOVEMENT_GROUPS.items():
            cb = QCheckBox(text_)
            cb.toggled.connect(self._changed)
            self.types[key] = cb
            tl.addWidget(cb)
        tl.addWidget(label("None ticked = all types.", "Muted"))
        lay.addWidget(box)

        f = group("Documents")
        self.dn_number = text("e.g. DN-74821")
        self.reference = text("job / PO / document no.")
        self.attachment = combo(ATTACHMENT_CHOICES)
        f.addRow("Delivery note number", self.dn_number)
        f.addRow("Reference number", self.reference)
        f.addRow("Attachment", self.attachment)

        f = group("Date")
        self.date_preset = combo(DATE_PRESETS, "Any time")
        self.date_from, self.date_to = OptionalDateEdit(), OptionalDateEdit()
        for d in (self.date_from, self.date_to):
            d.edit.editingFinished.connect(self._changed)
        f.addRow("Period", self.date_preset)
        f.addRow("From", self.date_from)
        f.addRow("To", self.date_to)
        lay.addWidget(label("Date, project, transaction and document filters look at stock movements: in the "
                            "Products list they keep the products that had a matching movement.", "Muted",
                            wrap=True))
        lay.addStretch(1)
        self.setWidget(inner)
        self._options_loaded = False

    def load_options(self) -> None:
        app = self.page.app
        self.category.set_options([(c.full_name, c.id) for c in app.services.reference.categories()])
        tree = app.services.locations.tree(include_inactive=True)
        for attr, kind in LOCATION_LEVELS:
            self.locations[attr].set_options([(f"{loc.name} ({loc.path})" if loc.name != loc.code else loc.path,
                                               loc.id) for loc in tree if loc.kind == kind.value])
        self.bin.set_options(app.bin_options(include_inactive=True))
        self.supplier.set_options(app.supplier_options(include_inactive=True))
        self.site.set_options(app.site_options(open_only=False))
        self._options_loaded = True

    def _changed(self, *_):
        if self._loading:
            return
        self.date_from.setEnabled(self.date_preset.currentData() in (None, "CUSTOM"))
        self.date_to.setEnabled(self.date_preset.currentData() in (None, "CUSTOM"))
        self.page.filters_changed()

    # -- criteria <-> widgets -----------------------------------------------
    def read_into(self, c: SearchCriteria) -> None:
        """Copy the panel into the criteria. Raises on an unreadable number or date."""
        for attr in ("product_code", "barcode", "name", "description", "brand", "supplier_code", "project_number",
                     "customer", "project_manager", "dn_number", "reference"):
            setattr(c, attr, getattr(self, attr).text().strip() or None)
        for attr in ("archive", "stock_status", "project_status", "attachment", "date_preset"):
            setattr(c, attr, getattr(self, attr).currentData())
        c.category_id = self.category.value()
        c.supplier_id = self.supplier.value()
        c.site_id = self.site.value()
        c.bin_id = self.bin.value()
        for attr, picker in self.locations.items():
            setattr(c, attr, picker.value())
        c.qty_gt, c.qty_lt = self.qty_gt.value(), self.qty_lt.value()
        c.movement_types = [k for k, cb in self.types.items() if cb.isChecked()]
        if c.date_preset in (None, "CUSTOM"):
            c.date_from, c.date_to = self.date_from.value(), self.date_to.value()
            if c.date_preset is None and (c.date_from or c.date_to):
                c.date_preset = "CUSTOM"
        else:
            c.date_from = c.date_to = None

    def load(self, c: SearchCriteria) -> None:
        self._loading = True
        try:
            for attr in ("product_code", "barcode", "name", "description", "brand", "supplier_code",
                         "project_number", "customer", "project_manager", "dn_number", "reference"):
                box = getattr(self, attr)
                box.blockSignals(True)
                box.setText(getattr(c, attr) or "")
                box.blockSignals(False)
            for attr in ("archive", "stock_status", "project_status", "attachment", "date_preset"):
                cb = getattr(self, attr)
                cb.setCurrentIndex(max(0, cb.findData(getattr(c, attr))))
            self.category.set_value(c.category_id)
            self.supplier.set_value(c.supplier_id)
            self.site.set_value(c.site_id)
            self.bin.set_value(c.bin_id)
            for attr, picker in self.locations.items():
                picker.set_value(getattr(c, attr))
            self.qty_gt.set_value(c.qty_gt)
            self.qty_lt.set_value(c.qty_lt)
            for k, cb in self.types.items():
                cb.setChecked(k in c.movement_types)
            self.date_from.set_value(c.date_from)
            self.date_to.set_value(c.date_to)
            custom = c.date_preset in (None, "CUSTOM")
            self.date_from.setEnabled(custom)
            self.date_to.setEnabled(custom)
        finally:
            self._loading = False


class SearchPage(Page):
    title = "Search"
    subtitle = "Find any product, bin, project, supplier, delivery note or transaction - type or scan"

    PRODUCTS, MOVEMENTS = 0, 1

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.criteria = SearchCriteria()
        self.main_window = None
        self.sorts = {self.PRODUCTS: ("relevance", False), self.MOVEMENTS: ("date", True)}
        self._stale = {self.PRODUCTS: True, self.MOVEMENTS: True}
        self.can_products = app.can(P.PRODUCTS_VIEW)
        self.can_movements = app.can(P.STOCK_VIEW)

        self.saved_btn = button("Saved searches", self._saved_menu)
        self.add_action(self.saved_btn)
        self.add_action(button("Save search...", self.save_search))
        self.filters_btn = button("Advanced Filters", self._toggle_filters, shortcut="Ctrl+Shift+F")
        self.filters_btn.setCheckable(True)
        self.add_action(self.filters_btn)

        # Search box (also takes barcode scans: scanners type the code and press Enter)
        self.box = SearchBox("Type or scan: code, barcode, name, supplier, brand, delivery note, project, bin, "
                             "reference...")
        self.box.setObjectName("GlobalSearch")
        self.box.setMinimumHeight(38)
        self.box.setStyleSheet("font-size: 12pt;")
        self.box.search.connect(self._text_changed)
        self.box.returnPressed.connect(self._entered)
        self._recent_model = QStringListModel(self)
        comp = QCompleter(self._recent_model, self)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        self.box.setCompleter(comp)
        self.body.addWidget(self.box)
        self.recent_row = QHBoxLayout()
        self.recent_row.setSpacing(4)
        self.body.addLayout(self.recent_row)

        # Quick filters
        self.quick = QButtonGroup(self)
        self.quick.setExclusive(True)
        qrow = QHBoxLayout()
        qrow.setSpacing(4)
        for key, text in QUICK_FILTERS.items():
            b = QPushButton(text)
            b.setCheckable(True)
            b.setAutoDefault(False)
            b.setProperty("quick", key)
            b.setStyleSheet("QPushButton:checked { background: #2F6FB2; color: white; border-color: #2F6FB2; }")
            self.quick.addButton(b)
            qrow.addWidget(b)
            if key == "ALL":
                b.setChecked(True)
        self.quick.buttonClicked.connect(self._quick_clicked)
        qrow.addStretch(1)
        self.body.addLayout(qrow)

        # Active filter chips
        self.chips_w = QWidget()
        self.chips = QHBoxLayout(self.chips_w)
        self.chips.setContentsMargins(0, 0, 0, 0)
        self.chips.setSpacing(6)
        self.body.addWidget(self.chips_w)
        self.chips_w.hide()

        # Exact match (barcode / code) banner with one-click actions
        self.exact_w = QWidget()
        self.exact_w.setObjectName("ExactMatch")
        self.exact_w.setStyleSheet("QWidget#ExactMatch { background: #ECFDF3; border: 1px solid #ABEFC6; "
                                   "border-radius: 6px; }")
        el = QHBoxLayout(self.exact_w)
        el.setContentsMargins(10, 6, 10, 6)
        self.exact_label = label("", "")
        self.exact_label.setStyleSheet("color: #067647; font-weight: 600;")
        el.addWidget(self.exact_label, 1)
        el.addWidget(button("View product", lambda: self._act_exact("view"), shortcut="F4"))
        if app.can(P.GOODS_IN_POST):
            el.addWidget(button("Goods In", lambda: self._act_exact("goods_in"), shortcut="F6"))
        if app.can(P.GOODS_OUT_POST):
            el.addWidget(button("Goods Out", lambda: self._act_exact("goods_out"), shortcut="F7"))
        if app.can(P.STOCK_TRANSFER):
            el.addWidget(button("Transfer", lambda: self._act_exact("transfer"), shortcut="F8"))
        self.body.addWidget(self.exact_w)
        self.exact_w.hide()
        self.exact = None

        # Filters | results | other matches
        split = QSplitter()
        self.panel = FilterPanel(self)
        self.panel.hide()
        split.addWidget(self.panel)
        self.tabs = QTabWidget()
        self.products_view = ResultView(product_result_columns(),
                                        "Check the spelling, try part of the code (e.g. H33), "
                                        "or remove a filter.")
        self.products_view.table.set_row_style(
            lambda r: "muted" if not r.product.is_active else ("low" if r.is_low else None))
        self.products_view.table.activated_item.connect(lambda r: self.product_action("view", r))
        self.products_view.table.sort_requested.connect(lambda k, d: self._sort(self.PRODUCTS, k, d))
        self.products_view.table.show_sort(None, False)
        self._product_buttons()
        self.movements_view = ResultView(movement_result_columns(), "Try a wider date range or fewer filters.")
        self.movements_view.table.activated_item.connect(self._open_movement)
        self.movements_view.table.sort_requested.connect(lambda k, d: self._sort(self.MOVEMENTS, k, d))
        self.movements_view.table.show_sort("date", True)
        self._movement_buttons()
        if self.can_products:
            self.tabs.addTab(self.products_view, "Products")
        if self.can_movements:
            self.tabs.addTab(self.movements_view, "Transactions")
        self.tabs.currentChanged.connect(lambda _: self._run_current())
        split.addWidget(self.tabs)
        side = QWidget()
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.addWidget(label("Other matches", "SectionTitle"))
        self.matches = QListWidget()
        self.matches.setWordWrap(True)
        self.matches.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.matches.itemActivated.connect(self._open_match)
        self.matches.itemClicked.connect(self._open_match)
        sl.addWidget(self.matches, 1)
        sl.addWidget(label("Bins, projects, suppliers and documents matching the search. Click to open.",
                           "Muted", wrap=True))
        side.setMinimumWidth(230)
        side.setMaximumWidth(330)
        self.side = side
        split.addWidget(side)
        split.setStretchFactor(1, 1)
        self.body.addWidget(split, 1)
        self._refresh_recent()

    # -- building blocks ----------------------------------------------------
    def _product_buttons(self):
        acts = [("View Product", "view"), ("View History", "history"), ("View Photo", "photo"),
                ("Open Delivery Note", "delivery_note")]
        if self.app.can(P.GOODS_IN_POST):
            acts.insert(1, ("Goods In", "goods_in"))
        if self.app.can(P.GOODS_OUT_POST):
            acts.insert(2, ("Goods Out", "goods_out"))
        if self.app.can(P.STOCK_TRANSFER):
            acts.insert(3, ("Transfer", "transfer"))
        table = self.products_view.table
        for text, key in acts:
            self.products_view.actions.addWidget(button(text, lambda k=key: self.product_action(k)))
            if key != "view":
                table.add_menu_action(text, lambda k=key: self.product_action(k))
        self.products_view.actions.addStretch(1)

    def _movement_buttons(self):
        v = self.movements_view
        for text, slot in (("Open Document", lambda: self._open_movement(v.table.current())),
                           ("View Product", lambda: self._movement_product("view")),
                           ("View History", lambda: self._movement_product("history"))):
            v.actions.addWidget(button(text, slot))
        v.table.add_menu_action("View Product", lambda: self._movement_product("view"))
        v.table.add_menu_action("View History", lambda: self._movement_product("history"))
        v.actions.addStretch(1)

    # -- page lifecycle -----------------------------------------------------
    def refresh(self):
        if not self.panel._options_loaded and self.panel.isVisible():
            self.panel.load_options()
        self._stale = {self.PRODUCTS: True, self.MOVEMENTS: True}
        self._run_current()
        self._update_matches()

    def search_for(self, text: str) -> None:
        """Called by the top bar / scanner: show results for ``text`` straight away."""
        self.box.blockSignals(True)
        self.box.setText(text)
        self.box.blockSignals(False)
        self._entered()
        self.box.setFocus()
        self.box.selectAll()

    def focus_box(self) -> None:
        self.box.setFocus()
        self.box.selectAll()

    # -- events -------------------------------------------------------------
    def _text_changed(self, text: str):
        self.criteria.text = text.strip()
        self._invalidate()
        self._update_matches()

    def _entered(self):
        self.box._t.stop()
        self._text_changed(self.box.text())
        if self.criteria.text:
            self.app.services.search.remember(self.criteria.text)
            self._refresh_recent()
        self.box.selectAll()  # the next scan replaces this one

    def filters_changed(self):
        try:
            self.panel.read_into(self.criteria)
        except WMSError as exc:
            show_error(self, exc)
            return
        self._invalidate()

    def _quick_clicked(self, b):
        self.criteria.quick = b.property("quick")
        self._invalidate()

    def _toggle_filters(self):
        show = self.filters_btn.isChecked()
        if show and not self.panel._options_loaded:
            self.panel.load_options()
            self.panel.load(self.criteria)
        self.panel.setVisible(show)

    def _invalidate(self):
        self._stale = {self.PRODUCTS: True, self.MOVEMENTS: True}
        # An old count on the other tab would describe the previous search.
        self.tabs.setTabText(self.tabs.indexOf(self.products_view), "Products")
        self.tabs.setTabText(self.tabs.indexOf(self.movements_view), "Transactions")
        self._render_chips()
        self._run_current()

    def _sort(self, which: int, key: str, desc: bool):
        self.sorts[which] = (key, desc)
        self._stale[which] = True
        self._run_current()

    def _current_kind(self) -> int:
        w = self.tabs.currentWidget()
        return self.MOVEMENTS if w is self.movements_view else self.PRODUCTS

    def _run_current(self):
        kind = self._current_kind()
        if not self._stale.get(kind) or self.tabs.count() == 0:
            return
        svc = self.app.services.search
        key, desc = self.sorts[kind]
        try:
            if kind == self.PRODUCTS:
                res = svc.products(self.criteria, sort=key, descending=desc)
                self.products_view.show_result(res, "product")
            else:
                res = svc.movements(self.criteria, sort=key, descending=desc)
                self.movements_view.show_result(res, "transaction")
        except WMSError as exc:
            show_error(self, exc)
            return
        except Exception as exc:  # noqa: BLE001 - the error must reach the user, not kill the page
            show_error(self, exc)
            return
        self._stale[kind] = False
        idx = self.tabs.indexOf(self.products_view if kind == self.PRODUCTS else self.movements_view)
        self.tabs.setTabText(idx, f"{'Products' if kind == self.PRODUCTS else 'Transactions'} ({res.total:,})")
        self._show_exact(res.exact)

    def _show_exact(self, product):
        self.exact = product
        if product is None:
            self.exact_w.hide()
            return
        self.exact_label.setText(f"Exact match: {product.internal_code} - {product.name}"
                                 + (f"   (barcode {product.barcode})" if product.barcode else ""))
        self.exact_w.show()

    def _update_matches(self):
        self.matches.clear()
        hits = self.app.services.search.matches(self.criteria.text) if self.criteria.text else []
        for hit in hits:
            item = QListWidgetItem(f"{hit.kind}:  {hit.title}\n{hit.detail}")
            item.setData(Qt.ItemDataRole.UserRole, hit)
            item.setToolTip("Open " + hit.kind.lower())
            self.matches.addItem(item)
        self.side.setVisible(bool(hits))

    def _open_match(self, item):
        hit = item.data(Qt.ItemDataRole.UserRole)
        self._remember()
        tab = {"SITE": "materials", "SUPPLIER": "products"}.get(hit.entity_type)
        self.app.open_entity(hit.entity_type, hit.entity_id, self, tab=tab)
        self.app.changed()

    # -- chips --------------------------------------------------------------
    def _render_chips(self):
        while self.chips.count():
            w = self.chips.takeAt(0).widget()
            if w is not None:
                w.hide()  # gone at once, not only after the next event-loop pass
                w.deleteLater()
        chips = self.app.services.search.describe(self.criteria)
        for key, text in chips:
            b = QPushButton(f"{text}   ×")
            b.setAutoDefault(False)
            b.setToolTip("Remove this filter")
            b.setStyleSheet("QPushButton { background: #EFF8FF; border: 1px solid #B2DDFF; color: #175CD3; "
                            "border-radius: 12px; padding: 3px 10px; }")
            b.clicked.connect(lambda _=False, k=key: self.remove_filter(k))
            self.chips.addWidget(b)
        if chips:
            self.chips.addWidget(button("Clear All Filters", self.clear_filters))
        self.chips.addStretch(1)
        self.chips_w.setVisible(bool(chips))

    def remove_filter(self, key: str) -> None:
        self.criteria.clear(key)
        if self.panel._options_loaded:
            self.panel.load(self.criteria)
        self._invalidate()

    def clear_filters(self) -> None:
        self.criteria.clear_filters()
        if self.panel._options_loaded:
            self.panel.load(self.criteria)
        for b in self.quick.buttons():
            b.setChecked(b.property("quick") == "ALL")
        self._invalidate()

    # -- saved and recent ----------------------------------------------------
    def _refresh_recent(self):
        while self.recent_row.count():
            w = self.recent_row.takeAt(0).widget()
            if w is not None:
                w.hide()  # gone at once, not only after the next event-loop pass
                w.deleteLater()
        recent = self.app.services.search.recent()
        self._recent_model.setStringList(recent)
        if not recent:
            return
        self.recent_row.addWidget(label("Recent:", "Muted"))
        for text in recent:
            b = QPushButton(text)
            b.setFlat(True)
            b.setAutoDefault(False)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet("QPushButton { color: #2F6FB2; border: none; padding: 2px 6px; } "
                            "QPushButton:hover { text-decoration: underline; }")
            b.clicked.connect(lambda _=False, t=text: self.search_for(t))
            self.recent_row.addWidget(b)
        clear = QPushButton("clear")
        clear.setFlat(True)
        clear.setStyleSheet("QPushButton { color: #98A2B3; border: none; padding: 2px 6px; }")
        clear.clicked.connect(lambda: (self.app.services.search.clear_recent(), self._refresh_recent()))
        self.recent_row.addWidget(clear)
        self.recent_row.addStretch(1)

    def _remember(self):
        if self.criteria.text:
            self.app.services.search.remember(self.criteria.text)
            QTimer.singleShot(0, self._refresh_recent)

    def save_search(self):
        if not self.criteria.has_filters and not self.criteria.text:
            info(self, "Save search", "Set some filters first (for example Supplier and Stock status), then save "
                                      "them under a name so they can be reopened with one click.")
            return
        name, ok = QInputDialog.getText(self, "Save search", "Name for this search (e.g. 'Low Stock - Egger'):")
        if not ok or not name.strip():
            return
        svc = self.app.services.search
        try:
            svc.save_search(name, self.criteria)
        except WMSError as exc:
            if "already have" in exc.message and confirm(self, "Save search", f"{exc.message}\n\nReplace it?",
                                                          yes="Replace"):
                run(self, svc.save_search, name, self.criteria, True)
            else:
                show_error(self, exc)
            return
        if self.main_window is not None:
            self.main_window.statusBar().showMessage(f"Search '{name.strip()}' saved", 5000)

    def _saved_menu(self):
        m = QMenu(self)
        saved = self.app.services.search.saved_searches()
        if not saved:
            act = m.addAction("No saved searches yet - set filters and press 'Save search...'")
            act.setEnabled(False)
        for s in saved:
            m.addAction(s.name, lambda s=s: self.open_saved(s))
        if saved:
            m.addSeparator()
            delete = m.addMenu("Delete")
            for s in saved:
                delete.addAction(s.name, lambda s=s: self._delete_saved(s))
        m.exec(self.saved_btn.mapToGlobal(self.saved_btn.rect().bottomLeft()))

    def open_saved(self, saved) -> None:
        self.apply_criteria(SearchCriteria.from_json(saved.criteria))

    def apply_criteria(self, c: SearchCriteria) -> None:
        """Show the results of a whole search (saved search, or a link from another screen)."""
        self.criteria = c
        self.box.blockSignals(True)
        self.box.setText(c.text)
        self.box.blockSignals(False)
        for b in self.quick.buttons():
            b.setChecked(b.property("quick") == c.quick)
        if self.panel._options_loaded:
            self.panel.load(c)
        # A transaction-type search opens on the Transactions list.
        if c.movement_types and self.can_movements:
            self.tabs.setCurrentWidget(self.movements_view)
        self._invalidate()
        self._update_matches()

    def _delete_saved(self, saved):
        if confirm(self, "Delete saved search", f"Delete the saved search '{saved.name}'?", yes="Delete"):
            run(self, self.app.services.search.delete_saved, saved.id)

    # -- actions on results ---------------------------------------------------
    def _act_exact(self, action: str):
        if self.exact is not None:
            self.product_action(action, product_id=self.exact.id)

    def product_action(self, action: str, row=None, product_id: int | None = None):
        if product_id is None:
            row = row or self.products_view.table.current()
            if row is None:
                info(self, "Search", "Select a product in the list first.")
                return
            product_id = row.product.id
        self._remember()
        app = self.app
        if action == "view":
            app.open_entity(EntityType.PRODUCT.value, product_id, self)
        elif action == "history":
            app.open_entity(EntityType.PRODUCT.value, product_id, self, tab="history")
        elif action == "photo":
            att = app.services.attachments.primary_photo(EntityType.PRODUCT.value, product_id)
            if att is None:
                info(self, "View photo", "This product has no photo yet. Open the product and use the Photos tab "
                                         "to add one.")
                return
            from wms.ui.widgets.attachments import ViewerDialog
            ViewerDialog(app.services.attachments.path_of(att), att.original_name, self).exec()
            return
        elif action == "delivery_note":
            dns = app.services.goods_in.search(product_id=product_id, limit=1)
            if not dns:
                info(self, "Open delivery note", "This product has not arrived on any delivery note yet.")
                return
            app.open_entity(EntityType.DELIVERY_NOTE.value, dns[0][0].id, self)
        elif action in ("goods_in", "goods_out"):
            self._go_post(action, product_id)
            return
        elif action == "transfer":
            from wms.ui.dialogs.stock_dialogs import TransferDialog
            levels = app.services.products.stock_by_bin(product_id)
            from_bin = max(levels, key=lambda lvl: lvl.quantity).bin_id if levels else None
            if TransferDialog(app, product_id, self, from_bin_id=from_bin).exec():
                app.changed()
            return
        app.changed()

    def _go_post(self, action: str, product_id: int):
        mw = self.main_window
        if mw is None:
            return
        product = self.app.services.products.get(product_id)
        mw.go(action)
        page = mw.page(action)
        if page is None:
            return
        if action == "goods_in":
            page.add_product(product_id, supplier_id=self.criteria.supplier_id or product.supplier_id)
        else:
            page.add_product(product_id, site_id=self.criteria.site_id)

    def _open_movement(self, m):
        if m is None:
            return
        self._remember()
        from wms.ui.dialogs.product_dialog import open_movement
        open_movement(self.app, m, self)
        self.app.changed()

    def _movement_product(self, action: str):
        m = self.movements_view.table.current()
        if m is not None:
            self.product_action(action, product_id=m.product_id)


"""Product record: details, photos, stock by bin, movement history, audit."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import AttachmentKind, EntityType, FinishedStatus, ItemType, MovementType
from wms.core.numbers import fmt_datetime, fmt_qty
from wms.core.permissions import P
from wms.services.search import location_of, movement_reference, movement_route, signed_qty
from wms.ui.widgets.attachments import AttachmentPanel, PhotoThumb
from wms.ui.widgets.common import (
    DATETIME,
    QTY,
    Col,
    DataTable,
    DecimalEdit,
    OptionalDateEdit,
    PickCombo,
    button,
    h,
    label,
    run,
    show_error,
)


class ProductDialog(QDialog):
    def __init__(self, app, product_id: int | None, parent=None, default_type: str | None = None,
                 tab: str | None = None):
        super().__init__(parent)
        self.app = app
        self.product_id = product_id
        self.product = app.services.products.get(product_id) if product_id else None
        self.saved = False
        self.can_edit = app.can(P.PRODUCTS_EDIT)
        self.setWindowTitle(f"Product {self.product.internal_code}" if self.product else "New product")
        self.resize(1100, 760)
        lay = QVBoxLayout(self)
        self.header = label("", "PageTitle")
        lay.addWidget(self.header)
        self.tabs = QTabWidget()
        lay.addWidget(self.tabs, 1)
        if product_id:
            self.tabs.addTab(self._build_overview(), "Overview")
        self._build_details(default_type)
        self.photos = AttachmentPanel(app, EntityType.PRODUCT.value, AttachmentKind.PHOTO.value)
        self.photos.changed.connect(self._refresh_thumb)
        self.tabs.addTab(self.photos, "Photos")
        self.mv_table = DataTable(product_history_columns())
        self.mv_table.activated_item.connect(self._open_movement_doc)
        mv_w = QWidget()
        ml = QVBoxLayout(mv_w)
        ml.addWidget(label("Everything that happened to this material, newest first. + came into the "
                           "warehouse, - left it. Double-click a line to open its document.", "Muted", wrap=True))
        ml.addWidget(self.mv_table, 1)
        self.mv_count = label("", "Muted")
        ml.addWidget(self.mv_count)
        self.tabs.addTab(mv_w, "Movement History")
        self.history_tab = mv_w
        self.audit_table = DataTable([Col("When", lambda a: a.occurred_at, DATETIME, 130),
                                      Col("User", lambda a: a.username, width=100),
                                      Col("Action", lambda a: a.action, width=170),
                                      Col("Summary", lambda a: a.summary or "", width=260),
                                      Col("Changes", lambda a: _changes(a))])
        if app.can(P.AUDIT_VIEW):
            self.tabs.addTab(self.audit_table, "Audit")
        self.btn_save = button("Save", self.save, primary=True, shortcut="Ctrl+S")
        self.btn_save.setVisible(self.can_edit)
        extra = []
        if self.product and self.can_edit:
            if self.product.is_active:
                extra.append(button("Delete product", self.delete_product, danger=True))
            else:
                extra.append(button("Restore product", self.restore_product))
        lay.addLayout(h(*extra, None, button("Close", self.reject), self.btn_save))
        self._load()
        if tab == "history":
            self.tabs.setCurrentWidget(self.history_tab)
        elif tab == "photos":
            self.tabs.setCurrentWidget(self.photos)

    def delete_product(self):
        from wms.ui.dialogs.removal import delete_record
        if delete_record(self, self.app.services.products.removal_plan, self.app.services.products.remove,
                         self.product.id):
            self.saved = True
            self.app.changed()
            self.accept()

    def restore_product(self):
        from wms.ui.dialogs.removal import restore_record
        if restore_record(self, "Product", self.product.internal_code, self.app.services.products.restore,
                          self.product.id):
            self.saved = True
            self.app.changed()
            self.accept()

    # -- overview tab (what search opens) -------------------------------------
    def _build_overview(self) -> QWidget:
        w = QWidget()
        grid = QGridLayout(w)
        grid.setHorizontalSpacing(24)
        self.ov_form = QFormLayout()
        self.ov_fields: dict[str, QLabel] = {}
        for key, text in (("code", "Code"), ("supplier_code", "Supplier code"), ("barcode", "Barcode"),
                          ("name", "Name"), ("description", "Description"), ("brand", "Brand"),
                          ("supplier", "Supplier"), ("category", "Category"), ("type", "Type"),
                          ("status", "Status")):
            lab = label("", wrap=True)
            lab.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            self.ov_fields[key] = lab
            self.ov_form.addRow(f"{text}:", lab)
        left = QVBoxLayout()
        left.addWidget(label("Product information", "SectionTitle"))
        left.addLayout(self.ov_form)
        left.addStretch(1)
        self.ov_thumb = PhotoThumb(self.app, 220)
        left.addWidget(self.ov_thumb)
        right = QVBoxLayout()
        self.ov_total = label("", "CardValue")
        right.addWidget(label("Current stock", "SectionTitle"))
        right.addWidget(self.ov_total)
        right.addWidget(label("Stock locations", "SectionTitle"))
        self.bins_table = DataTable([
            Col("Location", lambda l: location_of(l.bin), width=260),
            Col("Bin", lambda l: l.bin.code, width=110),
            Col("Quantity", lambda l: l.quantity, QTY, 100),
            Col("Available for issue", lambda l: "Yes" if l.bin.is_available else "No (quarantine)")])
        self.bins_table.activated_item.connect(lambda l: self.app.open_entity("BIN", l.bin_id, self))
        right.addWidget(self.bins_table, 1)
        self.bins_total = label("", "SectionTitle")
        right.addWidget(self.bins_total)
        actions = []
        if self.app.can(P.STOCK_TRANSFER):
            actions.append(button("Transfer between bins...", self.transfer))
        if self.app.can(P.STOCK_ADJUST):
            actions.append(button("Stock adjustment...", self.adjust))
        actions.append(button("Movement history", lambda: self.tabs.setCurrentWidget(self.history_tab)))
        right.addLayout(h(*actions, None))
        grid.addLayout(left, 0, 0)
        grid.addLayout(right, 0, 1)
        grid.setColumnStretch(0, 2)
        grid.setColumnStretch(1, 3)
        return w

    def _load_overview(self, p, levels):
        f = self.ov_fields
        f["code"].setText(p.internal_code)
        f["supplier_code"].setText(p.supplier_code or "-")
        f["barcode"].setText(p.barcode or "-")
        f["name"].setText(p.name)
        f["description"].setText(p.description or "-")
        f["brand"].setText(p.brand or "-")
        f["supplier"].setText(p.supplier.name if p.supplier else "-")
        f["category"].setText(p.category.full_name if p.category else "-")
        f["type"].setText(ItemType(p.item_type).label)
        f["status"].setText("Active" if p.is_active else "Archived")
        self.ov_thumb.show_for(EntityType.PRODUCT.value, p.id)
        total = sum((l.quantity for l in levels), 0)
        self.ov_total.setText(f"{fmt_qty(total)} {p.unit.code}")
        self.bins_table.set_items(levels)
        self.bins_total.setText(f"Total = {fmt_qty(total)} {p.unit.code}  in {len(levels)} bin(s)")

    # -- details tab --------------------------------------------------------
    def _build_details(self, default_type):
        w = QWidget()
        grid = QGridLayout(w)
        grid.setHorizontalSpacing(24)
        left = QFormLayout()
        right = QFormLayout()
        self.item_type = QComboBox()
        for val, text in ItemType.choices():
            self.item_type.addItem(text, val)
        if default_type:
            self.item_type.setCurrentIndex(self.item_type.findData(default_type))
        self.item_type.currentIndexChanged.connect(self._type_changed)
        self.code = QLineEdit()
        self.name = QLineEdit()
        self.supplier_code = QLineEdit()
        self.barcode = QLineEdit()
        self.brand = QLineEdit()
        self.brand.setPlaceholderText("e.g. Egger, Blum, Hettich")
        self.description = QPlainTextEdit()
        self.description.setFixedHeight(70)
        self.category = PickCombo("None")
        self.unit = PickCombo("Choose unit", allow_empty=False)
        self.supplier = PickCombo("None")
        self.default_bin = PickCombo("None")
        self.min_stock = DecimalEdit()
        self.reorder = DecimalEdit()
        self.cost = DecimalEdit("0.0001")
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(60)
        self.active = QCheckBox("Active")
        self.active.setChecked(True)
        left.addRow("Item type *", self.item_type)
        left.addRow("Internal product code *", self.code)
        left.addRow("Product name *", self.name)
        left.addRow("Description", self.description)
        left.addRow("Brand", self.brand)
        left.addRow("Category", self.category)
        left.addRow("Unit of measure *", self.unit)
        left.addRow("Supplier", self.supplier)
        left.addRow("Supplier product code", self.supplier_code)
        left.addRow("Barcode", self.barcode)
        right.addRow("Default storage bin", self.default_bin)
        right.addRow("Minimum stock level", self.min_stock)
        right.addRow("Reorder quantity", self.reorder)
        right.addRow("Unit cost (£)", self.cost)
        right.addRow("Notes", self.notes)
        right.addRow("", self.active)
        # finished product extras
        self.fin_box = QWidget()
        fl = QFormLayout(self.fin_box)
        fl.setContentsMargins(0, 0, 0, 0)
        self.f_status = QComboBox()
        for val, text in FinishedStatus.choices():
            self.f_status.addItem(text, val)
        self.f_date = OptionalDateEdit()
        self.f_site = PickCombo("None")
        self.f_customer = QLineEdit()
        fl.addRow(label("Finished product", "SectionTitle"))
        fl.addRow("Status", self.f_status)
        fl.addRow("Production date", self.f_date)
        fl.addRow("Project / site", self.f_site)
        fl.addRow("Customer", self.f_customer)
        right.addRow(self.fin_box)
        self.thumb = PhotoThumb(self.app, 200)
        self.info = label("", "Muted", wrap=True)
        side = QVBoxLayout()
        side.addWidget(self.thumb)
        side.addWidget(self.info)
        side.addStretch(1)
        grid.addLayout(left, 0, 0)
        grid.addLayout(right, 0, 1)
        grid.addLayout(side, 0, 2)
        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 3)
        self.tabs.addTab(w, "Details")
        for wdg in (self.item_type, self.code, self.name, self.supplier_code, self.barcode, self.brand, self.description,
                    self.category, self.unit, self.supplier, self.default_bin, self.min_stock, self.reorder,
                    self.cost, self.notes, self.active, self.f_status, self.f_site, self.f_customer, self.f_date):
            wdg.setEnabled(self.can_edit)

    def _type_changed(self):
        self.fin_box.setVisible(self.item_type.currentData() == ItemType.FINISHED.value)

    def _refresh_thumb(self):
        self.thumb.show_for(EntityType.PRODUCT.value, self.product_id)

    def _load(self):
        app = self.app
        self.category.set_options([(c.full_name, c.id) for c in app.services.reference.categories()])
        self.unit.set_options([(f"{u.code} - {u.name}", u.id) for u in app.services.reference.units()])
        self.supplier.set_options(app.supplier_options())
        self.default_bin.set_options(app.bin_options())
        self.f_site.set_options(app.site_options(open_only=False))
        p = self.product
        if p is None:
            self.header.setText("New product")
            self.photos.set_entity(None)
            self.info.setText("Save the product first, then add photos.")
            for i in range(1, self.tabs.count()):
                self.tabs.setTabEnabled(i, False)
            self._type_changed()
            self.code.setFocus()
            return
        self.header.setText(f"{p.internal_code}  -  {p.name}")
        self.item_type.setCurrentIndex(self.item_type.findData(p.item_type))
        self.code.setText(p.internal_code)
        self.name.setText(p.name)
        self.supplier_code.setText(p.supplier_code or "")
        self.barcode.setText(p.barcode or "")
        self.brand.setText(p.brand or "")
        self.description.setPlainText(p.description or "")
        self.category.set_value(p.category_id)
        self.unit.set_value(p.unit_id)
        self.supplier.set_value(p.supplier_id)
        self.default_bin.set_value(p.default_bin_id)
        self.min_stock.set_value(p.min_stock)
        self.reorder.set_value(p.reorder_qty)
        self.cost.set_value(p.unit_cost)
        self.notes.setPlainText(p.notes or "")
        self.active.setChecked(p.is_active)
        fi = p.finished_info
        if fi:
            self.f_status.setCurrentIndex(self.f_status.findData(fi.status))
            self.f_date.set_value(fi.production_date)
            self.f_site.set_value(fi.site_id)
            self.f_customer.setText(fi.customer or "")
        self._type_changed()
        self.photos.set_entity(p.id)
        self._refresh_thumb()
        levels = app.services.products.stock_by_bin(p.id)
        total = sum((l.quantity for l in levels), 0)
        self.info.setText(f"On hand: {fmt_qty(total)} {p.unit.code}\nCreated {fmt_datetime(p.created_at)}\n"
                          f"Last updated {fmt_datetime(p.updated_at)}")
        self._load_overview(p, levels)
        moves = app.services.products.movements(p.id)
        self.mv_table.set_items(moves)
        self.mv_count.setText(f"{len(moves)} movement(s)" + (" - showing the latest 1000" if len(moves) >= 1000
                                                            else ""))
        if app.can(P.AUDIT_VIEW):
            self.audit_table.set_items(app.services.audit.for_entity(EntityType.PRODUCT.value, p.id))

    def _data(self) -> dict:
        return {
            "item_type": self.item_type.currentData(),
            "internal_code": self.code.text(),
            "name": self.name.text(),
            "supplier_code": self.supplier_code.text(),
            "barcode": self.barcode.text(),
            "brand": self.brand.text(),
            "description": self.description.toPlainText(),
            "category_id": self.category.value(),
            "unit_id": self.unit.value(),
            "supplier_id": self.supplier.value(),
            "default_bin_id": self.default_bin.value(),
            "min_stock": self.min_stock.value(),
            "reorder_qty": self.reorder.value(),
            "unit_cost": self.cost.value(),
            "notes": self.notes.toPlainText(),
            "is_active": self.active.isChecked(),
            "status": self.f_status.currentData(),
            "production_date": self.f_date.value(),
            "site_id": self.f_site.value(),
            "customer": self.f_customer.text(),
        }

    def save(self):
        try:
            data = self._data()
        except Exception as e:  # noqa: BLE001 - invalid number or date
            show_error(self, e)
            return
        version = self.product.version if self.product else None
        p = run(self, self.app.services.products.save, self.product_id, data, version)
        if p is None:
            return
        self.saved = True
        was_new = self.product_id is None
        self.product_id, self.product = p.id, p
        self.setWindowTitle(f"Product {p.internal_code}")
        if was_new:
            self.tabs.insertTab(0, self._build_overview(), "Overview")
        for i in range(self.tabs.count()):
            self.tabs.setTabEnabled(i, True)
        self._load()
        if was_new:
            self.tabs.setCurrentWidget(self.photos)

    def transfer(self):
        from wms.ui.dialogs.stock_dialogs import TransferDialog
        if TransferDialog(self.app, self.product_id, self).exec():
            self.saved = True
            self._load()

    def adjust(self):
        from wms.ui.dialogs.stock_dialogs import AdjustmentDialog
        bin_ = self.bins_table.current()
        if AdjustmentDialog(self.app, self.product_id, bin_.bin_id if bin_ else self.product.default_bin_id,
                            self).exec():
            self.saved = True
            self._load()

    def _open_movement_doc(self, m):
        open_movement(self.app, m, self)


def product_history_columns() -> list[Col]:
    """Movement history as a warehouse user reads it: 25/10/2026 | Goods In | +50 | Egger -> Bin B01."""
    return [
        Col("Date / Time", lambda m: m.occurred_at, DATETIME, 130),
        Col("Movement Type", lambda m: MovementType(m.movement_type).label + (" (reversal)" if m.reversal_of_id
                                                                              else ""), width=160),
        Col("Quantity", signed_qty, QTY, 85),
        Col("From", lambda m: movement_route(m)[0], width=220),
        Col("To", lambda m: movement_route(m)[1], width=220),
        Col("Project", lambda m: m.site_label, width=170),
        Col("Supplier", lambda m: m.supplier_label, width=140),
        Col("Reference", movement_reference, width=140),
        Col("User", lambda m: m.user_label, width=120),
        Col("Notes", lambda m: m.notes or "")]


def open_movement(app, m, parent) -> None:
    """Open the document behind a movement, or the movement itself when it has no document screen."""
    if m.doc_type in ("DELIVERY_NOTE", "GOODS_ISSUE", "RETURN", "PRODUCTION") and m.doc_id:
        app.open_entity(m.doc_type, m.doc_id, parent)
    else:
        from wms.ui.pages.movements import MovementDetailDialog
        MovementDetailDialog(app, m, parent).exec()


def _changes(a) -> str:
    import json
    if not a.new_values:
        return ""
    try:
        new = json.loads(a.new_values)
        old = json.loads(a.old_values) if a.old_values else {}
    except ValueError:
        return a.new_values
    parts = []
    for k, v in new.items():
        if isinstance(v, (list, dict)):
            continue
        parts.append(f"{k}: {old.get(k, '')} -> {v}" if k in old else f"{k}: {v}")
    return "; ".join(parts)

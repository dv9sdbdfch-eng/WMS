"""Warehouse > Zone > Rack > Shelf > Bin structure."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QLineEdit,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import LocationKind
from wms.core.permissions import P
from wms.ui.pages.base import Page
from wms.ui.widgets.common import Col, DataTable, PickCombo, button, h, info, label, run


class LocationDialog(QDialog):
    def __init__(self, app, loc=None, parent_id: int | None = None, parent=None):
        super().__init__(parent)
        self.app, self.loc = app, loc
        self.setWindowTitle("Edit location" if loc else "New location")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.kind = QComboBox()
        for val, text in LocationKind.choices():
            self.kind.addItem(text, val)
        self.parent_loc = PickCombo("None (top level - warehouse)")
        self.parent_loc.set_options([(l.path, l.id) for l in app.services.locations.tree()
                                     if not loc or l.id != loc.id])
        self.code = QLineEdit()
        self.code.setPlaceholderText("Short code, e.g. MAIN, A, R03, S02")
        self.name = QLineEdit()
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(60)
        self.active = QCheckBox("Active")
        self.active.setChecked(True)
        form.addRow("Type *", self.kind)
        form.addRow("Inside", self.parent_loc)
        form.addRow("Code *", self.code)
        form.addRow("Name", self.name)
        form.addRow("Notes", self.notes)
        form.addRow("", self.active)
        lay.addLayout(form)
        lay.addWidget(label("Warehouse > Zone > Rack > Shelf. Bins are added to any of these.", "Muted"))
        lay.addLayout(h(None, button("Cancel", self.reject), button("Save", self.save, primary=True)))
        if loc:
            self.kind.setCurrentIndex(self.kind.findData(loc.kind))
            self.parent_loc.set_value(loc.parent_id)
            self.code.setText(loc.code)
            self.name.setText(loc.name)
            self.notes.setPlainText(loc.notes or "")
            self.active.setChecked(loc.is_active)
        elif parent_id:
            self.parent_loc.set_value(parent_id)
            parent_kind = next((l.kind for l in app.services.locations.tree() if l.id == parent_id), None)
            nxt = {"WAREHOUSE": "ZONE", "ZONE": "RACK", "RACK": "SHELF"}.get(parent_kind)
            if nxt:
                self.kind.setCurrentIndex(self.kind.findData(nxt))

    def save(self):
        data = {"kind": self.kind.currentData(), "parent_id": self.parent_loc.value(), "code": self.code.text(),
                "name": self.name.text(), "notes": self.notes.toPlainText(), "is_active": self.active.isChecked()}
        if run(self, self.app.services.locations.save_location, self.loc.id if self.loc else None, data):
            self.accept()


class BinDialog(QDialog):
    def __init__(self, app, b=None, location_id: int | None = None, parent=None):
        super().__init__(parent)
        self.app, self.b = app, b
        self.setWindowTitle("Edit bin" if b else "New bin")
        self.setMinimumWidth(480)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.location = PickCombo("Choose location...")
        self.location.set_options(app.location_options())
        self.code = QLineEdit()
        self.code.setPlaceholderText("e.g. B04")
        self.description = QLineEdit()
        self.available = QCheckBox("Stock in this bin can be issued (untick for quarantine / damaged goods)")
        self.available.setChecked(True)
        self.active = QCheckBox("Active")
        self.active.setChecked(True)
        self.notes = QPlainTextEdit()
        self.notes.setFixedHeight(60)
        form.addRow("Location *", self.location)
        form.addRow("Bin code *", self.code)
        form.addRow("Description", self.description)
        form.addRow("", self.available)
        form.addRow("", self.active)
        form.addRow("Notes", self.notes)
        lay.addLayout(form)
        lay.addLayout(h(None, button("Cancel", self.reject), button("Save", self.save, primary=True)))
        if b:
            self.location.set_value(b.location_id)
            self.code.setText(b.code)
            self.description.setText(b.description or "")
            self.available.setChecked(b.is_available)
            self.active.setChecked(b.is_active)
            self.notes.setPlainText(b.notes or "")
        elif location_id:
            self.location.set_value(location_id)

    def save(self):
        data = {"location_id": self.location.value(), "code": self.code.text(),
                "description": self.description.text(), "is_available": self.available.isChecked(),
                "is_active": self.active.isChecked(), "notes": self.notes.toPlainText()}
        if run(self, self.app.services.locations.save_bin, self.b.id if self.b else None, data):
            self.accept()


class BulkBinsDialog(QDialog):
    def __init__(self, app, location_id: int | None, parent=None):
        super().__init__(parent)
        self.app = app
        self.setWindowTitle("Create several bins")
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.location = PickCombo("Choose location...")
        self.location.set_options(app.location_options())
        if location_id:
            self.location.set_value(location_id)
        self.prefix = QLineEdit("B")
        self.start, self.end, self.width = QSpinBox(), QSpinBox(), QSpinBox()
        for sb, lo, hi, v in ((self.start, 0, 9999, 1), (self.end, 0, 9999, 10), (self.width, 1, 5, 2)):
            sb.setRange(lo, hi)
            sb.setValue(v)
        self.example = label("", "Muted")
        for w in (self.start, self.end, self.width):
            w.valueChanged.connect(self._example)
        self.prefix.textChanged.connect(self._example)
        form.addRow("Location *", self.location)
        form.addRow("Prefix", self.prefix)
        form.addRow("From number", self.start)
        form.addRow("To number", self.end)
        form.addRow("Digits", self.width)
        form.addRow("", self.example)
        lay.addLayout(form)
        lay.addLayout(h(None, button("Cancel", self.reject), button("Create bins", self.save, primary=True)))
        self._example()

    def _example(self):
        p, w = self.prefix.text().strip().upper(), self.width.value()
        self.example.setText(f"Creates {p}{self.start.value():0{w}d} ... {p}{self.end.value():0{w}d}. "
                             f"Existing codes are skipped.")

    def save(self):
        if not self.location.value():
            info(self, "Bins", "Choose the location.")
            return
        n = run(self, self.app.services.locations.bulk_create_bins, self.location.value(), self.prefix.text(),
                self.start.value(), self.end.value(), self.width.value())
        if n is not None:
            info(self, "Bins created", f"{n} bin(s) created.")
            self.accept()


class LocationsPage(Page):
    title = "Locations / Bins"
    subtitle = "Warehouse > Zone > Rack > Shelf > Bin. Double-click a bin to see what is in it."

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.can_edit = app.can(P.LOCATIONS_EDIT)
        if self.can_edit:
            self.add_action(button("New location", self.new_location))
            self.add_action(button("New bin", self.new_bin, primary=True))
            self.add_action(button("Create several bins", self.bulk_bins))
        if app.can(P.STOCK_TRANSFER):
            self.add_action(button("Transfer between bins", self.transfer))
        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Location", "Type"])
        self.tree.setColumnWidth(0, 220)
        self.tree.currentItemChanged.connect(lambda *_: self._load_bins())
        self.tree.itemDoubleClicked.connect(lambda *_: self.edit_location())
        ll.addWidget(self.tree, 1)
        self.show_inactive = QCheckBox("Show inactive")
        self.show_inactive.toggled.connect(lambda _: self.refresh())
        btns = [self.show_inactive, None]
        if self.can_edit:
            btns.append(button("Edit location", self.edit_location))
        ll.addLayout(h(*btns))
        split.addWidget(left)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        self.bins_title = label("All bins", "SectionTitle")
        rl.addWidget(self.bins_title)
        self.bins = DataTable([Col("Bin", lambda b: b.full_code, width=300),
                               Col("Code", lambda b: b.code, width=80),
                               Col("Description", lambda b: b.description or "", width=200),
                               Col("Issuable", lambda b: "Yes" if b.is_available else "No - quarantine", width=120),
                               Col("Active", lambda b: "Yes" if b.is_active else "No", width=60),
                               Col("Notes", lambda b: b.notes or "")])
        self.bins.set_row_style(lambda b: "muted" if not b.is_active else ("low" if not b.is_available else None))
        self.bins.activated_item.connect(self.open_bin)
        rl.addWidget(self.bins, 1)
        rb = [label("", "Muted"), None, button("Contents", lambda: self.open_bin(self.bins.current()))]
        self.bin_count = rb[0]
        if self.can_edit:
            rb.append(button("Edit bin", self.edit_bin))
        rl.addLayout(h(*rb))
        split.addWidget(right)
        split.setSizes([380, 900])
        self.body.addWidget(split, 1)
        self._locs = {}

    def refresh(self):
        current = self._current_id()
        locs = self.app.services.locations.tree(include_inactive=self.show_inactive.isChecked())
        self._locs = {l.id: l for l in locs}
        self.tree.clear()
        items: dict[int, QTreeWidgetItem] = {}
        for loc in locs:  # ordered by path so parents come first
            parent = items.get(loc.parent_id)
            it = QTreeWidgetItem([f"{loc.code}  {loc.name if loc.name != loc.code else ''}".strip(),
                                  LocationKind(loc.kind).label])
            it.setData(0, Qt.ItemDataRole.UserRole, loc.id)
            if not loc.is_active:
                it.setForeground(0, Qt.GlobalColor.gray)
            (parent.addChild(it) if parent else self.tree.addTopLevelItem(it))
            items[loc.id] = it
        self.tree.expandAll()
        if current in items:
            self.tree.setCurrentItem(items[current])
        self._load_bins()

    def _current_id(self):
        it = self.tree.currentItem()
        return it.data(0, Qt.ItemDataRole.UserRole) if it else None

    def _load_bins(self):
        loc_id = self._current_id()
        bins = self.app.services.locations.bins(loc_id, include_inactive=self.show_inactive.isChecked())
        self.bins.set_items(bins)
        self.bins_title.setText(f"Bins in {self._locs[loc_id].path}" if loc_id in self._locs else "All bins")
        self.bin_count.setText(f"{len(bins)} bin(s)")

    def open_bin(self, b):
        if b is not None:
            self.app.open_entity("BIN", b.id, self)
            self.app.changed()

    def new_location(self):
        if LocationDialog(self.app, None, self._current_id(), self).exec():
            self.app.changed()

    def edit_location(self):
        loc = self._locs.get(self._current_id())
        if loc and self.can_edit and LocationDialog(self.app, loc, None, self).exec():
            self.app.changed()

    def new_bin(self):
        if BinDialog(self.app, None, self._current_id(), self).exec():
            self.app.changed()

    def edit_bin(self):
        b = self.bins.current()
        if b and BinDialog(self.app, b, None, self).exec():
            self.app.changed()

    def bulk_bins(self):
        if BulkBinsDialog(self.app, self._current_id(), self).exec():
            self.app.changed()

    def transfer(self):
        from wms.ui.dialogs.stock_dialogs import TransferDialog
        b = self.bins.current()
        TransferDialog(self.app, None, self, b.id if b else None).exec()

"""Editable document lines (product, quantity, bin, ...) for Goods In/Out, returns and production."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QHeaderView,
    QLineEdit,
    QTableWidget,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import ReturnCondition
from wms.core.exceptions import ValidationError
from wms.core.numbers import fmt_qty
from wms.ui.widgets.common import DecimalEdit, PickCombo, button, h, label


@dataclass
class LineValue:
    row: int
    product_id: int | None
    quantity: Decimal | None
    bin_id: int | None = None
    unit_cost: Decimal | None = None
    notes: str | None = None
    condition: str | None = None


class LinesEditor(QWidget):
    """Columns: Product, Quantity, Unit, [In stock], [Bin], [Condition], [Unit cost], [Notes]."""

    changed = Signal()

    def __init__(self, app, *, show_bin: bool = True, bin_optional: bool = False, show_cost: bool = False,
                 show_stock: bool = False, show_condition: bool = False, product_types: list[str] | None = None,
                 bin_label: str = "Bin", parent=None):
        super().__init__(parent)
        self.app = app
        self.show_bin, self.show_cost, self.show_stock = show_bin, show_cost, show_stock
        self.show_condition, self.bin_optional = show_condition, bin_optional
        self.product_types = product_types
        cols = ["Product", "Quantity", "Unit"]
        if show_stock:
            cols.append("Available")
        if show_bin:
            cols.append(bin_label + (" (blank = automatic)" if bin_optional else ""))
        if show_condition:
            cols.append("Condition")
        if show_cost:
            cols.append("Unit Cost £")
        cols.append("Notes")
        self.cols = cols
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        hh.setStretchLastSection(True)  # Notes takes whatever space is left
        self.table.setColumnWidth(0, 340)
        self.table.setColumnWidth(1, 100)
        self.table.setColumnWidth(2, 65)
        for name, width in (("Available", 90), ("Condition", 110), ("Unit Cost", 100)):
            if any(c.startswith(name) for c in cols):
                self.table.setColumnWidth(self._col(name), width)
        if show_bin:
            self.table.setColumnWidth(self._bin_col_index(), 240)
        lay.addWidget(self.table, 1)
        self.total_label = label("", "Muted")
        lay.addLayout(h(button("Add line", self.add_row, shortcut="Ctrl+L"),
                        button("Remove line", self.remove_row), None, self.total_label))
        self._products: dict[int, object] = {}
        self._product_opts: list = []
        self._bin_opts: list = []
        self.reload_options()

    def reload_options(self):
        rows = self.app.services.products.list(item_types=self.product_types)
        self._products = {r.product.id: r for r in rows}
        self._product_opts = [(f"{r.product.internal_code} - {r.product.name}", r.product.id) for r in rows]
        self._bin_opts = self.app.bin_options()

    def _col(self, name_start: str) -> int:
        return next(i for i, c in enumerate(self.cols) if c.startswith(name_start))

    def add_row(self, product_id: int | None = None, qty: Decimal | None = None, bin_id: int | None = None):
        r = self.table.rowCount()
        self.table.insertRow(r)
        prod = PickCombo("Type code or name...")
        prod.set_options(self._product_opts)
        prod.picked.connect(lambda _v, row_widget=prod: self._product_picked(row_widget))
        self.table.setCellWidget(r, 0, prod)
        q = DecimalEdit()
        q.textChanged.connect(lambda _: self._update_total())
        # Scanner / keyboard flow: scan (or type a code) + Enter -> product chosen, cursor in Quantity;
        # quantity + Enter -> next line, cursor back in Product, ready for the next scan.
        prod.lineEdit().returnPressed.connect(lambda w=prod, qe=q: self._code_entered(w, qe))
        q.returnPressed.connect(lambda qe=q: self._quantity_entered(qe))
        self.table.setCellWidget(r, 1, q)
        unit = QLineEdit()
        unit.setReadOnly(True)
        unit.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.table.setCellWidget(r, 2, unit)
        if self.show_stock:
            av = QLineEdit()
            av.setReadOnly(True)
            av.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            av.setAlignment(Qt.AlignmentFlag.AlignRight)
            self.table.setCellWidget(r, self._col("Available"), av)
        if self.show_bin:
            b = PickCombo("Automatic" if self.bin_optional else "Choose bin...")
            b.set_options(self._bin_opts)
            self.table.setCellWidget(r, self._bin_col_index(), b)
        if self.show_condition:
            c = QComboBox()
            for val, text in ReturnCondition.choices():
                c.addItem(text, val)
            self.table.setCellWidget(r, self._col("Condition"), c)
        if self.show_cost:
            self.table.setCellWidget(r, self._col("Unit Cost"), DecimalEdit("0.0001"))
        self.table.setCellWidget(r, self._col("Notes"), QLineEdit())
        if product_id:
            prod.set_value(product_id)
            self._product_picked(prod)
        if qty is not None:
            q.set_value(qty)
        if bin_id and self.show_bin:
            self._bin_widget(r).set_value(bin_id)
        prod.setFocus()
        self.changed.emit()

    def _code_entered(self, prod: PickCombo, qty_edit: DecimalEdit) -> None:
        text = prod.currentText().strip()
        if prod.value() is None and text:
            p = self.app.services.products.find_by_code(text)
            if p is None or p.id not in self._products:
                prod.lineEdit().selectAll()
                QApplication.beep()
                self.total_label.setText(f"No product with code / barcode '{text}'")
                return
            prod.set_value(p.id)
            self._product_picked(prod)
        if prod.value() is not None:
            qty_edit.setFocus()
            qty_edit.selectAll()

    def _quantity_entered(self, qty_edit: DecimalEdit) -> None:
        r = next((i for i in range(self.table.rowCount()) if self.table.cellWidget(i, 1) is qty_edit), -1)
        if r < 0 or self.table.cellWidget(r, 0).value() is None or not qty_edit.text().strip():
            return
        if r == self.table.rowCount() - 1:
            self.add_row()
        else:
            self.table.cellWidget(r + 1, 0).setFocus()

    def _bin_col_index(self) -> int:
        return next(i for i, c in enumerate(self.cols) if i > 2 and c not in ("Available", "Condition", "Notes")
                    and not c.startswith("Unit Cost"))

    def _bin_widget(self, r: int) -> PickCombo:
        return self.table.cellWidget(r, self._bin_col_index())

    def _row_of(self, widget) -> int:
        for r in range(self.table.rowCount()):
            if self.table.cellWidget(r, 0) is widget:
                return r
        return -1

    def _product_picked(self, prod_widget: PickCombo):
        r = self._row_of(prod_widget)
        if r < 0:
            return
        pid = prod_widget.value()
        row = self._products.get(pid)
        unit = self.table.cellWidget(r, 2)
        unit.setText(row.product.unit.code if row else "")
        if self.show_stock:
            self.table.cellWidget(r, self._col("Available")).setText(fmt_qty(row.available) if row else "")
        if row and self.show_bin and not self.bin_optional and row.product.default_bin_id:
            bw = self._bin_widget(r)
            if bw.value() is None:
                bw.set_value(row.product.default_bin_id)
        if row and self.show_cost:
            cw = self.table.cellWidget(r, self._col("Unit Cost"))
            if not cw.text().strip() and row.product.unit_cost is not None:
                cw.set_value(row.product.unit_cost)
        self._update_total()
        self.changed.emit()

    def remove_row(self):
        r = self.table.currentRow()
        if r < 0:
            r = self.table.rowCount() - 1
        if r >= 0:
            self.table.removeRow(r)
            self._update_total()
            self.changed.emit()

    def clear(self):
        self.table.setRowCount(0)
        self._update_total()

    def _update_total(self):
        n = 0
        value = Decimal("0")
        for v in self.values(validate=False):
            if v.product_id and v.quantity:
                n += 1
                if v.unit_cost:
                    value += v.unit_cost * v.quantity
        text = f"{n} line(s)"
        if self.show_cost:
            text += f"   ·   Total value £{value.quantize(Decimal('0.01')):,}"
        self.total_label.setText(text)

    def values(self, validate: bool = True) -> list[LineValue]:
        out = []
        for r in range(self.table.rowCount()):
            pid = self.table.cellWidget(r, 0).value()
            try:
                qty = self.table.cellWidget(r, 1).value()
                cost = self.table.cellWidget(r, self._col("Unit Cost")).value() if self.show_cost else None
            except ValidationError:
                if validate:
                    raise ValidationError(f"Line {r + 1}: invalid number.") from None
                qty, cost = None, None
            if pid is None and qty is None:
                continue  # blank row
            lv = LineValue(row=r + 1, product_id=pid, quantity=qty, unit_cost=cost,
                           notes=self.table.cellWidget(r, self._col("Notes")).text().strip() or None)
            if self.show_bin:
                lv.bin_id = self._bin_widget(r).value()
            if self.show_condition:
                lv.condition = self.table.cellWidget(r, self._col("Condition")).currentData()
            if validate:
                if pid is None:
                    raise ValidationError(f"Line {r + 1}: choose a product from the list.")
                if qty is None or qty <= 0:
                    raise ValidationError(f"Line {r + 1}: enter a quantity greater than zero.")
                if self.show_bin and not self.bin_optional and lv.bin_id is None:
                    raise ValidationError(f"Line {r + 1}: choose a bin.")
            out.append(lv)
        return out

    def duplicate_products(self) -> list[str]:
        seen, dups = set(), []
        for v in self.values(validate=False):
            if v.product_id in seen and v.product_id in self._products:
                dups.append(self._products[v.product_id].product.internal_code)
            seen.add(v.product_id)
        return dups

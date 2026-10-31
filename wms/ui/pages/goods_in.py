"""Goods In: enter a supplier delivery note and receive its lines into stock."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

from PySide6.QtWidgets import (
    QFileDialog,
    QFormLayout,
    QLineEdit,
    QListWidget,
    QMenu,
    QPushButton,
    QSplitter,
    QVBoxLayout,
    QWidget,
)

from wms.core.exceptions import ValidationError
from wms.services.goods_in import ReceiptLine
from wms.ui.pages.base import Page
from wms.ui.widgets.attachments import FilePreview
from wms.ui.widgets.common import DateEdit, PickCombo, button, confirm, h, info, label, run, show_error
from wms.ui.widgets.lines import LinesEditor

FILE_FILTER = "Scans and documents (*.pdf *.jpg *.jpeg *.png);;All files (*)"


class GoodsInPage(Page):
    title = "Goods In"
    subtitle = "Receive a supplier delivery. Every line adds stock to the chosen bin."

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        self.add_action(button("Clear form", self.clear))
        # spec 40: stop half-way through a long delivery and finish it later - a draft never touches stock
        self.drafts_btn = QPushButton("Drafts")
        self.drafts_btn.setAutoDefault(False)
        self.drafts_btn.setToolTip("Open a delivery saved as a draft (not yet received)")
        self.drafts_menu = QMenu(self.drafts_btn)
        self.drafts_menu.aboutToShow.connect(self._fill_drafts)
        self.drafts_btn.setMenu(self.drafts_menu)
        self.add_action(self.drafts_btn)
        self.add_action(button("Save Draft", self.save_draft, shortcut="Ctrl+Shift+S",
                               tooltip="Keep this delivery to finish later (Ctrl+Shift+S). "
                               "Stock is not changed until you post it."))
        self._draft_id: int | None = None
        self.post_button = button("Post delivery", self.post, primary=True, shortcut="Ctrl+S")
        self.add_action(self.post_button)
        self._token = uuid.uuid4().hex  # one per filled-in form: the server refuses a second posting
        self._posting = False

        split = QSplitter()
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.supplier = PickCombo("Choose supplier...")
        self.supplier.picked.connect(lambda _: self._supplier_picked())
        self.order = PickCombo("No purchase order")
        self.order.picked.connect(self._order_picked)
        self.dn_number = QLineEdit()
        self.dn_number.setPlaceholderText("Number printed on the supplier's delivery note")
        self.dn_number.editingFinished.connect(self._check_duplicate)
        self.date = DateEdit(date.today())
        self.po = QLineEdit()
        self.po.setPlaceholderText("Optional purchase order number")
        self.notes = QLineEdit()
        form.addRow("Supplier *", self.supplier)
        form.addRow("Purchase order", self.order)
        form.addRow("Delivery note no. *", self.dn_number)
        form.addRow("Delivery date *", self.date)
        form.addRow("PO / reference", self.po)
        form.addRow("Notes", self.notes)
        ll.addLayout(form)
        self.dup_warning = label("", "Banner", wrap=True)
        self.dup_warning.hide()
        ll.addWidget(self.dup_warning)
        self.draft_banner = label("", "InfoBanner", wrap=True)
        self.draft_banner.hide()
        ll.addWidget(self.draft_banner)
        ll.addWidget(label("Products received", "SectionTitle"))
        self.lines = LinesEditor(app, show_bin=True, bin_optional=True, show_cost=True)
        ll.addWidget(self.lines, 1)
        ll.addWidget(label("If the bin is left empty the product's default bin is used. Unit cost updates the "
                           "product's current cost.", "Muted", wrap=True))
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(label("Original delivery note (scan / photo / PDF)", "SectionTitle"))
        self.files = QListWidget()
        self.files.setMaximumHeight(110)
        self.files.currentRowChanged.connect(self._preview)
        rl.addWidget(self.files)
        rl.addLayout(h(button("Attach file...", self.attach), button("Remove", self.remove_file), None))
        self.preview = FilePreview()
        self.preview.empty.setText("Attach the scanned delivery note so it can be checked against the lines.")
        rl.addWidget(self.preview, 1)
        split.addWidget(right)
        split.setSizes([1050, 380])
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 1)
        self.body.addWidget(split, 1)
        self._paths: list[str] = []
        self.lines.add_row()

    def refresh(self):
        self.supplier.set_options(self.app.supplier_options())
        self._order_options()
        self.lines.reload_options()

    # -- purchase orders -----------------------------------------------------
    def _order_options(self):
        sup = self.supplier.value()
        self.order.set_options([(f"{po.po_number} - {po.supplier_label}"
                                 + (f" - {po.site_label}" if po.site_id else "")
                                 + (f" (expected {po.expected_date:%d/%m/%Y})" if po.expected_date else ""), po.id)
                                for po in self.app.services.purchasing.open_orders(sup)])

    def _supplier_picked(self):
        self._order_options()  # keeps the chosen order if it is with this supplier
        self._check_duplicate()

    def _order_picked(self, po_id):
        if po_id and po_id != getattr(self, "_loaded_po", None):
            self.load_purchase_order(po_id)

    def load_purchase_order(self, po_id: int) -> None:
        """Fill the form with what is still to come on this order; change the quantities to what arrived."""
        v = self.app.services.purchasing.get(po_id)
        self._loaded_po = po_id
        self.supplier.set_value(v.po.supplier_id)
        self._order_options()
        self.order.set_value(po_id)
        if not self.po.text().strip():
            self.po.setText(v.po.po_number)
        self.lines.clear()
        for lv in v.lines:
            if lv.outstanding > 0:
                self.lines.add_row(lv.line.product_id, lv.outstanding)
                if lv.line.unit_cost is not None:
                    r = self.lines.table.rowCount() - 1
                    self.lines.table.cellWidget(r, self.lines._col("Unit Cost")).set_value(lv.line.unit_cost)
        if not self.lines.table.rowCount():
            self.lines.add_row()
        self._check_duplicate()

    def add_product(self, product_id: int, supplier_id: int | None = None) -> None:
        """Start receiving a product found in search."""
        if supplier_id and self.supplier.value() is None:
            self.supplier.set_value(supplier_id)
        self.lines.add_product(product_id)

    # -- duplicate protection ----------------------------------------------
    def _check_duplicate(self):
        sup, number = self.supplier.value(), self.dn_number.text().strip()
        existing = self.app.services.goods_in.find_existing(sup, number) if sup and number else None
        if existing:
            self.dup_warning.setText(f"Delivery note {existing.dn_number} from this supplier was already entered on "
                                     f"{existing.delivery_date:%d/%m/%Y}. Open it from Delivery Notes instead.")
        self.dup_warning.setVisible(bool(existing))
        return existing

    # -- attachments ---------------------------------------------------------
    def attach(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach delivery note", "", FILE_FILTER)
        for p in paths:
            try:
                self.app.services.attachments.validate_file(p)
            except ValidationError as e:
                show_error(self, e)
                continue
            if p not in self._paths:
                self._paths.append(p)
                self.files.addItem(p.replace("\\", "/").rsplit("/", 1)[-1])
        if self._paths:
            self.files.setCurrentRow(self.files.count() - 1)

    def remove_file(self):
        r = self.files.currentRow()
        if r >= 0:
            self._paths.pop(r)
            self.files.takeItem(r)
            if not self._paths:
                self.preview.show_file(None)

    def _preview(self, row: int):
        from pathlib import Path
        self.preview.show_file(Path(self._paths[row]) if 0 <= row < len(self._paths) else None)

    # -- drafts (spec 40) ------------------------------------------------------
    def form_data(self) -> dict:
        lines = []
        for v in self.lines.values(validate=False):
            lines.append({"product_id": v.product_id, "quantity": None if v.quantity is None else str(v.quantity),
                          "bin_id": v.bin_id, "unit_cost": None if v.unit_cost is None else str(v.unit_cost),
                          "notes": v.notes})
        try:
            d = self.date.value().isoformat()
        except Exception:  # noqa: BLE001 - half-typed date: keep today's
            d = date.today().isoformat()
        return {"supplier_id": self.supplier.value(), "purchase_order_id": self.order.value(),
                "dn_number": self.dn_number.text(), "delivery_date": d, "po_reference": self.po.text(),
                "notes": self.notes.text(), "lines": lines, "files": list(self._paths)}

    def save_draft(self) -> bool:
        data = self.form_data()
        if not data["lines"] and not data["supplier_id"] and not data["dn_number"].strip():
            info(self, "Save Draft", "The form is empty - nothing to save.")
            return False
        sup = self.supplier.currentText() if data["supplier_id"] else "No supplier"
        title = f"{sup} - DN {data['dn_number'].strip() or '(no number yet)'}"
        saved = run(self, self.app.services.drafts.save, "GOODS_IN", title, data, self._draft_id)
        if saved is None:
            return False
        self._draft_id = saved.id
        self._show_draft_banner(saved)
        mw = getattr(self, "main_window", None)
        if mw is not None:
            mw.statusBar().showMessage(f"Draft saved: {title} - stock not changed")
        return True

    def _show_draft_banner(self, d) -> None:
        self.draft_banner.setText(f"Draft: {d.title} - saved {d.updated_at.astimezone():%d/%m/%Y %H:%M} by "
                                  f"{d.created_by}. Not received yet: stock changes only when you post it.")
        self.draft_banner.show()

    def _fill_drafts(self) -> None:
        m = self.drafts_menu
        m.clear()
        drafts = run(self, self.app.services.drafts.list, "GOODS_IN") or []
        if not drafts:
            m.addAction("No drafts").setEnabled(False)
            return
        for d in drafts:
            m.addAction(f"{d.title}  ·  {d.lines} line(s)  ·  {d.updated_at.astimezone():%d/%m %H:%M}  ·  "
                        f"{d.created_by}", lambda i=d.id: self.open_draft(i))
        if self._draft_id:
            m.addSeparator()
            m.addAction("Delete this draft", self.delete_draft)

    def open_draft(self, draft_id: int) -> bool:
        got = run(self, self.app.services.drafts.load, draft_id)
        if got is None:
            return False
        d, data = got
        self.clear()
        from decimal import Decimal
        if data.get("supplier_id"):
            self.supplier.set_value(data["supplier_id"])
            self._order_options()
        if data.get("purchase_order_id"):
            self._loaded_po = data["purchase_order_id"]  # do not reload the order's lines over the draft
            self.order.set_value(data["purchase_order_id"])
        self.dn_number.setText(data.get("dn_number") or "")
        try:
            self.date.set_value(date.fromisoformat(data.get("delivery_date") or ""))
        except ValueError:
            pass
        self.po.setText(data.get("po_reference") or "")
        self.notes.setText(data.get("notes") or "")
        self.lines.clear()
        for ln in data.get("lines") or []:
            qty = Decimal(ln["quantity"]) if ln.get("quantity") else None
            self.lines.add_row(ln.get("product_id"), qty, ln.get("bin_id"))
            r = self.lines.table.rowCount() - 1
            if ln.get("unit_cost"):
                self.lines.table.cellWidget(r, self.lines._col("Unit Cost")).set_value(Decimal(ln["unit_cost"]))
            if ln.get("notes"):
                self.lines.table.cellWidget(r, self.lines._col("Notes")).setText(ln["notes"])
        if not self.lines.table.rowCount():
            self.lines.add_row()
        from pathlib import Path
        for p in data.get("files") or []:
            if Path(p).exists() and p not in self._paths:  # a file on another PC is simply not found
                self._paths.append(p)
                self.files.addItem(p.replace("\\", "/").rsplit("/", 1)[-1])
        self._draft_id = d.id
        self._show_draft_banner(d)
        self._check_duplicate()
        return True

    def delete_draft(self) -> None:
        if self._draft_id and confirm(self, "Delete draft", "Delete this draft? The form stays filled in.",
                                      yes="Delete"):
            run(self, self.app.services.drafts.delete, self._draft_id)
            self._draft_id = None
            self.draft_banner.hide()

    # -- posting -------------------------------------------------------------
    def clear(self):
        self._draft_id = None
        self.draft_banner.hide()
        self._loaded_po = None
        self.order.set_value(None)
        self.dn_number.clear()
        self.po.clear()
        self.notes.clear()
        self.date.set_value(date.today())
        self.lines.clear()
        self.lines.add_row()
        self._paths.clear()
        self.files.clear()
        self.preview.show_file(None)
        self.dup_warning.hide()
        self._token = uuid.uuid4().hex

    def post(self):
        # Ignore a second click / Ctrl+S that arrives while the first posting is still running.
        if self._posting:
            return
        self._posting = True
        self.post_button.setEnabled(False)
        try:
            self._post()
        finally:
            self._posting = False
            self.post_button.setEnabled(True)

    def _post(self):
        try:
            values = self.lines.values()
        except ValidationError as e:
            show_error(self, e)
            return
        if not self.supplier.value() or not self.dn_number.text().strip():
            info(self, "Goods In", "Choose the supplier and enter the delivery note number.")
            return
        if not values:
            info(self, "Goods In", "Add at least one product line.")
            return
        if self._check_duplicate():
            info(self, "Already entered", self.dup_warning.text())
            return
        d = self.date.value()
        if d < date.today() - timedelta(days=30) and not confirm(
                self, "Old delivery date", f"The delivery date {d:%d/%m/%Y} is more than 30 days ago. Is it correct?"):
            return
        dups = self.lines.duplicate_products()
        if dups and not confirm(self, "Repeated products",
                                "These products appear on more than one line: " + ", ".join(dups) +
                                "\n\nThat is fine if they go into different bins. Continue?"):
            return
        if not self._paths and not confirm(self, "No scan attached",
                                           "No scan of the delivery note is attached. Post without it? "
                                           "You can attach it later from Delivery Notes."):
            return
        header = {"supplier_id": self.supplier.value(), "purchase_order_id": self.order.value(),
                  "dn_number": self.dn_number.text(), "delivery_date": d,
                  "po_reference": self.po.text(), "notes": self.notes.text(), "client_token": self._token}
        lines = [ReceiptLine(v.product_id, v.quantity, v.bin_id, v.unit_cost, v.notes) for v in values]
        dn = run(self, self.app.services.goods_in.post, header, lines, list(self._paths))
        if dn and self._draft_id:
            try:
                self.app.services.drafts.delete(self._draft_id, "posted")
            except Exception:  # noqa: BLE001 - the delivery is posted; a leftover draft is harmless
                pass
        if dn:
            self.app.changed()
            text = f"Delivery note {dn.dn_number} has been posted: {len(dn.lines)} line(s) added to stock."
            if dn.purchase_order_id:
                v = self.app.services.purchasing.get(dn.purchase_order_id)
                text += f"\n\nPurchase order {v.po.po_number} is now {v.po.status.replace('_', ' ').lower()}."
                problems = self.app.services.purchasing.discrepancies(delivery_note_id=dn.id)
                if problems:
                    text += "\n\nRecorded as delivery problems:\n" + "\n".join(
                        f"  {d.number}: {d.kind_label} - {d.product.internal_code if d.product else ''} "
                        f"{d.quantity.normalize():f}" for d in problems)
            info(self, "Delivery posted", text)
            self.clear()
            self._order_options()

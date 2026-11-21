"""Drawings of a project or a unit (v1.37): list with revision and status, preview, open.

PDF and pictures are previewed here. DWG / DXF files have no preview: "Open" starts the PC's own
viewer (for example the free Autodesk DWG TrueView). Files are always opened from a read-only
copy, so saving in AutoCAD cannot change the stored drawing (a new version is added with
"New revision...").
"""

from __future__ import annotations

import shutil
import stat
import tempfile
import uuid
from pathlib import Path

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QMenu,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import DocStatus
from wms.core.numbers import fmt_datetime
from wms.core.permissions import P
from wms.ui.widgets import tasks
from wms.ui.widgets.attachments import FilePreview, ViewerDialog
from wms.ui.widgets.common import button, confirm, info, label, run, show_error

DRAWING_FILTER = "Drawings (*.pdf *.dwg *.dxf *.jpg *.jpeg *.png *.bmp *.gif *.webp);;All files (*)"
STATUS_COLOURS = {DocStatus.APPROVED.value: "#067647", DocStatus.PRELIMINARY.value: "#B54708",
                  DocStatus.SUPERSEDED.value: "#98A2B3"}
CAD = {".dwg", ".dxf"}
ATT = Qt.ItemDataRole.UserRole + 1


def size_text(n: int) -> str:
    return f"{n / 1048576:.1f} MB" if n >= 1048576 else f"{max(n // 1024, 1)} KB"


def open_read_only(path: Path, name: str) -> Path:
    """Copy the file to a temporary folder under its own name, make the copy read-only and open
    it in the PC's default program. Returns the copy."""
    root = Path(tempfile.gettempdir()) / "StoreWMS-view"
    _prune(root)
    folder = root / uuid.uuid4().hex[:8]
    folder.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in name if c not in '<>:"/\\|?*').strip() or path.name
    copy = folder / safe
    shutil.copyfile(path, copy)
    copy.chmod(stat.S_IREAD)
    QDesktopServices.openUrl(QUrl.fromLocalFile(str(copy)))
    return copy


def _prune(root: Path, max_age_s: float = 86400) -> None:
    """Delete view copies older than a day, so large drawings do not pile up in the temp folder."""
    import time
    if not root.is_dir():
        return
    cutoff = time.time() - max_age_s
    for folder in root.iterdir():
        try:
            if folder.is_dir() and folder.stat().st_mtime < cutoff:
                for f in folder.iterdir():
                    f.chmod(stat.S_IWRITE | stat.S_IREAD)
                shutil.rmtree(folder, ignore_errors=True)
        except OSError:
            pass  # still open in another program: try again next time


class DrawingDialog(QDialog):
    """Revision, status and an optional note for the drawing(s) being added."""

    def __init__(self, parent, title: str, files: list[str], revision: str = "", status: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        lay = QVBoxLayout(self)
        names = ", ".join(Path(f).name for f in files[:4]) + (f" and {len(files) - 4} more" if len(files) > 4 else "")
        lay.addWidget(label(names, wrap=True))
        form = QFormLayout()
        self.revision = QLineEdit(revision)
        self.revision.setPlaceholderText("as written on the drawing, e.g. C or P2")
        self.revision.setMaxLength(20)
        self.status = QComboBox()
        for st in (DocStatus.APPROVED, DocStatus.PRELIMINARY):
            self.status.addItem(st.label, st.value)
        self.status.setCurrentIndex(max(self.status.findData(status or DocStatus.APPROVED.value), 0))
        self.note = QLineEdit()
        self.note.setPlaceholderText("optional")
        self.note.setMaxLength(200)
        form.addRow("Revision", self.revision)
        form.addRow("Status *", self.status)
        form.addRow("Note", self.note)
        lay.addLayout(form)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(button("Cancel", self.reject))
        row.addWidget(button("Save", self.accept, primary=True))
        lay.addLayout(row)


class DrawingsPanel(QWidget):
    """The Drawings tab of a project or unit. ``set_entity(type, id, parent=(type, id, title))``:
    a unit also lists its project's drawings (read only here, under "From project")."""

    changed = Signal()

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.entity: tuple[str, int] | None = None
        self.parent_entity: tuple[str, int, str] | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 4, 0, 0)
        lay.setSpacing(4)
        self.can_add = app.can(P.ATTACHMENTS_ADD)
        self.btn_add = button("+ Add...", self.add, tooltip="Attach drawings: PDF, DWG, DXF or pictures "
                                                                 "(several at once)")
        self.btn_rev = button("New rev...", self.new_revision,
                              tooltip="Add the new version of the selected drawing; the old one is kept as Superseded")
        self.btn_open = button("Open", self.open_current, tooltip="Open in the PC's own program (read-only copy)")
        self.btn_more = button("More ▾", self._more_menu, tooltip="Mark Approved / Preliminary, remove")
        for b in (self.btn_add, self.btn_rev):
            b.setVisible(self.can_add)
        bar = QHBoxLayout()
        bar.setSpacing(4)
        for b in (self.btn_add, self.btn_rev):
            bar.addWidget(b)
        bar.addStretch(1)
        bar.addWidget(self.btn_open)
        bar.addWidget(self.btn_more)
        lay.addLayout(bar)
        self.split = QSplitter(Qt.Orientation.Vertical)
        self.list = QTreeWidget()
        self.list.setColumnCount(3)
        self.list.setHeaderLabels(["File", "Rev", "Status"])
        self.list.setRootIsDecorated(False)
        self.list.setIndentation(8)
        self.list.setColumnWidth(0, 200)
        self.list.setColumnWidth(1, 42)
        self.list.setUniformRowHeights(True)
        self.list.currentItemChanged.connect(lambda *_: self._preview())
        self.list.itemDoubleClicked.connect(lambda *_: self.open_current())
        self.list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.list.customContextMenuRequested.connect(lambda pos: self._more_menu(self.list.viewport()
                                                                                 .mapToGlobal(pos)))
        self.split.addWidget(self.list)
        self.preview = FilePreview()
        self.split.addWidget(self.preview)
        self.split.setSizes([150, 260])
        self.split.setChildrenCollapsible(False)
        lay.addWidget(self.split, 1)
        self.hint = label("", "Muted", wrap=True)
        lay.addWidget(self.hint)
        self._own = 0

    # ---- data -----------------------------------------------------------------------------------
    @property
    def svc(self):
        return self.app.services.attachments

    def set_entity(self, entity_type: str, entity_id: int | None,
                   parent: tuple[str, int, str] | None = None) -> None:
        self.entity = (entity_type, entity_id) if entity_id is not None else None
        self.parent_entity = parent
        self.refresh()

    def refresh(self) -> None:
        self.list.clear()
        self._own = 0
        self.preview.show_file(None)
        if self.entity is None:
            self._buttons()
            return
        own = run(self, self.svc.drawings_for, *self.entity) or []
        self._own = sum(a.doc_status != DocStatus.SUPERSEDED.value for a in own)
        for a in own:
            self.list.addTopLevelItem(self._row(a, editable=True))
        if self.parent_entity:
            ptype, pid, ptitle = self.parent_entity
            from_parent = [a for a in (run(self, self.svc.drawings_for, ptype, pid) or [])
                           if a.doc_status != DocStatus.SUPERSEDED.value]
            if from_parent:
                group = QTreeWidgetItem([f"From project: {ptitle} ({len(from_parent)})", "", ""])
                f = QFont()
                f.setItalic(True)
                group.setFont(0, f)
                group.setFirstColumnSpanned(True)
                group.setFlags(Qt.ItemFlag.ItemIsEnabled)
                self.list.addTopLevelItem(group)
                for a in from_parent:
                    group.addChild(self._row(a, editable=False))
                group.setExpanded(True)
                self.list.setRootIsDecorated(True)
        if own or self.list.topLevelItemCount():
            self.list.setCurrentItem(self.list.topLevelItem(0) if own else None)
        self.hint.setText("" if own or self.parent_entity else
                          "No drawings yet. Add the PDF / DWG drawings here; the stored copy never changes "
                          "(a new version is added with New revision...).")
        if not own and self.parent_entity:
            self.hint.setText("No drawings for this unit yet.")
        self._buttons()

    def _row(self, a, editable: bool) -> QTreeWidgetItem:
        status = DocStatus(a.doc_status).label if a.doc_status else ""
        it = QTreeWidgetItem([a.original_name, a.revision or "", status])
        it.setData(0, ATT, a)
        it.setData(1, ATT, editable)
        tip = (f"{a.original_name}\nRevision {a.revision or '-'} · {status or '-'} · {size_text(a.size_bytes)}\n"
               f"Added by {a.uploaded_by.full_name if a.uploaded_by else '?'} {fmt_datetime(a.uploaded_at)}")
        if a.caption:
            tip += f"\n{a.caption}"
        for col in range(3):
            it.setToolTip(col, tip)
        colour = STATUS_COLOURS.get(a.doc_status or "")
        if colour:
            it.setForeground(2, QBrush(QColor(colour)))
        if a.doc_status == DocStatus.SUPERSEDED.value:
            for col in range(3):
                it.setForeground(col, QBrush(QColor(STATUS_COLOURS[DocStatus.SUPERSEDED.value])))
        bold = QFont()
        bold.setBold(a.doc_status == DocStatus.APPROVED.value)
        it.setFont(2, bold)
        return it

    def current(self):
        it = self.list.currentItem()
        return it.data(0, ATT) if it is not None else None

    def _editable(self) -> bool:
        it = self.list.currentItem()
        return bool(it is not None and it.data(1, ATT))

    def _buttons(self) -> None:
        a = self.current()
        self.btn_add.setEnabled(self.entity is not None)
        self.btn_rev.setEnabled(a is not None and self._editable() and a.doc_status != DocStatus.SUPERSEDED.value)
        self.btn_open.setEnabled(a is not None)
        self.btn_more.setEnabled(a is not None and self._editable())

    @property
    def count(self) -> int:
        """Current (not superseded) drawings of this record itself."""
        return self._own

    # ---- preview / open -------------------------------------------------------------------------
    def _local(self, a) -> Path | None:
        """The file on this PC (fetched from the shared server first if needed, off the screen thread)."""
        try:
            path = tasks.with_progress(self, "Drawing", lambda _p: self.svc.path_of(a),
                                       text=f"Fetching {a.original_name}...")
        except tasks.Cancelled:
            return None
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return None
        if not path.is_file():
            info(self, "File missing", f"The file for '{a.original_name}' cannot be found.")
            return None
        return path

    def _preview(self) -> None:
        self._buttons()
        a = self.current()
        if a is None:
            self.preview.show_file(None)
            return
        if Path(a.original_name).suffix.lower() in CAD:
            self.preview.show_file(None)
            self.preview.empty.setText(f"No preview for {Path(a.original_name).suffix.upper()[1:]} drawings.\n"
                                       "Use Open (for example in the free Autodesk DWG TrueView).")
            return
        if self.svc.shared and not (self.svc.store / a.stored_name).is_file():
            # on the shared server a large file is fetched only when asked for
            self.preview.show_file(None)
            self.preview.empty.setText(f"{size_text(a.size_bytes)} on the server. Double-click or Open to view it.")
            return
        self.preview.show_file(self.svc.path_of(a))

    def open_current(self) -> None:
        a = self.current()
        if a is None:
            return
        path = self._local(a)
        if path is None:
            return
        if Path(a.original_name).suffix.lower() in CAD:
            try:
                open_read_only(path, a.original_name)
            except OSError as exc:
                show_error(self, exc, "Cannot open the drawing")
            return
        dlg = ViewerDialog(path, f"{a.original_name} - rev {a.revision or '-'}", self)
        dlg.exec()

    # ---- changes --------------------------------------------------------------------------------
    def _pick(self, many: bool) -> list[str]:
        if many:
            files, _ = QFileDialog.getOpenFileNames(self, "Choose drawings", str(Path.home()), DRAWING_FILTER)
            return files
        path, _ = QFileDialog.getOpenFileName(self, "Choose the new revision", str(Path.home()), DRAWING_FILTER)
        return [path] if path else []

    def add(self) -> None:
        if self.entity is None:
            return
        files = self._pick(True)
        if not files:
            return
        dlg = DrawingDialog(self, "Add drawings", files)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if self._upload(f"Uploading {len(files)} file(s)...", self.svc.add_drawings, *self.entity, files,
                        dlg.revision.text(), dlg.status.currentData(), dlg.note.text()) is not None:
            self.refresh()
            self.changed.emit()

    def new_revision(self) -> None:
        a = self.current()
        if a is None or not self._editable():
            return
        files = self._pick(False)
        if not files:
            return
        dlg = DrawingDialog(self, f"New revision of {a.original_name} (now rev {a.revision or '-'})", files,
                            status=DocStatus.APPROVED.value)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        if self._upload(f"Uploading {Path(files[0]).name}...", self.svc.new_revision, a.id, files[0],
                        dlg.revision.text(), dlg.status.currentData(), dlg.note.text()) is not None:
            self.refresh()
            self.changed.emit()

    def _upload(self, text: str, fn, *args):
        """Save files off the screen thread (large drawings over VPN take a while). None on error."""
        try:
            return tasks.with_progress(self, "Drawings", lambda _p: fn(*args), text=text)
        except tasks.Cancelled:
            return None
        except Exception as exc:  # noqa: BLE001
            show_error(self, exc)
            return None

    def set_status(self, status: str) -> None:
        a = self.current()
        if a is not None and run(self, self.svc.set_doc_status, a.id, status) is not False:
            self.refresh()
            self.changed.emit()

    def remove(self) -> None:
        a = self.current()
        if a is None:
            return
        older = "" if a.doc_status == DocStatus.SUPERSEDED.value else (
            "\n\nThis is the current revision: older revisions stay marked Superseded, so nothing will be "
            "current until you add a new one.")
        if confirm(self, "Remove drawing", f"Remove '{a.original_name}' rev {a.revision or '-'}?\n\nThe file is "
                   f"kept in the archive and the removal is recorded in the audit trail.{older}", yes="Remove",
                   danger=True):
            run(self, self.svc.remove, a.id)
            self.refresh()
            self.changed.emit()

    def _more_menu(self, pos=None) -> None:
        a = self.current()
        if a is None or not self._editable():
            return
        m = QMenu(self)
        if self.can_add and a.doc_status != DocStatus.SUPERSEDED.value:
            for st in (DocStatus.APPROVED, DocStatus.PRELIMINARY):
                act = m.addAction(f"Mark {st.label}", lambda s=st.value: self.set_status(s))
                act.setEnabled(a.doc_status != st.value)
            m.addSeparator()
        if self.app.can(P.ATTACHMENTS_DELETE):
            m.addAction("Remove...", self.remove)
        if m.isEmpty():
            return
        if pos is None:
            pos = self.btn_more.mapToGlobal(self.btn_more.rect().bottomLeft())
        m.exec(pos)

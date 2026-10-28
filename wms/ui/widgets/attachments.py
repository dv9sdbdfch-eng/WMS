"""Photo and document attachments: thumbnails, viewer (images and PDF)."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QPixmap
from PySide6.QtWidgets import (
    QDialog,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from wms.core.enums import AttachmentKind
from wms.core.numbers import fmt_datetime
from wms.core.permissions import P
from wms.ui.widgets.common import button, confirm, h, info, label, run

FILE_FILTER = "Images and PDF (*.jpg *.jpeg *.png *.bmp *.gif *.webp *.pdf);;All files (*)"
PHOTO_FILTER = "Images (*.jpg *.jpeg *.png *.bmp *.gif *.webp)"

try:  # QtPdf is optional at runtime; fall back to the system viewer.
    from PySide6.QtPdf import QPdfDocument
    from PySide6.QtPdfWidgets import QPdfView
    HAS_PDF = True
except ImportError:  # pragma: no cover
    HAS_PDF = False


class FilePreview(QWidget):
    """Shows an image (fit to width, Ctrl+wheel not needed) or a PDF inline."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget()
        self.empty = label("No document selected.", "Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.image_label = QLabel()
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setWidget(self.image_label)
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.scroll)
        if HAS_PDF:
            self.pdf_doc = QPdfDocument(self)
            self.pdf_view = QPdfView()
            self.pdf_view.setDocument(self.pdf_doc)
            self.pdf_view.setPageMode(QPdfView.PageMode.MultiPage)
            self.pdf_view.setZoomMode(QPdfView.ZoomMode.FitToWidth)
            self.stack.addWidget(self.pdf_view)
        lay.addWidget(self.stack)
        self._pix: QPixmap | None = None
        self.path: Path | None = None

    def show_file(self, path: Path | None):
        self.path = Path(path) if path else None
        if self.path is None or not self.path.is_file():
            self.empty.setText("File not found." if path else "No document selected.")
            self.stack.setCurrentWidget(self.empty)
            return
        if self.path.suffix.lower() == ".pdf":
            if HAS_PDF:
                self.pdf_doc.load(str(self.path))
                self.stack.setCurrentWidget(self.pdf_view)
            else:
                self.empty.setText("PDF preview unavailable - use Open to view it.")
                self.stack.setCurrentWidget(self.empty)
            return
        self._pix = QPixmap(str(self.path))
        self._fit()
        self.stack.setCurrentWidget(self.scroll)

    def _fit(self):
        if self._pix and not self._pix.isNull():
            w = max(self.scroll.viewport().width() - 8, 100)
            self.image_label.setPixmap(self._pix.scaledToWidth(min(w, self._pix.width()),
                                                               Qt.TransformationMode.SmoothTransformation))

    def resizeEvent(self, e):  # noqa: N802
        super().resizeEvent(e)
        if self.stack.currentWidget() is self.scroll:
            self._fit()


class ViewerDialog(QDialog):
    def __init__(self, path: Path, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(1000, 800)
        lay = QVBoxLayout(self)
        prev = FilePreview()
        prev.show_file(path)
        lay.addWidget(prev, 1)
        lay.addLayout(h(None, button("Open in default app",
                                     lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))),
                        button("Close", self.accept)))


class AttachmentPanel(QWidget):
    """Lists photos/documents for one record with Add / Replace / Remove / Open."""

    changed = Signal()

    def __init__(self, app, entity_type: str, kind: str | None = None, parent=None, thumbs: bool = True):
        super().__init__(parent)
        self.app = app
        self.entity_type = entity_type
        self.kind = kind
        self.entity_id: int | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.list = QListWidget()
        if thumbs:
            self.list.setViewMode(QListWidget.ViewMode.IconMode)
            self.list.setIconSize(QSize(150, 150))
            self.list.setGridSize(QSize(176, 196))
            self.list.setResizeMode(QListWidget.ResizeMode.Adjust)
            self.list.setMovement(QListWidget.Movement.Static)
            self.list.setWordWrap(True)
        self.list.itemDoubleClicked.connect(lambda _: self.open_current())
        lay.addWidget(self.list, 1)
        can_add = app.can(P.ATTACHMENTS_ADD)
        self.btn_add = button("Add photo..." if kind == AttachmentKind.PHOTO.value else "Attach file...", self.add)
        self.btn_replace = button("Replace...", self.replace)
        self.btn_primary = button("Set as main photo", self.set_primary)
        self.btn_open = button("Open", self.open_current)
        self.btn_remove = button("Remove", self.remove, danger=True)
        for b in (self.btn_add, self.btn_replace, self.btn_primary):
            b.setVisible(can_add)
        self.btn_remove.setVisible(app.can(P.ATTACHMENTS_DELETE))
        if kind != AttachmentKind.PHOTO.value:
            self.btn_replace.hide()
            self.btn_primary.hide()
        lay.addLayout(h(self.btn_add, self.btn_replace, self.btn_primary, None, self.btn_open, self.btn_remove))
        self._items: list = []

    def set_entity(self, entity_id: int | None):
        self.entity_id = entity_id
        self.setEnabled(entity_id is not None)
        self.refresh()

    def refresh(self):
        self.list.clear()
        self._items = []
        if self.entity_id is None:
            return
        svc = self.app.services.attachments
        for att in svc.list_for(self.entity_type, self.entity_id, self.kind):
            path = svc.path_of(att)
            text = att.original_name + ("\n(main photo)" if att.is_primary else "") \
                + f"\n{fmt_datetime(att.uploaded_at)}"
            item = QListWidgetItem(text)
            item.setToolTip(f"{att.original_name}\nAdded by {att.uploaded_by.full_name} {fmt_datetime(att.uploaded_at)}")
            if att.is_image and path.is_file():
                item.setIcon(QIcon(QPixmap(str(path)).scaled(150, 150, Qt.AspectRatioMode.KeepAspectRatio,
                                                             Qt.TransformationMode.SmoothTransformation)))
            else:
                item.setIcon(self.style().standardIcon(self.style().StandardPixmap.SP_FileIcon))
            self.list.addItem(item)
            self._items.append(att)

    def current(self):
        row = self.list.currentRow()
        return self._items[row] if 0 <= row < len(self._items) else None

    def _pick_file(self, photo: bool) -> str | None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose file", str(Path.home()),
                                              PHOTO_FILTER if photo else FILE_FILTER)
        return path or None

    def _warn_duplicate(self, path: str) -> bool:
        dups = run(self, self.app.services.attachments.find_duplicates, path) or []
        if not dups:
            return True
        where = ", ".join(sorted({f"{d.entity_type.replace('_', ' ').title()} #{d.entity_id}" for d in dups}))
        return confirm(self, "Same file already attached",
                       f"This exact file is already attached to: {where}.\n\nAttach it again anyway?")

    def add(self):
        if self.entity_id is None:
            return
        path = self._pick_file(self.kind == AttachmentKind.PHOTO.value)
        if not path or not self._warn_duplicate(path):
            return
        if run(self, self.app.services.attachments.add, self.entity_type, self.entity_id, path, self.kind):
            self.refresh()
            self.changed.emit()

    def replace(self):
        att = self.current()
        if att is None:
            info(self, "Replace photo", "Select the photo to replace first.")
            return
        path = self._pick_file(True)
        if path and run(self, self.app.services.attachments.replace_photo, att.id, path):
            self.refresh()
            self.changed.emit()

    def set_primary(self):
        att = self.current()
        if att and run(self, self.app.services.attachments.set_primary, att.id) is not False:
            self.refresh()
            self.changed.emit()

    def remove(self):
        att = self.current()
        if att is None:
            return
        if confirm(self, "Remove attachment", f"Remove '{att.original_name}'?\n\nThe file is kept in the archive and "
                   f"the removal is recorded in the audit trail.", yes="Remove", danger=True):
            run(self, self.app.services.attachments.remove, att.id)
            self.refresh()
            self.changed.emit()

    def open_current(self):
        att = self.current()
        if att is None:
            return
        path = self.app.services.attachments.path_of(att)
        if not path.is_file():
            info(self, "File missing", f"The file for '{att.original_name}' cannot be found at\n{path}")
            return
        ViewerDialog(path, att.original_name, self).exec()


class PhotoThumb(QLabel):
    """Main photo of a record, scaled; click to open."""

    def __init__(self, app, size: int = 180, parent=None):
        super().__init__(parent)
        self.app = app
        self.size_ = size
        self.setFixedSize(size, size)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setStyleSheet("background: #F2F4F7; border: 1px solid #E4E7EC; border-radius: 8px; color: #98A2B3;")
        self.path: Path | None = None
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def show_for(self, entity_type: str, entity_id: int | None):
        self.path = None
        att = self.app.services.attachments.primary_photo(entity_type, entity_id) if entity_id else None
        if att:
            p = self.app.services.attachments.path_of(att)
            if p.is_file():
                self.path = p
                self.setPixmap(QPixmap(str(p)).scaled(self.size_ - 4, self.size_ - 4,
                                                      Qt.AspectRatioMode.KeepAspectRatio,
                                                      Qt.TransformationMode.SmoothTransformation))
                return
        self.setPixmap(QPixmap())
        self.setText("No photo")

    def mousePressEvent(self, e):  # noqa: N802
        if self.path:
            ViewerDialog(self.path, self.path.name, self).exec()


__all__ = ["AttachmentPanel", "FilePreview", "PhotoThumb", "ViewerDialog", "HAS_PDF", "QHBoxLayout"]

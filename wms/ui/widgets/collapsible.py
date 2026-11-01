"""A section with a one-line header that folds its content away (v1.14, Dashboard workspace).

* The header stays visible all the time: chevron, title, an optional count and optional quiet
  widgets on the right (for example the "Last successful backup" line).
* A click anywhere on the header, or on the chevron (also Space / Enter on the chevron), expands or
  collapses the content.
* With ``settings_key`` the expanded / collapsed state is kept between sessions on this PC
  (QSettings, the same place as the table columns and the sidebar).

When the content is hidden Qt's layout gives the freed height to whatever has a stretch factor below
it (the stock table), so nothing has to be recalculated by hand: resizing, maximising and folding
all go through the same layout pass.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QSettings, Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QSizePolicy, QToolButton, QVBoxLayout, QWidget

log = logging.getLogger(__name__)

CHEVRON_OPEN, CHEVRON_CLOSED = "▾", "▸"


def _qs() -> QSettings:
    return QSettings(QSettings.Format.IniFormat, QSettings.Scope.UserScope, "WMS", "WMS")


class _Header(QFrame):
    clicked = Signal()

    def mouseReleaseEvent(self, event):  # noqa: N802 - Qt name
        if event.button() == Qt.MouseButton.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class CollapsibleSection(QWidget):
    """``section.body`` is the content layout; ``add_header_widget`` puts a widget on the right of the header."""

    toggled = Signal(bool)  # True = expanded

    def __init__(self, title: str, settings_key: str | None = None, expanded: bool = True,
                 tooltip: str | None = None, parent=None):
        super().__init__(parent)
        self.title = title
        self.settings_key = settings_key
        self._expanded = expanded
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self.header = _Header()
        self.header.setObjectName("SectionHeader")
        self.header.setCursor(Qt.CursorShape.PointingHandCursor)
        self.header.setToolTip(tooltip or "Click to show or hide this section")
        hl = QHBoxLayout(self.header)
        hl.setContentsMargins(4, 2, 8, 2)
        hl.setSpacing(6)
        self.chevron = QToolButton()
        self.chevron.setObjectName("SectionChevron")
        self.chevron.setAutoRaise(True)
        self.chevron.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.chevron.clicked.connect(self.toggle)
        hl.addWidget(self.chevron)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("SectionHeaderTitle")
        hl.addWidget(self.title_label)
        self.count_label = QLabel("")
        self.count_label.setObjectName("SectionCount")
        self.count_label.setVisible(False)
        hl.addWidget(self.count_label)
        self.hint_label = QLabel("")
        self.hint_label.setObjectName("SectionHint")
        hl.addWidget(self.hint_label)
        hl.addStretch(1)
        self._header_layout = hl
        self.header.clicked.connect(self.toggle)
        lay.addWidget(self.header)

        self.content = QWidget()
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(4, 0, 0, 2)
        self.body.setSpacing(4)
        lay.addWidget(self.content)

        if settings_key:
            self._expanded = self._load(expanded)
        self._apply()

    # -- header ---------------------------------------------------------------------------------
    def add_header_widget(self, w: QWidget) -> None:
        self._header_layout.addWidget(w)

    def set_count(self, n: int | None, severity: str = "") -> None:
        """Show "(n)" next to the title (also while collapsed). ``severity`` "critical" / "action"
        colours it; the number itself is always shown in words too, so colour is never the only signal."""
        if n is None:
            self.count_label.setVisible(False)
            return
        self.count_label.setText(f"({n:,})")
        self.count_label.setProperty("severity", severity or "none")
        self.count_label.style().unpolish(self.count_label)
        self.count_label.style().polish(self.count_label)
        self.count_label.setVisible(True)

    def set_hint(self, text: str) -> None:
        """Quiet text after the title, e.g. what is inside while the section is folded."""
        self.hint_label.setText(text)

    # -- state ----------------------------------------------------------------------------------
    @property
    def expanded(self) -> bool:
        return self._expanded

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, on: bool, save: bool = True) -> None:
        on = bool(on)
        changed = on != self._expanded
        self._expanded = on
        self._apply()
        if save:
            self._save()
        if changed:
            self.toggled.emit(on)

    def saved_state(self) -> bool | None:
        """The state saved on this PC, or None when the person never folded / unfolded it."""
        if not self.settings_key:
            return None
        try:
            v = _qs().value(f"{self.settings_key}/expanded")
        except Exception:  # noqa: BLE001 - settings are a convenience only
            return None
        if v is None:
            return None
        return str(v).lower() in ("true", "1")

    def _load(self, default: bool) -> bool:
        saved = self.saved_state()
        return default if saved is None else saved

    def _save(self) -> None:
        if not self.settings_key:
            return
        try:
            _qs().setValue(f"{self.settings_key}/expanded", "true" if self._expanded else "false")
        except Exception:  # noqa: BLE001
            log.debug("Could not save section state", exc_info=True)

    def _apply(self) -> None:
        self.content.setVisible(self._expanded)
        self.chevron.setText(CHEVRON_OPEN if self._expanded else CHEVRON_CLOSED)
        verb = "Hide" if self._expanded else "Show"
        self.chevron.setToolTip(f"{verb} {self.title}")
        self.chevron.setAccessibleName(f"{verb} {self.title}")

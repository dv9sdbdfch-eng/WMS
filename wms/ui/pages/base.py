"""Base class for the screens shown in the main window."""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from wms.ui.widgets.common import label


class Page(QWidget):
    title = ""
    subtitle = ""

    def __init__(self, app, parent=None):
        super().__init__(parent)
        self.app = app
        self.dirty = True
        outer = QVBoxLayout(self)
        outer.setContentsMargins(24, 18, 24, 18)
        outer.setSpacing(12)
        head = QHBoxLayout()
        titles = QVBoxLayout()
        titles.setSpacing(2)
        titles.addWidget(label(self.title, "PageTitle"))
        if self.subtitle:
            titles.addWidget(label(self.subtitle, "PageSubtitle"))
        head.addLayout(titles)
        head.addStretch(1)
        self.actions = QHBoxLayout()
        self.actions.setSpacing(8)
        head.addLayout(self.actions)
        outer.addLayout(head)
        self.body = QVBoxLayout()
        self.body.setSpacing(10)
        outer.addLayout(self.body, 1)

    def add_action(self, widget) -> None:
        self.actions.addWidget(widget)

    def on_show(self) -> None:
        if self.dirty:
            self.refresh()
            self.dirty = False

    def refresh(self) -> None:  # pragma: no cover - overridden
        pass


class ListPage(Page):
    """A searchable list of records with an optional date range.

    Subclasses set ``columns()`` and ``fetch()`` and may add extra filter widgets
    with ``add_filter``; double-click opens the record via ``open_item``.
    """

    search_hint = "Search..."
    date_filter = True

    def __init__(self, app, parent=None):
        super().__init__(app, parent)
        from wms.ui.widgets.common import DataTable, OptionalDateEdit, SearchBox, button, h, label
        self.search = SearchBox(self.search_hint)
        self.search.search.connect(lambda _: self.refresh())
        self.filters = h(self.search)
        if self.date_filter:
            self.date_from, self.date_to = OptionalDateEdit(), OptionalDateEdit()
            for d in (self.date_from, self.date_to):
                d.edit.editingFinished.connect(self.refresh)
            self.filters.addWidget(label("From", "Muted"))
            self.filters.addWidget(self.date_from)
            self.filters.addWidget(label("To", "Muted"))
            self.filters.addWidget(self.date_to)
        self.body.addLayout(self.filters)
        self.table = DataTable(self.columns())
        self.table.activated_item.connect(self._open)
        self.body.addWidget(self.table, 1)
        self.count = label("", "Muted")
        self.footer = h(self.count, None, button("Open", lambda: self._open(self.table.current())))
        self.body.addLayout(self.footer)

    def add_filter(self, widget, signal=None) -> None:
        self.filters.addWidget(widget)
        if signal is not None:
            signal.connect(lambda *_: self.refresh())

    def dates(self) -> tuple:
        """(from, to); an unreadable date is ignored rather than blocking the list."""
        if not self.date_filter:
            return None, None
        out = []
        for d in (self.date_from, self.date_to):
            try:
                out.append(d.value())
            except Exception:  # noqa: BLE001 - half-typed date in a filter
                out.append(None)
        return tuple(out)

    def columns(self):  # pragma: no cover - overridden
        return []

    def fetch(self) -> list:  # pragma: no cover - overridden
        return []

    def describe(self, rows: list) -> str:
        return f"{len(rows)} record(s)"

    def refresh(self) -> None:
        rows = self.fetch()
        self.table.set_items(rows)
        self.count.setText(self.describe(rows))

    def _open(self, item) -> None:
        if item is not None:
            self.open_item(item)
            self.app.changed()

    def open_item(self, item) -> None:  # pragma: no cover - overridden
        pass

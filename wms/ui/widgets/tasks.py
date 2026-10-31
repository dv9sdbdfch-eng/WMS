"""Run slow work (big lists, exports, imports) off the screen thread so the window never freezes.

Two helpers:

* ``background(fn, on_done, on_error)`` - run ``fn()`` on a worker thread; the callbacks run back on
  the screen thread. Used by the Materials table to load pages while the person keeps typing.
* ``with_progress(parent, title, fn)`` - the same, with a progress window and a Cancel button.
  ``fn(progress)`` calls ``progress(done, total)``; it returns False once Cancel was pressed, and
  the work stops at the next step.

Each worker opens its own database session (the services already do that per call), so threads
never share one. Tests set ``SYNC = True``: the work then runs straight away on the calling thread,
which keeps them simple and deterministic.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import QApplication, QProgressDialog, QWidget

log = logging.getLogger(__name__)

SYNC = False  # tests: run everything inline

_live: set = set()  # keeps workers alive until their result has been delivered


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(object)
    progress = Signal(int, object)


class _Job(QRunnable):
    def __init__(self, fn: Callable[[], Any]):
        super().__init__()
        self.fn = fn
        self.signals = _Signals()
        self.setAutoDelete(False)

    def run(self):  # worker thread
        try:
            result = self.fn()
        except BaseException as exc:  # noqa: BLE001 - delivered to the screen thread
            self.signals.failed.emit(exc)
        else:
            self.signals.done.emit(result)


def background(fn: Callable[[], Any], on_done: Callable[[Any], None],
               on_error: Callable[[BaseException], None] | None = None) -> None:
    """Run ``fn`` on a worker thread and hand its result to ``on_done`` on the screen thread."""
    if SYNC:
        try:
            result = fn()
        except Exception as exc:  # noqa: BLE001
            if on_error is None:
                raise
            on_error(exc)
            return
        on_done(result)
        return
    job = _Job(fn)
    _live.add(job)

    def finish(cb, value):
        _live.discard(job)
        if cb is not None:
            cb(value)
        elif isinstance(value, BaseException):
            log.error("Background task failed", exc_info=value)

    job.signals.done.connect(lambda r: finish(on_done, r), Qt.ConnectionType.QueuedConnection)
    job.signals.failed.connect(lambda e: finish(on_error, e), Qt.ConnectionType.QueuedConnection)
    QThreadPool.globalInstance().start(job)


class Cancelled(Exception):
    """The person pressed Cancel in a progress window."""


def with_progress(parent: QWidget | None, title: str, fn: Callable[[Callable[[int, int | None], bool]], Any],
                  text: str = "Working...") -> Any:
    """Run ``fn(progress)`` with a progress window. Returns its result; raises what it raised
    (``Cancelled`` if the person cancelled). The rest of the program stays responsive."""
    state = {"cancel": False}

    if SYNC:
        return fn(lambda done, total: not state["cancel"])

    dlg = QProgressDialog(text, "Cancel", 0, 0, parent)
    dlg.setWindowTitle(title)
    dlg.setWindowModality(Qt.WindowModality.WindowModal)
    dlg.setMinimumDuration(400)
    dlg.setAutoClose(False)
    dlg.setAutoReset(False)
    dlg.canceled.connect(lambda: state.__setitem__("cancel", True))
    sig = _Signals()

    def show(done: int, total):
        if total:
            dlg.setMaximum(int(total))
            dlg.setValue(min(int(done), int(total)))
            dlg.setLabelText(f"{text}  {done:,} of {int(total):,}")
        else:
            dlg.setLabelText(f"{text}  {done:,}")

    sig.progress.connect(show, Qt.ConnectionType.QueuedConnection)

    def progress(done: int, total) -> bool:  # worker thread
        sig.progress.emit(int(done), total)
        return not state["cancel"]

    out: dict = {}
    job = _Job(lambda: fn(progress))
    job.signals.done.connect(lambda r: out.setdefault("result", r), Qt.ConnectionType.QueuedConnection)
    job.signals.failed.connect(lambda e: out.setdefault("error", e), Qt.ConnectionType.QueuedConnection)
    QThreadPool.globalInstance().start(job)
    while "result" not in out and "error" not in out:
        QApplication.processEvents()
        QThreadPool.globalInstance().waitForDone(30)
    QApplication.processEvents()
    dlg.close()
    dlg.deleteLater()
    if "error" in out:
        raise out["error"]
    return out["result"]


__all__ = ["SYNC", "Cancelled", "background", "with_progress"]

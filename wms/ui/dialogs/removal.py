"""'Delete' and 'Restore' for bins, products, projects and suppliers.

The user only ever presses Delete. The service works out the safe outcome (delete a never-used
record, or remove a used one from active use while keeping its history) and this dialog shows
exactly that before anything changes - including why removal is not possible yet.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QDialog, QPlainTextEdit, QVBoxLayout

from wms.ui.widgets.common import button, h, info, label, run


class RemovalDialog(QDialog):
    def __init__(self, plan, parent=None, title: str | None = None):
        super().__init__(parent)
        self.setWindowTitle(title or f"Delete {plan.kind.lower()}")
        self.setMinimumWidth(520)
        lay = QVBoxLayout(self)
        lay.addWidget(label(plan.message, wrap=True))
        lay.addWidget(label("Reason (optional, recorded in the audit trail)", "Muted"))
        self.reason = QPlainTextEdit()
        self.reason.setFixedHeight(60)
        lay.addWidget(self.reason)
        lay.addLayout(h(None, button("Cancel", self.reject), button(plan.action, self.accept, danger=True)))

    def reason_text(self) -> str:
        return self.reason.toPlainText().strip()


def delete_record(parent, plan_fn: Callable, remove_fn: Callable, record_id: int) -> bool:
    """Ask, then remove. Returns True when something changed."""
    plan = run(parent, plan_fn, record_id)
    if plan is None:
        return False
    if not plan.allowed:
        info(parent, f"Delete {plan.kind.lower()}", plan.message)
        return False
    dlg = RemovalDialog(plan, parent)
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return False
    done = run(parent, remove_fn, record_id, dlg.reason_text() or None)
    if done is None:
        return False
    if done.permanent:
        info(parent, "Deleted", f"{done.kind} {done.name} was deleted permanently.")
    else:
        info(parent, "Removed from active use",
             f"{done.kind} {done.name} has been removed from active use. Its history is kept and can be "
             f"found with Search; it can be restored at any time.")
    return True


def restore_record(parent, kind: str, name: str, restore_fn: Callable, record_id: int) -> bool:
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"Restore {kind.lower()}")
    dlg.setMinimumWidth(480)
    lay = QVBoxLayout(dlg)
    lay.addWidget(label(f"Restore {kind} {name} to active use? It will be available for new transactions "
                        f"again. Its past transactions are not changed.", wrap=True))
    lay.addWidget(label("Reason (optional, recorded in the audit trail)", "Muted"))
    reason = QPlainTextEdit()
    reason.setFixedHeight(60)
    lay.addWidget(reason)
    lay.addLayout(h(None, button("Cancel", dlg.reject), button("Restore", dlg.accept, primary=True)))
    if dlg.exec() != QDialog.DialogCode.Accepted:
        return False
    return run(parent, restore_fn, record_id, reason.toPlainText().strip() or None) is not None

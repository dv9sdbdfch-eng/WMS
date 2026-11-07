"""Click every button / menu item / combo / table action on one WMS screen (offscreen) and record
every exception, critical message, error log and warning.

    python -m tools.ui_audit_page <page_key> <out.json>   (normally started by tools.ui_audit)
"""
# ruff: noqa: E402 - Qt settings and the data folder must be set before the app modules load
import faulthandler
import json
import logging
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
TMP = Path(tempfile.mkdtemp())
os.environ["WMS_DATA_DIR"] = str(TMP / "data")
faulthandler.dump_traceback_later(400, exit=True)  # a hang becomes a reported failure
KEY = sys.argv[1]
OUT = Path(sys.argv[2])

from PySide6.QtCore import QSettings, Qt  # noqa: E402

QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, str(TMP / "qs"))
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractButton,
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QInputDialog,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTabWidget,
    QToolButton,
    QTreeWidget,
)

from tools import ui_audit_demo as demo_data  # noqa: E402
from wms.ui.widgets import tasks  # noqa: E402

tasks.SYNC = True
ctx, svc, P, sites, sup = demo_data.build()
qa = QApplication.instance() or QApplication(sys.argv)
from wms.ui import theme  # noqa: E402

theme.apply_theme(qa)

R = {"page": KEY, "errors": [], "warnings": [], "infos": [], "dialogs": [], "clicked": [], "menus": [],
     "exports": [], "skipped": [], "timing": {}}
CTX = ["start"]


def where():
    return " > ".join(CTX)


def err(kind, detail):
    R["errors"].append({"where": where(), "kind": kind, "detail": str(detail)[-3000:]})


def hook(t, v, tb):
    err("EXCEPTION", "".join(traceback.format_exception(t, v, tb)))


sys.excepthook = hook


class H(logging.Handler):
    def emit(self, rec):
        if rec.levelno >= logging.ERROR:
            msg = rec.getMessage()
            if rec.exc_info:
                msg += "\n" + "".join(traceback.format_exception(*rec.exc_info))
            err("LOG", msg)


logging.getLogger().addHandler(H())


def pump(n=6):
    for _ in range(n):
        qa.processEvents()


def mb(kind):
    def fn(parent, title, text, *a, **k):
        (R["warnings"] if kind == "warning" else R["infos"]).append({"where": where(), "title": title,
                                                                    "text": str(text)[:400]})
        if kind == "critical":
            err("CRITICAL-BOX", f"{title}: {text}")
        if kind == "question":
            return QMessageBox.StandardButton.Yes
        return QMessageBox.StandardButton.Ok
    return fn


for k in ("warning", "critical", "information", "question"):
    setattr(QMessageBox, k, staticmethod(mb(k)))


def box_exec(box):
    R["infos"].append({"where": where(), "title": box.windowTitle(), "text": box.text()[:300], "confirm": True})
    for b in box.buttons():
        if box.buttonRole(b) in (QMessageBox.ButtonRole.AcceptRole, QMessageBox.ButtonRole.YesRole):
            b.click()
            return box.standardButton(b) if box.standardButton(b) != QMessageBox.StandardButton.NoButton else 0
    return QMessageBox.StandardButton.Ok


n_files = [0]


def save_name(parent=None, caption="", directory="", filt="", *a, **k):
    n_files[0] += 1
    ext = ".xlsx"
    f = filt or (a[0] if a else "")
    for e in (".xlsx", ".pdf", ".csv", ".zip", ".db", ".png", ".txt"):
        if "*" + e in str(f):
            ext = e
            break
    p = TMP / "out" / f"file{n_files[0]}{ext}"
    p.parent.mkdir(exist_ok=True)
    R["exports"].append({"where": where(), "path": str(p)})
    return str(p), str(f)


QFileDialog.getSaveFileName = staticmethod(save_name)
QFileDialog.getOpenFileName = staticmethod(lambda *a, **k: ("", ""))
QFileDialog.getOpenFileNames = staticmethod(lambda *a, **k: ([], ""))
QFileDialog.getExistingDirectory = staticmethod(lambda *a, **k: str(TMP / "dir"))
QInputDialog.getText = staticmethod(lambda *a, **k: ("Audit test", True))
QInputDialog.getMultiLineText = staticmethod(lambda *a, **k: ("Audit test reason", True))
QInputDialog.getItem = staticmethod(lambda *a, **k: ((a[3][0] if len(a) > 3 and a[3] else ""), True))
QInputDialog.getInt = staticmethod(lambda *a, **k: (1, True))
QInputDialog.getDouble = staticmethod(lambda *a, **k: (1.0, True))

SKIP_TEXT = ("cancel", "close", "log out", "restore selected", "restore from file", "exit", "quit")
MAX_DEPTH = 2
DEEP_SKIP = ("mark completed", "delete", "remove", "cancel count", "archive", "reverse")
DEPTH = [0]


def btn_text(b):
    t = (b.text() or b.toolTip() or b.objectName() or type(b).__name__).replace("&", "").strip()
    return t[:60]


def select_first(widget):
    for v in widget.findChildren(QAbstractItemView):
        try:
            if not v.isVisible() or v.model() is None or v.model().rowCount() == 0:
                continue
            if isinstance(v, QTreeWidget):
                v.setCurrentItem(v.topLevelItem(0))
            else:
                idx = v.model().index(0, 0)
                v.setCurrentIndex(idx)
                if v.selectionModel():
                    v.selectRow(0) if hasattr(v, "selectRow") else None
        except Exception as e:  # noqa: BLE001
            err("HARNESS", f"select_first: {e}")


MENU_ONLY = [None]
MENU_N = [0]


def handle_menu(menu, name="menu"):
    if menu is None or DEPTH[0] > MAX_DEPTH + 1:
        return
    acts = [a for a in menu.actions() if not a.isSeparator() and a.text()]
    only = MENU_ONLY[0]
    MENU_ONLY[0] = None  # applies to the top menu only
    if only == -1:
        MENU_N[0] = len(acts)
        R["menus"].append({"where": where(), "items": [a.text() for a in acts]})
        return
    if only is not None:
        acts = acts[only:only + 1]
    else:
        R["menus"].append({"where": where(), "items": [a.text() for a in acts]})
    for a in acts:
        if a.menu() is not None:
            CTX.append(f"[{a.text()}]")
            handle_menu(a.menu())
            CTX.pop()
            continue
        if DEPTH[0] >= 2 and any(a.text().lower().startswith(s) for s in DEEP_SKIP):
            R["skipped"].append(f"{where()} > menu {a.text()} (deep destructive)")
            continue
        if not a.isEnabled() or any(s in a.text().lower() for s in SKIP_TEXT):
            R["skipped"].append(f"{where()} > menu {a.text()} (enabled={a.isEnabled()})")
            continue
        CTX.append(f"menu:{a.text()}")
        try:
            a.trigger()
            pump()
        except Exception:  # noqa: BLE001
            err("EXCEPTION", traceback.format_exc())
        CTX.pop()
        restore()


class FakeMenu(QMenu):
    def exec(self, *a, **k):
        handle_menu(self)
        return None

    def popup(self, *a, **k):
        handle_menu(self)


class FakeBox(QMessageBox):
    def exec(self):
        return box_exec(self)


def swap_names():
    import importlib
    import pkgutil

    import wms
    for m in pkgutil.walk_packages(wms.__path__, "wms."):
        if m.name.endswith("__main__") or m.name in ("wms.main",):
            continue
        try:
            mod = importlib.import_module(m.name)
        except Exception:  # noqa: BLE001
            continue
        if getattr(mod, "QMenu", None) is QMenu:
            mod.QMenu = FakeMenu
        if getattr(mod, "QMessageBox", None) is QMessageBox:
            mod.QMessageBox = FakeBox


swap_names()


RESTORE = [None]


N_RESTORE = [0]


def dump():
    R["partial"] = True
    OUT.write_text(json.dumps(R, indent=1, default=str))


def restore():
    N_RESTORE[0] += 1
    if N_RESTORE[0] % 15 == 0:
        dump()
    if DEPTH[0] == 0 and RESTORE[0] is not None:
        try:
            RESTORE[0]()
        except Exception:  # noqa: BLE001
            err("HARNESS", traceback.format_exc())


def visible_buttons(widget):
    out, seen = [], {}
    for b in widget.findChildren(QAbstractButton):
        try:
            if not b.isVisible() or b.window() is not widget.window() or isinstance(b, QCheckBox):
                continue
            t = btn_text(b)
        except RuntimeError:
            continue
        n = seen.get(t, 0)
        seen[t] = n + 1
        out.append(((t, n), b))
    return out


def click_all(widget, label):
    """Click each visible enabled button of ``widget`` once (first row of tables selected), going
    back to the screen after each click."""
    done = set()
    for _guard in range(150):
        restore()
        try:
            select_first(widget)
            pump(2)
            cands = [(k, b) for k, b in visible_buttons(widget) if k not in done]
        except RuntimeError:
            return
        if not cands:
            return
        key, b = cands[0]
        done.add(key)
        t = key[0]
        try:
            if not b.isEnabled():
                R["skipped"].append(f"{where()} > {t} (disabled)")
                continue
        except RuntimeError:
            continue
        if DEPTH[0] >= 2 and any(t.lower().startswith(s) for s in DEEP_SKIP):
            R["skipped"].append(f"{where()} > {t} (deep destructive, tested at its own screen)")
            continue
        if any(s == t.lower() or t.lower().startswith(s) for s in SKIP_TEXT):
            R["skipped"].append(f"{where()} > {t} (skip list)")
            continue
        CTX.append(f"btn:{t}")
        R["clicked"].append(where())
        t0 = time.perf_counter()
        try:
            m = b.menu() if isinstance(b, (QToolButton, QPushButton)) else None
            if m is not None:
                handle_menu(m)
            else:
                b.click()
            pump()
        except RuntimeError:
            pass
        except Exception:  # noqa: BLE001
            err("EXCEPTION", traceback.format_exc())
        dt = time.perf_counter() - t0
        if dt > 2:
            R["timing"][where()] = round(dt, 2)
        CTX.pop()


def views_actions(widget):
    """Double-click and right-click the first row of every table / tree."""
    for v in list(widget.findChildren(QAbstractItemView)):
        try:
            if not v.isVisible() or v.model() is None or v.model().rowCount() == 0:
                continue
            name = v.objectName() or type(v).__name__
            idx = v.model().index(0, 0)
            CTX.append(f"view:{name}:double-click")
            try:
                v.setCurrentIndex(idx)
                v.doubleClicked.emit(idx)
                v.activated.emit(idx)
                pump()
            except Exception:  # noqa: BLE001
                err("EXCEPTION", traceback.format_exc())
            CTX.pop()
            restore()
            if not v.isVisible() or v.model() is None or v.model().rowCount() == 0:
                continue
            idx = v.model().index(0, 0)
            if v.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu:
                CTX.append(f"view:{name}:right-click")
                try:
                    if DEPTH[0] == 0:
                        MENU_ONLY[0] = -1  # collect the item list
                        v.customContextMenuRequested.emit(v.visualRect(idx).center())
                        n_items = MENU_N[0]
                        for i in range(n_items):
                            restore()
                            if not v.isVisible() or v.model() is None or v.model().rowCount() == 0:
                                break
                            idx = v.model().index(0, 0)
                            v.setCurrentIndex(idx)
                            MENU_ONLY[0] = i
                            v.customContextMenuRequested.emit(v.visualRect(idx).center())
                            pump()
                        MENU_ONLY[0] = None
                    else:
                        v.customContextMenuRequested.emit(v.visualRect(idx).center())
                        pump()
                except Exception:  # noqa: BLE001
                    err("EXCEPTION", traceback.format_exc())
                MENU_ONLY[0] = None
                CTX.pop()
        except RuntimeError:
            continue


def combos(widget):
    for c in list(widget.findChildren(QComboBox)):
        try:
            if not c.isVisible() or not c.isEnabled() or c.isEditable() and c.count() == 0:
                continue
            name = c.objectName() or c.currentText() or "combo"
            orig = c.currentIndex()
            for i in range(min(c.count(), 8 if DEPTH[0] == 0 else 3)):
                CTX.append(f"combo:{name}={c.itemText(i)}")
                try:
                    c.setCurrentIndex(i)
                    pump(2)
                except Exception:  # noqa: BLE001
                    err("EXCEPTION", traceback.format_exc())
                CTX.pop()
            c.setCurrentIndex(orig)
        except RuntimeError:
            continue


def searches(widget):
    for e in list(widget.findChildren(QLineEdit)):
        try:
            if not e.isVisible() or e.isReadOnly() or e.objectName() == "GlobalSearch":
                continue
            if type(e).__name__ not in ("SearchBox",) and "search" not in (e.placeholderText() or "").lower():
                continue
            for text in ("H33", "zzz-no-match", "' OR 1=1 --", ""):
                CTX.append(f"search:{text!r}")
                try:
                    e.setText(text)
                    e.returnPressed.emit()
                    if hasattr(e, "search"):
                        try:
                            e.search.emit(text)
                        except Exception:  # noqa: BLE001
                            pass
                    pump(3)
                except Exception:  # noqa: BLE001
                    err("EXCEPTION", traceback.format_exc())
                CTX.pop()
        except RuntimeError:
            continue


TABS_REF = [None]


def tabs_walk(widget, fn):
    tws = [t for t in widget.findChildren(QTabWidget) if t.isVisible()]
    if not tws:
        fn()
        return
    tw = tws[0]
    for i in range(tw.count()):
        CTX.append(f"tab:{tw.tabText(i)}")
        tw.setCurrentIndex(i)
        pump()
        TABS_REF[0] = (tw, i)
        fn()
        CTX.pop()


_orig_done = QDialog.done
_orig_accept = QDialog.accept
_orig_reject = QDialog.reject


def _held(self, r, orig):
    if getattr(self, "_audit_hold", False):
        self._audit_result = r
        return None
    return orig(self) if orig is not _orig_done else _orig_done(self, r)


QDialog.accept = lambda self: _held(self, 1, _orig_accept)
QDialog.reject = lambda self: _held(self, 0, _orig_reject)
QDialog.done = lambda self, r: _held(self, r, _orig_done)
EXEC_COUNT = {}


def dialog_exec(dlg):
    name = type(dlg).__name__
    n = EXEC_COUNT.get(id(dlg), 0) + 1
    EXEC_COUNT[id(dlg)] = n
    if n > 2:  # the same dialog shown again and again (e.g. "fill in the reason" loop): cancel
        return 0
    R["dialogs"].append({"where": where(), "dialog": name, "title": dlg.windowTitle()})
    depth_ctx = len(CTX)
    CTX.append(f"dialog:{name}")
    dlg._audit_result = None
    try:
        dlg.show()
        pump()
        if DEPTH[0] < MAX_DEPTH:
            DEPTH[0] += 1
            dlg._audit_hold = True
            try:
                if DEPTH[0] <= 1:
                    combos(dlg)
                views_actions(dlg)
                click_all(dlg, name)
            except Exception:  # noqa: BLE001
                err("EXCEPTION", traceback.format_exc())
            finally:
                DEPTH[0] -= 1
                try:
                    dlg._audit_hold = False
                except RuntimeError:
                    pass
        try:
            dlg.close()
        except RuntimeError:
            pass
    finally:
        del CTX[depth_ctx:]
    return 1 if getattr(dlg, "_audit_result", None) == 1 else 0


QDialog.exec = dialog_exec
QDialog.open = lambda self: dialog_exec(self)

from wms.ui.app import App  # noqa: E402
from wms.ui.main_window import MainWindow  # noqa: E402

app = App(ctx)
win = MainWindow(app)
win.resize(1366, 768)
win.show()
pump()

t0 = time.perf_counter()
if KEY == "_main":
    CTX[:] = ["main window"]
    for k in ("goods_in", "goods_out", "transfer"):
        CTX.append(f"quick:{k}")
        win.quick_action(k)
        pump()
        CTX.pop()
    CTX.append("user menu")
    handle_menu(win.user_menu)
    CTX.pop()
    for text in ("H3311", "P-1042", "DN-1041", "nothing-here", "A1B01"):
        CTX.append(f"global search {text}")
        win.search.setText(text)
        win.global_search()
        pump()
        CTX.pop()
    for k in ("transfer", "import_export", "archive", "backup"):
        CTX.append(f"sidebar action {k}")
        win.run_action(k)
        pump()
        CTX.pop()
    for s in list(win.sections):
        win.toggle_section(s)
        win.toggle_section(s)
    win.refresh_current()
else:
    CTX[:] = [f"page {KEY}"]
    win.go(KEY)
    pump()
    page = win.page(KEY)
    TABS = [None]

    def _restore():
        for w in qa.topLevelWidgets():
            if isinstance(w, QDialog) and w.isVisible():
                w.close()
        if win.stack.currentWidget() is not page:
            win.go(KEY)
            pump()
        if TABS_REF[0] is not None and TABS_REF[0][0].currentIndex() != TABS_REF[0][1]:
            TABS_REF[0][0].setCurrentIndex(TABS_REF[0][1])
            pump()

    RESTORE[0] = _restore
    if page is None:
        err("OPEN", "page did not open")
    else:
        R["timing"]["open"] = round(time.perf_counter() - t0, 2)

        def body():
            searches(page)
            combos(page)
            views_actions(page)
            click_all(page, KEY)

        if KEY == "projects":
            from PySide6.QtWidgets import QTreeWidgetItemIterator

            from wms.ui.pages.projects import KEY as KROLE

            def all_keys():
                page.expand_all()
                pump()
                out = {}
                it = QTreeWidgetItemIterator(page.tree)
                while it.value():
                    k = it.value().data(0, KROLE)
                    if k:
                        out.setdefault(k.split(":")[0], k)
                    it += 1
                return out

            kinds = all_keys()
            R["tree_kinds"] = kinds
            base_restore = RESTORE[0]
            order = ["L", "U", "V", "B", "P", "N", "M"]
            for _kind, nkey in sorted(kinds.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99):
                CTX[:] = ["page projects", f"tree node {nkey}"]

                def node_restore(nkey=nkey):
                    base_restore()
                    if page.current_key() != nkey:
                        page.expand_all()
                        page.select(nkey)
                        pump()

                RESTORE[0] = node_restore
                node_restore()
                if page.current_key() != nkey:
                    R["skipped"].append(f"tree node {nkey} gone")
                    continue
                try:
                    MENU_ONLY[0] = -1
                    handle_menu(page.build_menu(page.tree.currentItem()))
                    for i in range(MENU_N[0]):
                        node_restore()
                        if page.current_key() != nkey:
                            break
                        MENU_ONLY[0] = i
                        handle_menu(page.build_menu(page.tree.currentItem()))
                    MENU_ONLY[0] = None
                    node_restore()
                    if page.current_key() == nkey:
                        click_all(page, KEY)
                except Exception:  # noqa: BLE001
                    err("EXCEPTION", traceback.format_exc())
            RESTORE[0] = base_restore
        CTX[:] = [f"page {KEY}"]
        tabs_walk(page, body)
        CTX[:] = [f"page {KEY}", "refresh"]
        win.go(KEY)
        page.dirty = True
        page.on_show()
        pump()
R["timing"]["total"] = round(time.perf_counter() - t0, 1)
R["partial"] = False
R["integrity"] = None
try:
    rep = svc.integrity.check() if hasattr(svc, "integrity") else None
    R["integrity"] = str(rep)[:1500]
except Exception as e:  # noqa: BLE001
    R["integrity"] = f"check failed: {e}"
win.hide()
OUT.write_text(json.dumps(R, indent=1, default=str))
os._exit(0)

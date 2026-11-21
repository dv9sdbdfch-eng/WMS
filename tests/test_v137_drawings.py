"""v1.37 Drawings on projects and units: PDF / DWG / DXF / pictures with revision and status."""

from __future__ import annotations

import pytest

from wms.core.enums import DocStatus, EntityType
from wms.core.exceptions import ValidationError

SITE, UNIT = EntityType.SITE.value, EntityType.PROJECT_UNIT.value


def _pdf(path, text="drawing"):
    path.write_bytes(b"%PDF-1.4\n% " + text.encode() + b"\n%%EOF\n")
    return path


def _dwg(path, body=b"x" * 200):
    path.write_bytes(b"AC1032" + body)
    return path


def _dxf(path):
    path.write_text("  0\nSECTION\n  2\nHEADER\n  0\nENDSEC\n  0\nEOF\n")
    return path


@pytest.fixture
def tree(svc):
    t = svc.project_tree
    p = t.save_project(None, {"project_number": "TRI", "name": "Tribeca", "status": "ACTIVE"})
    u = t.save_unit(None, {"name": "Kitchen", "unit_code": "K01", "quantity": "12"}, site_id=p.id)
    return type("T", (), dict(project=p, unit=u))


def test_dwg_dxf_and_pdf_are_accepted_and_counted(svc, tree, tmp_path):
    att = svc.attachments
    files = [_pdf(tmp_path / "K01 layout.pdf"), _dwg(tmp_path / "K01 carcass.dwg"), _dxf(tmp_path / "K01.dxf")]
    added = att.add_drawings(UNIT, tree.unit.id, files, revision="C", status=DocStatus.APPROVED.value)
    assert [a.mime_type for a in added] == ["application/pdf", "application/acad", "application/dxf"]
    assert all(a.revision == "C" and a.doc_status == "APPROVED" and a.kind == "DRAWING" for a in added)
    assert not any(a.is_image for a in added)
    assert att.drawing_counts(UNIT) == {tree.unit.id: 3}
    assert att.drawing_counts(SITE) == {}
    att.add_drawings(SITE, tree.project.id, [_pdf(tmp_path / "finishes.pdf", "f")], status="PRELIMINARY")
    assert att.drawing_counts(SITE, [tree.project.id]) == {tree.project.id: 1}
    assert [a.original_name for a in att.drawings_for(UNIT, tree.unit.id)] == ["K01 carcass.dwg", "K01 layout.pdf",
                                                                              "K01.dxf"]


def test_a_renamed_file_is_refused_and_nothing_is_kept(svc, tree, tmp_path):
    fake = tmp_path / "carcass.dwg"
    fake.write_bytes(b"PK\x03\x04 not a drawing")
    good = _pdf(tmp_path / "ok.pdf")
    with pytest.raises(ValidationError, match="not a valid DWG"):
        svc.attachments.add_drawings(UNIT, tree.unit.id, [good, fake])
    assert svc.attachments.drawings_for(UNIT, tree.unit.id) == []      # all or none
    stored = [p for p in svc.ctx.config.attachments_path.rglob("*") if p.is_file()]
    assert stored == []
    bad_text = tmp_path / "x.dxf"
    bad_text.write_text("hello")
    with pytest.raises(ValidationError):
        svc.attachments.add_drawings(UNIT, tree.unit.id, [bad_text])
    xls = tmp_path / "list.xlsx"
    xls.write_bytes(b"PK\x03\x04")
    with pytest.raises(ValidationError):
        svc.attachments.add_drawings(UNIT, tree.unit.id, [xls])
    with pytest.raises(ValidationError, match="Preliminary or Approved"):
        svc.attachments.add_drawings(UNIT, tree.unit.id, [good], status="SUPERSEDED")


def test_new_revision_keeps_the_old_one_as_superseded(svc, tree, tmp_path):
    att = svc.attachments
    [b] = att.add_drawings(UNIT, tree.unit.id, [_pdf(tmp_path / "K01.pdf", "rev B")], revision="B")
    c = att.new_revision(b.id, _pdf(tmp_path / "K01 rev C.pdf", "rev C"), revision="C")
    rows = att.drawings_for(UNIT, tree.unit.id)
    assert [(a.revision, a.doc_status) for a in rows] == [("C", "APPROVED"), ("B", "SUPERSEDED")]
    assert rows[1].superseded_by_id == c.id
    assert att.drawing_counts(UNIT) == {tree.unit.id: 1}                # only the current one counts
    with pytest.raises(ValidationError, match="already superseded"):
        att.new_revision(b.id, _pdf(tmp_path / "again.pdf", "d"), revision="D")
    with pytest.raises(ValidationError, match="cannot change status"):
        att.set_doc_status(b.id, "APPROVED")
    att.set_doc_status(c.id, "PRELIMINARY")
    assert att.drawings_for(UNIT, tree.unit.id)[0].doc_status == "PRELIMINARY"
    actions = [r.action for r in svc.audit.search(entity_type=UNIT)]
    assert "DRAWING_SUPERSEDED" in actions and "DRAWING_STATUS" in actions


def test_size_limit(svc, tree, tmp_path):
    svc.ctx.config.max_attachment_mb = 1
    big = _dwg(tmp_path / "big.dwg", b"x" * (1024 * 1024 + 10))
    with pytest.raises(ValidationError, match="larger than 1 MB"):
        svc.attachments.add_drawings(UNIT, tree.unit.id, [big])


def test_status_change_alone_and_viewer_cannot_add(svc, tree, tmp_path):
    from wms.core.exceptions import PermissionDeniedError
    att = svc.attachments
    [a] = att.add_drawings(UNIT, tree.unit.id, [_pdf(tmp_path / "K01.pdf")], status="PRELIMINARY")
    att.set_doc_status(a.id, "APPROVED")
    att.set_doc_status(a.id, "APPROVED")                                 # no change, no second audit row
    assert att.drawings_for(UNIT, tree.unit.id)[0].doc_status == "APPROVED"
    assert [r.action for r in svc.audit.search(entity_type=UNIT)].count("DRAWING_STATUS") == 1
    roles = {r.code: r for r in svc.users.list_roles()}
    svc.users.create_user({"username": "view", "full_name": "View", "role_id": roles["VIEWER"].id,
                           "must_change_password": False}, "secret1")
    svc.auth.login("view", "secret1")
    with pytest.raises(PermissionDeniedError):
        att.add_drawings(UNIT, tree.unit.id, [_pdf(tmp_path / "x.pdf", "x")])
    with pytest.raises(PermissionDeniedError):
        att.new_revision(a.id, _pdf(tmp_path / "y.pdf", "y"))
    with pytest.raises(PermissionDeniedError):
        att.set_doc_status(a.id, "PRELIMINARY")


def test_removing_the_current_revision_leaves_the_old_one_superseded(svc, tree, tmp_path):
    att = svc.attachments
    [b] = att.add_drawings(UNIT, tree.unit.id, [_pdf(tmp_path / "K01.pdf", "b")], revision="B")
    c = att.new_revision(b.id, _pdf(tmp_path / "K01c.pdf", "c"), revision="C")
    att.remove(c.id)
    rows = att.drawings_for(UNIT, tree.unit.id)
    assert [(a.revision, a.doc_status) for a in rows] == [("B", "SUPERSEDED")]
    assert att.drawing_counts(UNIT) == {}                                 # nothing current: the tab warns


def test_a_failed_revision_leaves_no_file_or_row(svc, tree, tmp_path):
    att = svc.attachments
    [b] = att.add_drawings(UNIT, tree.unit.id, [_pdf(tmp_path / "K01.pdf", "b")], revision="B")
    att.new_revision(b.id, _pdf(tmp_path / "K01c.pdf", "c"), revision="C")
    before = sorted(p for p in svc.ctx.config.attachments_path.rglob("*") if p.is_file())
    with pytest.raises(ValidationError, match="already superseded"):
        att.new_revision(b.id, _pdf(tmp_path / "K01d.pdf", "d"), revision="D")
    assert sorted(p for p in svc.ctx.config.attachments_path.rglob("*") if p.is_file()) == before
    assert [a.revision for a in att.drawings_for(UNIT, tree.unit.id)] == ["C", "B"]


# ---- the screen -------------------------------------------------------------------------------------
from test_ui_smoke import _errors, ui  # noqa: E402,F401 - the shared screen fixture


def _projects(win):
    from PySide6.QtWidgets import QApplication
    win.go("projects")
    QApplication.processEvents()
    page = win.page("projects")
    page.refresh()
    return page


def _select(page, key):
    page.expand_all()
    item = page.items[key]
    page.tree.setCurrentItem(item)
    return item


def test_drawings_tab_on_unit_and_clip_count_in_tree(ui, tree, tmp_path, monkeypatch):  # noqa: F811
    from PySide6.QtWidgets import QDialog, QFileDialog

    from wms.ui.widgets import drawings as dw
    _app, win, msgs, _ = ui
    svc = win.app.services
    svc.attachments.add_drawings(SITE, tree.project.id, [_pdf(tmp_path / "finishes.pdf", "p")])
    page = _projects(win)
    _select(page, f"U:{tree.unit.id}")
    panel = page.panel
    assert panel.currentWidget() is panel.u_tabs and panel.u_tabs.tabText(1) == "Drawings"
    # the project's drawings show under "From project", read only
    lst = panel.u_drawings.list
    assert lst.topLevelItem(0).text(0).startswith("From project: Tribeca")
    # add two files with revision B
    files = [str(_pdf(tmp_path / "K01.pdf")), str(_dwg(tmp_path / "K01.dwg"))]
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: (files, ""))

    def accept(dlg):
        dlg.revision.setText("B")
        return QDialog.DialogCode.Accepted
    monkeypatch.setattr(dw.DrawingDialog, "exec", accept)
    panel.u_drawings.add()
    assert panel.u_tabs.tabText(1) == "Drawings (2)"
    assert any("📎 2" in page.items[f"U:{tree.unit.id}"].text(c) for c in range(page.tree.columnCount()))
    # a DWG has no preview; opening it opens a read-only copy in the PC's own program
    opened = []
    monkeypatch.setattr(dw, "open_read_only", lambda path, name: opened.append(name))
    dwg_row = next(lst.topLevelItem(i) for i in range(lst.topLevelItemCount())
                   if lst.topLevelItem(i).text(0) == "K01.dwg")
    lst.setCurrentItem(dwg_row)
    assert "No preview" in panel.u_drawings.preview.empty.text()
    panel.u_drawings.open_current()
    assert opened == ["K01.dwg"]
    assert not _errors(msgs)


def test_project_drawings_tab(ui, tree, tmp_path):  # noqa: F811
    _app, win, msgs, _ = ui
    win.app.services.attachments.add_drawings(SITE, tree.project.id, [_pdf(tmp_path / "plan.pdf")], revision="A")
    page = _projects(win)
    _select(page, f"P:{tree.project.id}")
    panel = page.panel
    assert panel.currentWidget() is panel.p_tabs and panel.p_tabs.tabText(1) == "Drawings (1)"
    row = panel.p_drawings.list.topLevelItem(0)
    assert (row.text(0), row.text(1), row.text(2)) == ("plan.pdf", "A", "Approved")
    assert any("📎 1" in page.items[f"P:{tree.project.id}"].text(c) for c in range(page.tree.columnCount()))
    assert not _errors(msgs)


def test_read_only_copy(tmp_path, qapp, monkeypatch):
    import stat

    from PySide6.QtGui import QDesktopServices

    from wms.ui.widgets.drawings import open_read_only
    src = _dwg(tmp_path / "stored.dwg")
    urls = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: urls.append(url))
    copy = open_read_only(src, "K01: carcass.dwg")
    assert copy.name == "K01 carcass.dwg" and copy.read_bytes() == src.read_bytes()
    assert not copy.stat().st_mode & stat.S_IWUSR and urls          # read-only (root can still write)


def test_old_view_copies_are_cleaned(tmp_path, qapp, monkeypatch):
    import os
    import stat
    import tempfile

    from PySide6.QtGui import QDesktopServices

    from wms.ui.widgets.drawings import open_read_only
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: None)
    old = open_read_only(_dwg(tmp_path / "a.dwg"), "old.dwg")
    os.utime(old.parent, (0, 0))
    new = open_read_only(_dwg(tmp_path / "b.dwg"), "new.dwg")
    assert not old.parent.exists() and new.is_file() and not new.stat().st_mode & stat.S_IWUSR

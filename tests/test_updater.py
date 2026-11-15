"""v1.33.0: automatic program updates (GitHub Releases) - checking, safe download, installer set-up."""

from __future__ import annotations

import hashlib
import io
import json
import re
from pathlib import Path

import pytest

import wms
from wms.services import updater

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD = b"MZ fake installer " * 1000
SHA = hashlib.sha256(PAYLOAD).hexdigest()


class _Resp(io.BytesIO):
    def __init__(self, data: bytes, headers: dict | None = None):
        super().__init__(data)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _release(version="9.1.0", digest=True, sidecar=False, tag=None, **extra):
    asset = {"name": f"StoreWMS-Setup-{version}.exe", "size": len(PAYLOAD),
             "browser_download_url": f"https://github.com/x/releases/download/v{version}/StoreWMS-Setup-{version}.exe"}
    if digest:
        asset["digest"] = f"sha256:{SHA}"
    assets = [asset]
    if sidecar:
        assets.append({"name": asset["name"] + ".sha256", "browser_download_url": "https://github.com/x/sha"})
    data = {"tag_name": tag or f"v{version}", "body": "- New stuff", "published_at": "2026-11-15T10:00:00Z",
            "html_url": "https://github.com/x/releases/tag/v" + version, "assets": assets}
    data.update(extra)
    return data


@pytest.fixture
def fake_net(monkeypatch):
    """Serve URLs from a dict instead of the internet."""
    routes: dict[str, object] = {}

    def fake_request(url, timeout, accept="application/vnd.github+json"):
        body = routes.get(url)
        if body is None:
            raise updater.urllib.error.URLError("no route")
        if isinstance(body, Exception):
            raise body
        if isinstance(body, dict):
            body = json.dumps(body).encode()
        return _Resp(body, {"Content-Length": str(len(body))})

    monkeypatch.setattr(updater, "_request", fake_request)
    return routes


def test_version_compare():
    assert updater.parse_version("v1.33.0") == (1, 33)
    assert updater.is_newer("1.33.1", "1.33.0")
    assert updater.is_newer("1.40", "1.33.9")
    assert not updater.is_newer("1.33.0", "1.33")
    assert not updater.is_newer("1.9.0", "1.33.0")  # numbers, not text
    assert not updater.is_newer("garbage", "1.0.0")


def test_check_offers_only_a_newer_verified_release(fake_net):
    fake_net[updater.RELEASES_API] = _release("9.1.0")
    info = updater.check(current="1.33.0")
    assert info.version == "9.1.0" and info.sha256 == SHA and info.size == len(PAYLOAD)
    assert updater.check(current="9.1.0") is None  # up to date
    assert updater.check(current="9.2.0") is None  # newer than the release (developer copy)


def test_check_uses_sidecar_sha256_file(fake_net):
    fake_net[updater.RELEASES_API] = _release("9.1.0", digest=False, sidecar=True)
    fake_net["https://github.com/x/sha"] = f"{SHA}  StoreWMS-Setup-9.1.0.exe".encode()
    assert updater.check(current="1.0").sha256 == SHA


@pytest.mark.parametrize("data", [
    _release("9.1.0", digest=False),                 # no fingerprint at all: never offered
    _release("9.1.0", tag="v9.2.0"),                 # tag and file name disagree
    _release("9.1.0", prerelease=True),
    _release("9.1.0", draft=True),
    {"tag_name": "v9.1.0", "assets": [{"name": "notes.txt", "browser_download_url": "https://x"}]},
    {},                                               # no release published yet (404)
])
def test_check_ignores_unsafe_or_unusable_releases(fake_net, data):
    fake_net[updater.RELEASES_API] = data
    assert updater.check(current="1.0") is None


def test_check_without_internet_raises_friendly_error(fake_net):
    with pytest.raises(updater.UpdateError, match="internet"):
        updater.check(current="1.0")


def test_download_checks_fingerprint_and_reuses_good_copy(fake_net, tmp_path):
    fake_net[updater.RELEASES_API] = _release("9.1.0")
    info = updater.check(current="1.0")
    fake_net[info.asset_url] = PAYLOAD
    (tmp_path / "StoreWMS-Setup-8.0.0.exe").write_bytes(b"old")  # an older download is cleaned up
    seen = []
    path = updater.download(info, tmp_path, progress=lambda d, t: seen.append((d, t)))
    assert path.read_bytes() == PAYLOAD and path.name == "StoreWMS-Setup-9.1.0.exe"
    assert seen[-1] == (len(PAYLOAD), len(PAYLOAD))
    assert not (tmp_path / "StoreWMS-Setup-8.0.0.exe").exists()
    del fake_net[info.asset_url]  # second time: no network needed, the checked copy is reused
    assert updater.download(info, tmp_path) == path


def test_download_rejects_tampered_file(fake_net, tmp_path):
    fake_net[updater.RELEASES_API] = _release("9.1.0")
    info = updater.check(current="1.0")
    fake_net[info.asset_url] = PAYLOAD[:-1] + b"X"
    with pytest.raises(updater.UpdateError, match="safety check"):
        updater.download(info, tmp_path)
    assert list(tmp_path.iterdir()) == []  # nothing left behind that could be run


def test_download_can_be_cancelled(fake_net, tmp_path):
    fake_net[updater.RELEASES_API] = _release("9.1.0")
    info = updater.check(current="1.0")
    fake_net[info.asset_url] = PAYLOAD
    with pytest.raises(updater.UpdateError, match="cancelled"):
        updater.download(info, tmp_path, cancelled=lambda: True)
    assert list(tmp_path.iterdir()) == []


def test_download_refuses_odd_file_names(tmp_path):
    info = updater.UpdateInfo("9.1.0", "", "", "..\\evil.exe", "https://x", 1, SHA)
    with pytest.raises(updater.UpdateError):
        updater.download(info, tmp_path)


def test_installer_runs_silently_and_relaunches(tmp_path):
    args = updater.installer_arguments(tmp_path / "update-install.log")
    assert {"/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/RELAUNCH=1"} <= set(args)
    iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert "{param:RELAUNCH|0}" in iss and "Check: RelaunchAfterUpdate" in iss


def test_launch_refused_outside_installed_windows_program(tmp_path, monkeypatch):
    monkeypatch.setattr(updater, "can_install_here", lambda: False)
    with pytest.raises(updater.UpdateError):
        updater.launch_installer(tmp_path / "x.exe", tmp_path / "log.txt")


def test_versions_agree_everywhere():
    """The release step publishes StoreWMS-Setup-<wms.__version__>.exe: the installer must carry the same number."""
    v = wms.__version__
    iss = (ROOT / "packaging" / "installer.iss").read_text(encoding="utf-8")
    assert re.search(r'#define AppVersion "([^"]+)"', iss).group(1) == v
    info = (ROOT / "packaging" / "version_info.txt").read_text(encoding="utf-8")
    assert f'"FileVersion", "{v}"' in info


def test_data_lives_outside_the_program_folder(tmp_path, monkeypatch):
    """Updates replace the program folder only: the database must never default to it."""
    from wms import config
    monkeypatch.delenv("WMS_DATA_DIR", raising=False)
    monkeypatch.setattr(config.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    assert config.default_data_dir() == tmp_path / "Local" / "StoreWMS"
    assert "Program Files" not in str(config.default_data_dir())


def test_update_settings_saved(svc):
    from wms.services.settings import EDITABLE
    assert "check_for_updates" in EDITABLE
    svc.settings.save_general({"check_for_updates": False})
    assert svc.ctx.config.check_for_updates is False


def test_update_dialog_builds_offscreen(svc):
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    from wms.ui.dialogs.update import UpdateDialog
    info = updater.UpdateInfo("9.1.0", "- Faster", "2026-11-15", "StoreWMS-Setup-9.1.0.exe", "https://x", 10, SHA)
    dlg = UpdateDialog(info, svc.ctx.config, svc)
    assert dlg.install_btn.text() in ("Install now", "Download page")
    dlg._skip()
    assert svc.ctx.config.skipped_update_version == "9.1.0"
    dlg.deleteLater()


def test_update_found_later_shows_status_bar_button_only(qtbot, svc, monkeypatch):
    """Found at sign-in: the window opens. Found later (PC left on): only a button, no interruption."""
    from PySide6.QtWidgets import QDialog

    from wms.ui.app import App
    from wms.ui.main_window import MainWindow
    opened = []
    monkeypatch.setattr(QDialog, "exec", lambda self: opened.append(type(self).__name__) or 0)
    win = MainWindow(App(svc.ctx))
    qtbot.addWidget(win)
    info = updater.UpdateInfo("9.1.0", "", "", "StoreWMS-Setup-9.1.0.exe", "https://x", 10, SHA)
    win._update_checks = 2
    win._update_found(info)
    assert opened == [] and "9.1.0" in win._update_button.text()
    win._update_button.click()
    assert opened == ["UpdateDialog"]
    svc.ctx.config.skipped_update_version = "9.2.0"
    win._update_found(updater.UpdateInfo("9.2.0", "", "", "StoreWMS-Setup-9.2.0.exe", "https://x", 10, SHA))
    assert "9.1.0" in win._update_button.text()  # a skipped version is not offered


def test_close_for_update_skips_exit_backup(qtbot, svc, monkeypatch):
    from wms.ui.app import App
    from wms.ui.main_window import MainWindow
    svc.ctx.config.auto_backup_on_exit = True
    made = []
    monkeypatch.setattr(svc.backup.__class__, "create", lambda *a, **k: made.append(k))
    win = MainWindow(App(svc.ctx))
    qtbot.addWidget(win)
    assert win.can_close_for_update()
    win.close_for_update()
    assert made == []

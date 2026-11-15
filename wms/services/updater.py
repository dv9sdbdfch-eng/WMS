"""Program updates: find a newer Store WMS on GitHub Releases, download it safely, install it.

How an update works (nothing here touches the data):

1. ``check()`` asks GitHub for the newest published release (a few KB, 8-second limit, so a PC
   with no internet just carries on). Only an installer that comes with its SHA-256 fingerprint is
   offered; anything else is ignored.
2. ``download()`` saves the installer to ``<data>/updates`` and refuses it unless its fingerprint
   and size match exactly (a broken or tampered download is deleted, never run).
3. The window makes a backup of this PC's database (SQLite) before going on.
4. ``launch_installer()`` starts the installer in silent mode and the program closes. The installer
   replaces only the program files in Program Files, then starts the new version, which upgrades
   the database (adding tables / columns) on its first start, after its own copy of the file.

The database, photos, settings and backups are in other folders (``%LOCALAPPDATA%\\StoreWMS``,
``Documents\\StoreWMS Backups``) that the installer never touches.

This module has no Qt in it so it can be tested on its own; the windows are in
``wms.ui.dialogs.update``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from wms import APP_ID, __version__

log = logging.getLogger(__name__)

GITHUB_REPO = "dv9sdbdfch-eng/wms"
RELEASES_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases/latest"
INSTALLER_RE = re.compile(r"^StoreWMS-Setup-(\d+(?:\.\d+)*)\.exe$", re.IGNORECASE)
USER_AGENT = f"{APP_ID}-updater/{__version__}"
CHECK_TIMEOUT = 8          # seconds; a PC without internet must not wait long at start-up
DOWNLOAD_TIMEOUT = 30      # seconds without any data before a download gives up
MAX_INSTALLER_BYTES = 500 * 1024 * 1024


class UpdateError(Exception):
    """An update could not be checked, downloaded or started. The message is for the user."""


@dataclass
class UpdateInfo:
    version: str
    notes: str
    published: str
    asset_name: str
    asset_url: str
    size: int
    sha256: str
    page_url: str = RELEASES_PAGE


def parse_version(text: str) -> tuple[int, ...]:
    """'v1.33.0' -> (1, 33, 0). Anything unreadable counts as 0.0.0 (never newer)."""
    m = re.match(r"^\s*v?(\d+(?:\.\d+)*)", text or "")
    if not m:
        return (0,)
    parts = [int(p) for p in m.group(1).split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


def is_newer(candidate: str, current: str = __version__) -> bool:
    return parse_version(candidate) > parse_version(current)


def can_install_here() -> bool:
    """Only the installed program (StoreWMS.exe on Windows) can replace itself. Running from source
    (developers, tests) the update window just points at the download page."""
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def _request(url: str, timeout: float, accept: str = "application/vnd.github+json"):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": accept})
    return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - fixed https URLs only


def _get_json(url: str, timeout: float) -> dict:
    try:
        with _request(url, timeout) as resp:
            return json.loads(resp.read(2_000_000).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return {}
        raise UpdateError(f"The update server answered with error {exc.code}.") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise UpdateError("Could not reach the update server. Check the internet connection and try again.") \
            from exc
    except (ValueError, UnicodeDecodeError) as exc:
        raise UpdateError("The update server sent an answer that could not be read.") from exc


def _get_text(url: str, timeout: float) -> str:
    try:
        with _request(url, timeout, accept="application/octet-stream") as resp:
            return resp.read(10_000).decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError):
        return ""


def _sha256_of(asset: dict, assets: list[dict], timeout: float) -> str:
    """The installer's SHA-256: GitHub's own 'digest' field, or the '<installer>.sha256' file the
    build publishes next to it. Empty when neither is there (then nothing is offered)."""
    digest = str(asset.get("digest") or "")
    if digest.lower().startswith("sha256:"):
        value = digest.split(":", 1)[1].strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", value):
            return value
    for other in assets:
        if str(other.get("name", "")).lower() == str(asset.get("name", "")).lower() + ".sha256":
            m = re.search(r"\b([0-9a-fA-F]{64})\b", _get_text(other.get("browser_download_url", ""), timeout))
            if m:
                return m.group(1).lower()
    return ""


def release_from_json(data: dict, timeout: float = CHECK_TIMEOUT) -> UpdateInfo | None:
    """Turn GitHub's 'latest release' answer into an UpdateInfo (None: nothing usable in it)."""
    if not data or data.get("draft") or data.get("prerelease"):
        return None
    assets = [a for a in data.get("assets") or [] if isinstance(a, dict)]
    for asset in assets:
        m = INSTALLER_RE.match(str(asset.get("name", "")))
        if not m:
            continue
        url = str(asset.get("browser_download_url", ""))
        if not url.startswith("https://"):
            continue
        sha = _sha256_of(asset, assets, timeout)
        if not sha:
            log.warning("Release %s has no SHA-256 for %s; not offered", data.get("tag_name"), asset.get("name"))
            return None
        tag_version = str(data.get("tag_name") or "").lstrip("vV")
        version = m.group(1)
        if tag_version and parse_version(tag_version) != parse_version(version):
            log.warning("Release tag %s does not match installer %s; not offered", data.get("tag_name"), version)
            return None
        return UpdateInfo(version=version, notes=str(data.get("body") or "").strip(),
                          published=str(data.get("published_at") or "")[:10], asset_name=str(asset["name"]),
                          asset_url=url, size=int(asset.get("size") or 0), sha256=sha,
                          page_url=str(data.get("html_url") or RELEASES_PAGE))
    return None


def check(current: str = __version__, timeout: float = CHECK_TIMEOUT) -> UpdateInfo | None:
    """The newest release if it is newer than ``current``; None when this copy is up to date.
    Raises UpdateError when the update server cannot be reached."""
    info = release_from_json(_get_json(RELEASES_API, timeout), timeout)
    if info is None or not is_newer(info.version, current):
        return None
    return info


def updates_dir(cfg) -> Path:
    return Path(cfg.data_dir) / "updates"


def download(info: UpdateInfo, dest_dir: Path, progress: Callable[[int, int], None] | None = None,
             cancelled: Callable[[], bool] | None = None, timeout: float = DOWNLOAD_TIMEOUT) -> Path:
    """Download the installer, check its size and SHA-256, return its path. A copy already
    downloaded and checked is reused. Older installers in the folder are removed."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    if not INSTALLER_RE.match(info.asset_name) or "/" in info.asset_name or "\\" in info.asset_name:
        raise UpdateError("The update file has an unexpected name and was not downloaded.")
    target = dest_dir / info.asset_name
    for old in dest_dir.glob("StoreWMS-Setup-*"):
        if old.name != target.name:
            old.unlink(missing_ok=True)
    if target.exists() and _file_sha256(target) == info.sha256:
        return target
    part = target.with_name(target.name + ".part")
    h = hashlib.sha256()
    done = 0
    try:
        with _request(info.asset_url, timeout, accept="application/octet-stream") as resp, open(part, "wb") as out:
            total = int(resp.headers.get("Content-Length") or info.size or 0)
            while True:
                if cancelled and cancelled():
                    raise UpdateError("Download cancelled.")
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                done += len(chunk)
                if done > MAX_INSTALLER_BYTES:
                    raise UpdateError("The update file is far larger than expected and was not kept.")
                h.update(chunk)
                out.write(chunk)
                if progress:
                    progress(done, total)
    except UpdateError:
        part.unlink(missing_ok=True)
        raise
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        part.unlink(missing_ok=True)
        raise UpdateError(f"The download stopped: {getattr(exc, 'reason', None) or exc}. "
                          f"Check the internet connection and try again.") from exc
    if (info.size and done != info.size) or h.hexdigest() != info.sha256:
        part.unlink(missing_ok=True)
        raise UpdateError("The downloaded file did not pass the safety check (its fingerprint does not match) "
                          "and was deleted. Nothing was changed. Try again later.")
    part.replace(target)
    return target


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def installer_arguments(log_file: Path) -> list[str]:
    """Inno Setup: no questions, a progress bar only, close the old program if it is still open,
    start the new version at the end (/RELAUNCH=1, see packaging/installer.iss)."""
    return ["/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/CLOSEAPPLICATIONS", "/RELAUNCH=1",
            f"/LOG={log_file}"]


def launch_installer(installer: Path, log_file: Path) -> None:
    """Start the installer and return at once; the caller then closes the program.
    Windows asks for administrator permission (UAC) if the program is installed for all users."""
    if not can_install_here():
        raise UpdateError("Updates can only be installed by the installed program on Windows.")
    if not Path(installer).is_file():
        raise UpdateError(f"The update file is missing: {installer}")
    args = installer_arguments(Path(log_file))
    try:
        # ShellExecute (not CreateProcess) so Windows can show its permission prompt when needed.
        os.startfile(str(installer), arguments=subprocess.list2cmdline(args))  # type: ignore[attr-defined]
    except OSError as exc:
        raise UpdateError(f"The installer could not be started: {exc.strerror or exc}") from exc

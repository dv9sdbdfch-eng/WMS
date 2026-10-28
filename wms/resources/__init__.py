"""Bundled files (icon). Works both from source and from the PyInstaller build."""

from __future__ import annotations

import sys
from pathlib import Path


def resource_dir() -> Path:
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "wms" / "resources" if base else Path(__file__).parent


def icon_path() -> Path:
    return resource_dir() / "icon.ico"

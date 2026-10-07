"""Locations of PolyCopy's data files."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def data_dir() -> Path:
    """Return (and create) the directory that holds settings, the database and logs.

    ``POLYCOPY_HOME`` overrides the default. On macOS the default is
    ``~/Library/Application Support/PolyCopy``; elsewhere ``~/.polycopy``.
    """
    override = os.environ.get("POLYCOPY_HOME")
    if override:
        path = Path(override).expanduser()
    elif sys.platform == "darwin":
        path = Path.home() / "Library" / "Application Support" / "PolyCopy"
    else:
        path = Path.home() / ".polycopy"
    path.mkdir(parents=True, exist_ok=True)
    return path


def static_dir() -> Path:
    """Directory with the web terminal's static files (works inside PyInstaller bundles)."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / "polycopy" / "web" / "static"
    return Path(__file__).resolve().parent / "web" / "static"

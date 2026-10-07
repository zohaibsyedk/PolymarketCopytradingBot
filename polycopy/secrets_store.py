"""Storage for the private key and API credentials.

On macOS secrets go into the login Keychain (via ``keyring``), so they are
encrypted at rest and never written to PolyCopy's data folder. If no keychain
backend is available (for example on a headless Linux box) the secrets fall
back to a file readable only by the current user.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from pathlib import Path
from typing import Any

from polycopy.paths import data_dir

log = logging.getLogger(__name__)

SERVICE = "PolyCopy"
ACCOUNT = "credentials"


class SecretStore:
    def __init__(self, fallback_path: Path | None = None, use_keyring: bool = True) -> None:
        self.fallback_path = fallback_path or (data_dir() / ".credentials.json")
        self._keyring = None
        if use_keyring:
            try:
                import keyring

                if _backend_usable(keyring.get_keyring()):
                    self._keyring = keyring
            except Exception as error:  # pragma: no cover - depends on platform
                log.info("Keychain unavailable (%s); using file storage", error)

    @property
    def backend_name(self) -> str:
        return "macOS Keychain" if self._keyring is not None else "local file"

    def load(self) -> dict[str, Any] | None:
        raw: str | None = None
        if self._keyring is not None:
            try:
                raw = self._keyring.get_password(SERVICE, ACCOUNT)
            except Exception as error:  # pragma: no cover
                log.warning("Keychain read failed: %s", error)
        if raw is None and self.fallback_path.exists():
            try:
                raw = self.fallback_path.read_text()
            except OSError:
                raw = None
        if not raw:
            return None
        try:
            data = json.loads(raw)
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    def save(self, data: dict[str, Any]) -> None:
        payload = json.dumps(data)
        if self._keyring is not None:
            try:
                self._keyring.set_password(SERVICE, ACCOUNT, payload)
                if self.fallback_path.exists():
                    self.fallback_path.unlink()
                return
            except Exception as error:  # pragma: no cover
                log.warning("Keychain write failed (%s); using file storage", error)
        fd = os.open(self.fallback_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(payload)
        os.chmod(self.fallback_path, stat.S_IRUSR | stat.S_IWUSR)

    def clear(self) -> None:
        if self._keyring is not None:
            try:
                self._keyring.delete_password(SERVICE, ACCOUNT)
            except Exception:
                pass
        if self.fallback_path.exists():
            self.fallback_path.unlink()


def _backend_usable(backend: Any) -> bool:
    name = type(backend).__module__ + "." + type(backend).__name__
    if "fail" in name.lower() or "null" in name.lower():
        return False
    if "chainer" in name.lower():
        return bool(getattr(backend, "backends", None))
    return True

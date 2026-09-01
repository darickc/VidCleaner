"""Symmetric encryption for secret settings (PLAN.md §5).

Integration API keys live in the SQLite database, which sits on a share the user may
back up or copy around, so they are encrypted with a Fernet key kept in a 0600 file at
``<config_dir>/secret.key``. Losing the key file only means re-entering the keys.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"


class SecretBox:
    def __init__(self, key_path: Path) -> None:
        self.key_path = key_path
        self._fernet: Fernet | None = None

    def _load_key(self) -> Fernet:
        if self._fernet is None:
            if not self.key_path.exists():
                self.key_path.parent.mkdir(parents=True, exist_ok=True)
                # Create 0600 before writing, so the key is never briefly world-readable.
                fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(Fernet.generate_key())
            self._fernet = Fernet(self.key_path.read_bytes().strip())
        return self._fernet

    @staticmethod
    def is_encrypted(value: str) -> bool:
        return value.startswith(PREFIX)

    def encrypt(self, value: str) -> str:
        if not value:
            return ""
        return PREFIX + self._load_key().encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        """Returns plaintext. A value stored before encryption is passed through."""
        if not value or not self.is_encrypted(value):
            return value
        try:
            return self._load_key().decrypt(value[len(PREFIX) :].encode()).decode()
        except InvalidToken:
            # Wrong or regenerated key file: treat the secret as unset rather than
            # crashing the whole settings page.
            return ""


@lru_cache(maxsize=1)
def get_secret_box() -> SecretBox:
    from vidcleaner.config import get_settings

    return SecretBox(get_settings().config_dir / "secret.key")

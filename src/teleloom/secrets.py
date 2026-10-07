import os
from typing import Any

import keyring
from keyring.errors import KeyringError

from .models import TeleloomError


class Secrets:
    """OS credentials, with explicit environment overrides; never plaintext files."""

    @staticmethod
    def backend() -> Any:
        backend = keyring.get_keyring()
        candidates = getattr(backend, "backends", [backend])
        secure_modules = (
            "keyring.backends.Windows",
            "keyring.backends.macOS",
            "keyring.backends.SecretService",
            "keyring.backends.kwallet",
        )
        return next(
            (
                candidate
                for candidate in candidates
                if type(candidate).__module__.startswith(secure_modules) and candidate.priority > 0
            ),
            None,
        )

    def get(self, profile: str, name: str) -> str | None:
        variable = (
            f"TELELOOM_{profile.upper()}_{name.upper()}" if profile else f"TELELOOM_{name.upper()}"
        )
        if value := os.getenv(variable):
            return value
        try:
            backend = self.backend()
            return backend.get_password("teleloom", f"{profile}:{name}") if backend else None
        except KeyringError:
            raise TeleloomError(
                "credentials_unavailable", f"Configure an OS keyring or set {variable}."
            ) from None

    def require(self, profile: str, name: str) -> str:
        value = self.get(profile, name)
        if not value:
            variable = (
                f"TELELOOM_{profile.upper()}_{name.upper()}"
                if profile
                else f"TELELOOM_{name.upper()}"
            )
            raise TeleloomError(
                "credentials_missing",
                f"Missing credential. Set {variable} or run teleloom init/auth.",
            )
        return value

    def set(self, profile: str, name: str, value: str) -> None:
        backend = self.backend()
        if backend is None:
            raise TeleloomError(
                "credentials_unavailable", "A secure OS credential backend is required."
            )
        try:
            backend.set_password("teleloom", f"{profile}:{name}", value)
        except KeyringError:
            raise TeleloomError(
                "credentials_unavailable", "Cannot save credentials in the OS keyring."
            ) from None

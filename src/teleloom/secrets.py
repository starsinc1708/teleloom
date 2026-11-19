import os
from contextlib import suppress
from typing import Any

import keyring
from keyring.errors import KeyringError

from .models import TeleloomError

# Profile names start with a letter; this keyring and env namespace cannot overlap.
NAMED_PROXY = "_named_proxy"
_PROFILE_CREDENTIALS = {
    "api_id",
    "api_hash",
    "session",
    "bot_token",
    "proxy",
    "openai_transcription_key",
    "groq_transcription_key",
}


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

    def get(self, profile: str, name: str, *, environment: bool = True) -> str | None:
        variable = (
            f"TELELOOM_{profile.upper()}_{name.upper()}" if profile else f"TELELOOM_{name.upper()}"
        )
        if environment and (value := os.getenv(variable)):
            return value
        try:
            backend = self.backend()
            return backend.get_password("teleloom", f"{profile}:{name}") if backend else None
        except KeyringError:
            raise TeleloomError(
                "credentials_unavailable", f"Configure an OS keyring or set {variable}."
            ) from None

    def named_proxy(self, name: str, *, legacy: bool) -> str | None:
        value = self.get(NAMED_PROXY, name)
        if value is not None or not legacy:
            return value
        value = self.get("proxy", name)
        if value is not None and name in _PROFILE_CREDENTIALS:
            raise TeleloomError(
                "proxy_credentials_ambiguous",
                "Re-enter the named proxy credentials in the proxy catalogue. "
                "The old slot overlaps a profile credential; no alternate route was selected.",
            )
        return value

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

    def delete(self, profile: str, name: str) -> None:
        """Roll back a credential this process just wrote; a missing key is already rolled back."""
        backend = self.backend()
        if backend is None:
            return
        with suppress(KeyringError):
            backend.delete_password("teleloom", f"{profile}:{name}")

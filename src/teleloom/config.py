import ctypes
import json
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Literal

from platformdirs import user_data_path
from pydantic import BaseModel, Field, field_validator

from .models import TeleloomError


def data_path() -> Path:
    override = os.getenv("TELELOOM_DATA_DIR")
    return (
        Path(override).expanduser().resolve()
        if override
        else user_data_path("teleloom", appauthor=False)
    )


def windows_system_directory() -> Path:
    if sys.platform != "win32":
        raise OSError("Windows system directory is unavailable on this platform.")
    buffer = ctypes.create_unicode_buffer(32768)
    size = ctypes.windll.kernel32.GetSystemDirectoryW(buffer, len(buffer))
    if not 0 < size < len(buffer):
        raise OSError("Windows system directory is unavailable.")
    return Path(buffer.value)


def private_dir(path: Path) -> None:
    created = not path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created and sys.platform == "win32":
        system = windows_system_directory()
        identity = subprocess.run(
            [str(system / "whoami.exe"), "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        import csv

        sid = next(csv.reader([identity.stdout.strip()]))[1]
        subprocess.run(
            [
                str(system / "icacls.exe"),
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"*{sid}:(OI)(CI)F",
                "*S-1-5-18:(OI)(CI)F",
            ],
            capture_output=True,
            check=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )


class Limits(BaseModel):
    recipients: int = Field(default=50, ge=1, le=1000)
    interval_seconds: float = Field(default=5, ge=1)
    daily_messages: int = Field(default=100, ge=1)
    jev_daily_calls: int = Field(default=100, ge=1)
    jev_max_characters: int = Field(default=16000, ge=1000, le=64000)


class TranscriptionConfig(BaseModel):
    local_model_path: str | None = None
    openai_endpoint: str | None = None
    openai_model: str = Field(default="whisper-1", min_length=1, max_length=200)
    groq_model: str = Field(default="whisper-large-v3-turbo", min_length=1, max_length=200)
    external_daily_calls: int = Field(default=0, ge=0, le=10000)
    external_daily_bytes: int = Field(default=0, ge=0, le=1_000_000_000)


class Profile(BaseModel):
    kind: Literal["user", "bot"]
    bot_backend: Literal["bot_api", "mtproto"] = "bot_api"
    generation: str = Field(default_factory=lambda: uuid.uuid4().hex)
    api_id: int | None = None
    identity: dict[str, str] = Field(default_factory=dict)
    send_chats: list[str] = Field(default_factory=list)
    mutation_chats: list[str] = Field(default_factory=list)
    broadcast_chats: list[str] = Field(default_factory=list)
    sync_chats: list[str] = Field(default_factory=list)
    jev_chats: list[str] = Field(default_factory=list)
    manage_scopes: list[Literal["contacts", "folders", "groups", "account"]] = Field(
        default_factory=list
    )
    file_roots: list[str] = Field(default_factory=list)
    polling: bool = False
    event_chats: list[str] = Field(default_factory=list)
    event_retention_hours: int = Field(default=24, ge=1, le=168)
    transcription_chats: list[str] = Field(default_factory=list)
    transcription_external_chats: list[str] = Field(default_factory=list)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    read_mode: Literal["all", "selected"] = "all"
    read_chats: list[str] = Field(default_factory=list)

    def allows_read(self, chat: str) -> bool:
        return self.read_mode == "all" or chat in self.read_chats

    def require_read(self, chat: str) -> None:
        if not self.allows_read(chat):
            raise TeleloomError("read_not_allowed", "This chat is outside the owner's read policy.")

    def read_policy(self) -> list[object]:
        return [self.read_mode, sorted(self.read_chats) if self.read_mode == "selected" else []]

    def grants(self) -> dict[str, object]:
        return {
            "read": {"mode": self.read_mode, "chat_ids": self.read_chats},
            **{
                grant: [chat for chat in getattr(self, field) if self.allows_read(chat)]
                for grant, field in {
                    "send": "send_chats",
                    "mutation": "mutation_chats",
                    "broadcast": "broadcast_chats",
                    "sync": "sync_chats",
                    "ai": "jev_chats",
                    "event": "event_chats",
                    "transcription": "transcription_chats",
                    "transcription_external": "transcription_external_chats",
                }.items()
            },
            "management": self.manage_scopes,
        }


class Settings(BaseModel):
    data_dir: Path = Field(default_factory=data_path, exclude=True)
    port: int = Field(default=8765, ge=1024, le=65535)
    read_timeout_seconds: float = Field(default=30, ge=0.05, le=40)
    profiles: dict[str, Profile] = Field(default_factory=dict)
    limits: Limits = Field(default_factory=Limits)
    jev_model: str = "jev-latest"
    exposure_mode: Literal["all", "read-only", "selected"] = "all"
    exposed_tools: list[str] = Field(default_factory=list)

    @field_validator("profiles")
    @classmethod
    def validate_profile_names(cls, value: dict[str, Profile]) -> dict[str, Profile]:
        for name in value:
            validate_name(name)
        return value

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    @classmethod
    def load(cls, directory: Path | None = None) -> "Settings":
        directory = directory or data_path()
        file = directory / "config.json"
        if not file.exists():
            return cls(data_dir=directory)
        return cls.model_validate(
            {**json.loads(file.read_text(encoding="utf-8")), "data_dir": directory}
        )

    def save(self) -> None:
        private_dir(self.data_dir)
        file = self.data_dir / "config.json"
        temporary = file.with_suffix(".tmp")
        temporary.write_text(self.model_dump_json(indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(file)

    def profile(self, name: str) -> Profile:
        if name not in self.profiles:
            raise TeleloomError(
                "profile_not_found", "Unknown profile. Run teleloom auth user or auth bot."
            )
        return self.profiles[name]


def validate_name(name: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", name):
        raise ValueError(
            "Profile names must start with a lowercase letter and use letters, digits or underscores."
        )
    return name

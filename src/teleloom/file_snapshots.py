"""Owner-selected file bytes frozen before confirmation, without following links."""

import hashlib
import os
import re
import stat
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO

from .config import Settings, private_dir
from .models import TeleloomError, utcnow
from .store import Store

FILE_LIMIT = 50_000_000
DISK_LIMIT = 500_000_000


def plain_path(value: str | Path, *, directory: bool = False) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts:
        raise TeleloomError(
            "unsafe_file_path", "Use an absolute path without traversal components."
        )
    try:
        for part in (*reversed(path.parents), path):
            if part.is_symlink() or part.is_junction():
                raise TeleloomError(
                    "unsafe_file_path", "File paths cannot contain links or junctions."
                )
            if part.exists() and getattr(part.lstat(), "st_file_attributes", 0) & 0x400:
                raise TeleloomError("unsafe_file_path", "File paths cannot contain reparse points.")
        if directory and not path.is_dir() or not directory and not path.is_file():
            raise TeleloomError(
                "unsafe_file_path", "The selected file or directory is unavailable."
            )
        return path.resolve(strict=True)
    except (OSError, ValueError):
        raise TeleloomError("unsafe_file_path", "The selected file path is unavailable.") from None


def _handle_path(stream: BinaryIO, path: Path) -> Path:
    if sys.platform == "win32":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        api = ctypes.WinDLL("kernel32", use_last_error=True)
        final_path = api.GetFinalPathNameByHandleW
        final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        final_path.restype = wintypes.DWORD
        buffer = ctypes.create_unicode_buffer(32768)
        length = final_path(msvcrt.get_osfhandle(stream.fileno()), buffer, len(buffer), 0)
        if not length or length >= len(buffer):
            raise TeleloomError(
                "unsafe_file_path", "The opened file identity could not be verified."
            )
        value = buffer.value
        return Path("\\\\" + value[8:] if value.startswith("\\\\?\\UNC\\") else value[4:])
    descriptor = Path(f"/proc/self/fd/{stream.fileno()}")
    return descriptor.resolve() if descriptor.exists() else path.resolve(strict=True)


def read_verified(path: Path, max_bytes: int) -> bytes:
    path = plain_path(path)
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(path, flags), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or _handle_path(stream, path) != path:
                raise TeleloomError(
                    "unsafe_file_path", "The opened file differs from its verified path."
                )
            if before.st_size > max_bytes:
                raise TeleloomError("file_too_large", "The selected file exceeds the byte budget.")
            content = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
            current = plain_path(path).stat()

            def identity(info: os.stat_result) -> tuple[int, int, int, int]:
                return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns

            if identity(before) != identity(after) or identity(after) != identity(current):
                raise TeleloomError("source_changed", "The selected file changed during capture.")
            if _handle_path(stream, path) != path:
                raise TeleloomError(
                    "unsafe_file_path", "The opened file moved outside its verified path."
                )
        if not content or len(content) > max_bytes:
            raise TeleloomError("file_too_large", "Select a nonempty file within the byte budget.")
        return content
    except OSError:
        raise TeleloomError(
            "unsafe_file_path", "The selected file cannot be read safely."
        ) from None


def write_new_verified(path: Path, content: bytes) -> None:
    parent = plain_path(path.parent, directory=True)
    expected = parent / path.name
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_BINARY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(os.open(expected, flags, 0o600), "wb") as stream:
            if _handle_path(stream, expected) != expected or plain_path(expected) != expected:
                raise TeleloomError(
                    "unsafe_file_path", "The opened destination differs from its allowed path."
                )
            stream.write(content)
            stream.flush()
            if _handle_path(stream, expected) != expected or plain_path(expected) != expected:
                raise TeleloomError("unsafe_file_path", "The destination moved during its write.")
        if read_verified(expected, FILE_LIMIT) != content:
            raise TeleloomError("source_changed", "The saved file changed during its write.")
    except OSError:
        raise TeleloomError(
            "unsafe_file_path", "The new destination could not be written safely."
        ) from None


class FileSnapshots:
    def __init__(self, settings: Settings, store: Store) -> None:
        self.settings, self.store = settings, store

    def _source(self, profile_id: str, source: str) -> Path:
        path = plain_path(source)
        roots = self.settings.profile(profile_id).file_roots
        if not any(path.is_relative_to(plain_path(root, directory=True)) for root in roots):
            raise TeleloomError("file_not_allowed", "The owner has not allowed this file root.")
        return path

    def _path(self, snapshot_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", snapshot_id):
            raise TeleloomError("snapshot_not_found", "The selected snapshot is unavailable.")
        root = self.settings.data_dir / "file-snapshots"
        if root.exists():
            plain_path(root, directory=True)
        else:
            plain_path(root.parent, directory=True)
            private_dir(root)
        return root / (snapshot_id + ".bin")

    def capture(self, profile_id: str, source: str, max_bytes: int = FILE_LIMIT) -> dict[str, Any]:
        if not 1 <= max_bytes <= FILE_LIMIT:
            raise TeleloomError("invalid_limit", "File bytes must be 1..50000000.")
        path = self._source(profile_id, source)
        content = read_verified(path, max_bytes)
        snapshot_id = uuid.uuid4().hex
        destination = self._path(snapshot_id)
        used = sum(plain_path(child).stat().st_size for child in destination.parent.glob("*.bin"))
        if used + len(content) > DISK_LIMIT:
            raise TeleloomError(
                "file_disk_limit", "The private file snapshot disk budget is exhausted."
            )
        write_new_verified(destination, content)
        destination.chmod(0o600)
        import mimetypes

        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if content.startswith(b"\x89PNG\r\n\x1a\n"):
            mime = "image/png"
        elif content.startswith(b"\xff\xd8\xff"):
            mime = "image/jpeg"
        elif content.startswith((b"GIF87a", b"GIF89a")):
            mime = "image/gif"
        elif content.startswith(b"RIFF") and content[8:12] == b"WEBP":
            mime = "image/webp"
        elif content.startswith(b"OggS") and b"OpusHead" in content:
            mime = "audio/ogg"
        elif content[4:8] == b"ftyp":
            mime = "video/mp4"
        descriptor = {
            "snapshot_id": snapshot_id,
            "source_path": str(path),
            "name": path.name,
            "size": len(content),
            "mime_type": mime,
            "sha256": hashlib.sha256(content).hexdigest(),
            "expires_at": (utcnow() + timedelta(minutes=15)).isoformat(),
        }
        self.store.set_state(
            "file_snapshot:" + snapshot_id,
            {
                "profile_id": profile_id,
                "generation": self.settings.profile(profile_id).generation,
                **descriptor,
            },
        )
        return descriptor

    def validate(
        self, profile_id: str, descriptor: dict[str, Any], *, require_source: bool = True
    ) -> bytes:
        record = self.store.state("file_snapshot:" + descriptor["snapshot_id"])
        if not record or record["profile_id"] != profile_id:
            raise TeleloomError(
                "snapshot_not_found", "The selected snapshot is unavailable to this profile."
            )
        if record["generation"] != self.settings.profile(profile_id).generation:
            raise TeleloomError(
                "account_changed", "The file snapshot belongs to a previous account."
            )
        fields = {"snapshot_id", "source_path", "name", "size", "mime_type", "sha256", "expires_at"}
        if any(record[key] != descriptor.get(key) for key in fields):
            raise TeleloomError("snapshot_changed", "The file descriptor changed after preview.")
        if utcnow() >= datetime.fromisoformat(record["expires_at"]):
            raise TeleloomError("snapshot_expired", "Create a fresh file preview.")
        source = self._source(profile_id, record["source_path"])
        if (
            require_source
            and hashlib.sha256(read_verified(source, FILE_LIMIT)).hexdigest() != record["sha256"]
        ):
            raise TeleloomError("source_changed", "The original file differs from its preview.")
        content = read_verified(self._path(record["snapshot_id"]), FILE_LIMIT)
        if (
            len(content) != record["size"]
            or hashlib.sha256(content).hexdigest() != record["sha256"]
        ):
            raise TeleloomError("snapshot_changed", "The frozen file differs from its preview.")
        return content

    def discard(self, snapshot_id: str) -> None:
        path = self._path(snapshot_id)
        if path.exists():
            plain_path(path).unlink()
        with self.store.db:
            self.store.db.execute(
                "DELETE FROM state WHERE key=?", ("file_snapshot:" + snapshot_id,)
            )

    def cleanup_expired(self, profile_id: str | None = None) -> int:
        records = self.store.db.execute(
            "SELECT data FROM state WHERE key GLOB 'file_snapshot:*'"
        ).fetchall()
        import json

        removed = 0
        for row in records:
            record = json.loads(row[0])
            if (
                profile_id is None or record["profile_id"] == profile_id
            ) and utcnow() >= datetime.fromisoformat(record["expires_at"]):
                self.discard(record["snapshot_id"])
                removed += 1
        return removed

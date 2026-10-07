"""Explicitly selected attachment reads through the existing durable queue.

Extractors run in a disposable local process. No extractor opens a Telegram
connection, downloads a model, evaluates document code, or submits data to AI.
"""

import asyncio
import codecs
import importlib
import importlib.util
import json
import os
import re
import shutil
import signal
import sys
import time
import zipfile
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree

from .adapters import Adapter, telegram_error
from .config import Settings, private_dir
from .models import Message, TeleloomError, utcnow
from .store import Store

if TYPE_CHECKING:
    from .jobs import Jobs

DISK_LIMIT = 500_000_000
WORKER_DISK_LIMIT = 50_000_000
FREE_RESERVE = 10_000_000
TEXT_MIMES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/xml",
    "application/json",
    "application/xml",
}
DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MODEL_FILES = ("model.bin", "config.json", "tokenizer.json")


def _installed(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _local_model(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value).expanduser()
    if not path.is_absolute() or not path.is_dir():
        return None
    return path.resolve() if all((path / file).is_file() for file in MODEL_FILES) else None


def _engine_error(method: str) -> TeleloomError:
    install = {
        "pdf_text": "Install teleloom[pdf] in the daemon environment.",
        "image_ocr": "Install teleloom[ocr] and the Tesseract executable on PATH with its language data.",
        "audio_transcription": "Install teleloom[transcription] and supply an absolute transcription_model_path to an existing CTranslate2 model directory containing model.bin, config.json and tokenizer.json.",
    }[method]
    return TeleloomError(
        "engine_unavailable", install, details={"method": method, "installation": install}
    )


def _model_revision(model: Path) -> list[list[Any]]:
    return [
        [name, (model / name).stat().st_size, (model / name).stat().st_mtime_ns]
        for name in MODEL_FILES
    ]


class AttachmentManager:
    def __init__(
        self,
        settings: Settings,
        store: Store,
        adapter: Callable[[str], Awaitable[Adapter]],
        jobs: "Jobs",
    ) -> None:
        self.settings, self.store, self.adapter, self.jobs = settings, store, adapter, jobs
        self.whisper_process: asyncio.subprocess.Process | None = None
        self.whisper_model: str | None = None
        self.whisper_revision: str | None = None

    async def close(self) -> None:
        process = self.whisper_process
        self.whisper_process = None
        if process is not None and process.returncode is None:
            cleanup = asyncio.create_task(_terminate(process))
            try:
                await asyncio.shield(cleanup)
            except BaseException:
                await cleanup
                raise

    def capabilities(self, transcription_model_path: str | None = None) -> dict[str, Any]:
        return {
            "text_utf8": {"available": True, "formats": sorted(TEXT_MIMES)},
            "docx_text": {"available": True, "format": DOCX_MIME},
            "pdf_text": {
                "available": _installed("pypdf"),
                "installation": _engine_error("pdf_text").message,
                "scanned_pdf_ocr": False,
            },
            "image_ocr": {
                "available": _installed("PIL")
                and _installed("pytesseract")
                and shutil.which("tesseract") is not None,
                "installation": _engine_error("image_ocr").message,
            },
            "audio_transcription": {
                "available": _installed("faster_whisper")
                and _local_model(transcription_model_path) is not None,
                "installation": _engine_error("audio_transcription").message,
                "automatic_model_download": False,
            },
            "processing": "local",
            "disk_limit_bytes": DISK_LIMIT,
            "max_selected_messages": 50,
            "bot_source": "persisted_updates_only",
        }

    async def start(
        self,
        profile_id: str,
        chat_id: str,
        message_ids: list[str],
        *,
        max_bytes: int = 10_000_000,
        max_characters: int = 50_000,
        timeout_seconds: int = 30,
        retention_hours: int = 24,
        transcription_model_path: str | None = None,
    ) -> dict[str, Any]:
        from .runtime import number

        self.settings.profile(profile_id).require_read(chat_id)
        number(chat_id)
        if not message_ids or len(message_ids) > 50 or len(set(message_ids)) != len(message_ids):
            raise TeleloomError(
                "invalid_selection", "Select 1 to 50 unique message IDs from one explicit chat."
            )
        for message_id in message_ids:
            number(message_id, positive=True)
        bounds = (
            (max_bytes, 1, 100_000_000),
            (max_characters, 1, 500_000),
            (timeout_seconds, 1, 120),
            (retention_hours, 1, 168),
        )
        if any(not low <= value <= high for value, low, high in bounds):
            raise TeleloomError(
                "invalid_limit",
                "Bytes must be 1..100000000, characters 1..500000, timeout 1..120 seconds and retention 1..168 hours.",
            )
        if transcription_model_path and _local_model(transcription_model_path) is None:
            raise _engine_error("audio_transcription")
        with self.store.db:
            job = self.jobs._job(
                profile_id,
                "attachments",
                {
                    "chat_id": chat_id,
                    "message_ids": message_ids,
                    "position": 0,
                    "max_bytes": max_bytes,
                    "max_characters": max_characters,
                    "timeout_seconds": timeout_seconds,
                    "expires_at": (utcnow() + timedelta(hours=retention_hours)).isoformat(),
                    "transcription_model_path": str(_local_model(transcription_model_path))
                    if transcription_model_path
                    else None,
                    "bytes_used": 0,
                    "characters_used": 0,
                },
            )
        return {
            "job_id": job["id"],
            "status": job["status"],
            "expires_at": job["payload"]["expires_at"],
            "capabilities": self.capabilities(transcription_model_path),
            "source": "bot_updates"
            if self.settings.profile(profile_id).kind == "bot"
            else "telegram",
        }

    def _directory(self, job: dict[str, Any], *, create: bool = False) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job["id"]):
            raise TeleloomError("unsafe_attachment_path", "Invalid attachment job identity.")
        root = self.settings.data_dir / "attachments"
        directory = root / job["id"]
        if (
            root.is_symlink()
            or root.is_junction()
            or directory.is_symlink()
            or directory.is_junction()
        ):
            raise TeleloomError("unsafe_attachment_path", "Attachment paths cannot be symlinks.")
        if directory.resolve().parent != root.resolve():
            raise TeleloomError("unsafe_attachment_path", "Attachment path escaped its owned root.")
        if create:
            private_dir(root)
            private_dir(directory)
        return directory

    def _disk_bytes(self, directory: Path) -> int:
        total = 0
        if directory.exists():
            for child in directory.rglob("*"):
                if child.is_symlink() or child.is_junction():
                    raise TeleloomError("unsafe_attachment_path", "Owned files cannot be symlinks.")
                if child.is_file():
                    total += child.stat().st_size
        return total

    def _disk_available(self, directory: Path, remaining: int) -> None:
        used = self._disk_bytes(directory.parent)
        if used + remaining + WORKER_DISK_LIMIT > DISK_LIMIT:
            raise TeleloomError(
                "attachment_disk_limit", "Local attachment storage budget exceeded."
            )
        if shutil.disk_usage(directory).free < remaining + WORKER_DISK_LIMIT + FREE_RESERVE:
            raise TeleloomError(
                "attachment_disk_limit", "Insufficient free space for bounded extraction."
            )

    def discard(self, job: dict[str, Any]) -> None:
        cleanup_error = None
        try:
            directory = self._directory(job)
            if directory.exists():
                # shutil.rmtree unlinks nested symlinks/junctions without following them.
                shutil.rmtree(directory)
        except (TeleloomError, OSError):
            cleanup_error = "An unsafe or unavailable attachment path could not be removed."
        job["result"] = {
            "items": [],
            "errors": [],
            "coverage": {"selected": len(job["payload"]["message_ids"]), "retained": False},
            "incomplete": True,
            "cleaned": True,
            "cleanup_error": cleanup_error,
        }
        with self.store.db:
            self.store.put("jobs", job)
            self.store.db.execute(
                "DELETE FROM state WHERE substr(key,1,16)='reading_results:' AND json_extract(data,'$.job_id')=?",
                (job["id"],),
            )

    async def cleanup(self, profile_id: str, job_id: str | None = None) -> dict[str, Any]:
        self.settings.profile(profile_id)
        jobs = (
            [self.jobs._owned(profile_id, job_id)]
            if job_id
            else [
                job
                for job in self.store.jobs()
                if job["profile_id"] == profile_id
                and job["kind"] in {"attachments", "transcription"}
            ]
        )
        if any(job["kind"] not in {"attachments", "transcription"} for job in jobs):
            raise TeleloomError("job_not_found", "Attachment job not found for this profile.")
        for job in jobs:
            if job["status"] in {"queued", "running", "paused"}:
                job["status"] = "cancelled"
            if job["kind"] == "transcription" and self.jobs.transcription:
                self.jobs.transcription.discard(job)
            else:
                self.discard(job)
        return {"cleaned_job_ids": [job["id"] for job in jobs]}

    async def cleanup_expired(self) -> None:
        for job in self.store.expired_jobs("attachments", utcnow().isoformat()):
            if job["status"] in {"queued", "running", "paused"}:
                job["status"] = "cancelled"
            self.discard(job)
            job["result"]["expired"] = True
            with self.store.db:
                self.store.put("jobs", job)

    async def step(self, job: dict[str, Any]) -> None:
        payload = job["payload"]
        position = payload["position"]
        if position >= len(payload["message_ids"]):
            return
        message_id = payload["message_ids"][position]
        directory = self._directory(job, create=True)
        destination = directory / f"{position:03d}.bin"
        # An interrupted, uncheckpointed download belongs only to this position.
        if destination.exists():
            if destination.is_symlink():
                raise TeleloomError("unsafe_attachment_path", "Owned files cannot be symlinks.")
            destination.unlink()
        result = job.get("result") or {
            "items": [],
            "errors": [],
            "coverage": {},
            "incomplete": False,
            "source": "bot_updates"
            if self.settings.profile(job["profile_id"]).kind == "bot"
            else "telegram",
        }
        downloaded_bytes = 0
        message: Message | None = None
        try:
            remaining = payload["max_bytes"] - payload["bytes_used"]
            characters = payload["max_characters"] - payload["characters_used"]
            if remaining <= 0 or characters <= 0:
                raise TeleloomError(
                    "attachment_budget_exhausted", "Selected attachment budget exhausted."
                )
            self._disk_available(directory, remaining)
            async with asyncio.timeout(payload["timeout_seconds"]):
                adapter = await self.adapter(job["profile_id"])
                rows = await adapter.history(
                    payload["chat_id"],
                    before=None,
                    since=None,
                    until=None,
                    limit=1,
                    ids=[int(message_id)],
                )
                message = next(
                    (
                        row
                        for row in rows
                        if row.profile_id == job["profile_id"]
                        and row.chat_id == payload["chat_id"]
                        and row.id == message_id
                        and not row.deleted
                    ),
                    None,
                )
                if not message:
                    raise TeleloomError(
                        "message_not_found", "Selected message is unavailable in this chat."
                    )
                if not message.media:
                    raise TeleloomError(
                        "attachment_not_found", "Selected message has no attachment."
                    )
                declared_size = message.media.get("size") or message.media.get("file_size")
                if isinstance(declared_size, int) and declared_size > remaining:
                    raise TeleloomError(
                        "attachment_too_large", "Declared attachment exceeds the byte budget."
                    )
                metadata = await adapter.download_attachment(
                    payload["chat_id"], int(message_id), destination, max_bytes=remaining
                )
                if destination.is_symlink() or not destination.is_file():
                    raise TeleloomError(
                        "unsafe_attachment_path", "Downloader did not produce an owned file."
                    )
                downloaded_bytes = destination.stat().st_size
                if downloaded_bytes > remaining:
                    raise TeleloomError(
                        "attachment_too_large", "Actual attachment exceeds the byte budget."
                    )
                destination.chmod(0o600)
                method = _method(destination, {**message.media, **metadata})
                capability = self.capabilities(payload["transcription_model_path"])[method]
                if not capability["available"]:
                    raise _engine_error(method)
                extracted = await self._extract(
                    destination,
                    method,
                    characters,
                    payload["timeout_seconds"],
                    payload["transcription_model_path"],
                )
                text = extracted["text"][:characters]
                parts = [
                    text[offset : offset + 32000] for offset in range(0, len(text), 32000)
                ] or [""]
                for part_index, part in enumerate(parts, start=1):
                    result["items"].append(
                        {
                            "profile_id": job["profile_id"],
                            "chat_id": payload["chat_id"],
                            "message_id": message_id,
                            "link": message.link,
                            "method": method,
                            "text": part,
                            "part_index": part_index,
                            "part_count": len(parts),
                            "bytes": downloaded_bytes,
                            "characters": len(part),
                            "truncated": bool(extracted.get("truncated"))
                            or len(extracted["text"]) > characters,
                            "untrusted": True,
                            "expires_at": payload["expires_at"],
                        }
                    )
                payload["characters_used"] += len(text)
        except asyncio.CancelledError:
            if destination.exists() and not destination.is_symlink():
                destination.unlink()
            raise
        except Exception as exc:
            error = (
                TeleloomError(
                    "attachment_timeout", "Selected attachment read exceeded its timeout."
                )
                if isinstance(exc, TimeoutError)
                else exc
                if isinstance(exc, TeleloomError)
                else telegram_error(exc)
            )
            if destination.exists() and not destination.is_symlink():
                downloaded_bytes = max(downloaded_bytes, destination.stat().st_size)
                destination.unlink()
            if error.retry_after is not None:
                attempts = payload.get("retry_attempts", 0) + 1
                payload["retry_attempts"] = attempts
                if attempts < 3:
                    payload["bytes_used"] += downloaded_bytes
                    current = self.store.get("jobs", job["id"])
                    if current and current["status"] in {"queued", "running"}:
                        with self.store.db:
                            self.store.put("jobs", job)
                    raise error from exc
            result["errors"].append(
                {
                    "chat_id": payload["chat_id"],
                    "message_id": message_id,
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                }
            )
        current = self.store.get("jobs", job["id"])
        if not current or current["status"] not in {"queued", "running"}:
            if destination.exists() and not destination.is_symlink():
                destination.unlink()
            return
        payload["bytes_used"] += downloaded_bytes
        payload["position"] += 1
        payload["retry_attempts"] = 0
        job["progress"] = payload["position"]
        job["status"] = "completed" if job["progress"] == len(payload["message_ids"]) else "queued"
        result["incomplete"] = bool(result["errors"]) or any(
            item["truncated"] for item in result["items"]
        )
        result["coverage"] = {
            "selected": len(payload["message_ids"]),
            "processed": job["progress"],
            "extracted": len({item["message_id"] for item in result["items"]}),
            "bytes_used": payload["bytes_used"],
            "characters_used": payload["characters_used"],
            "complete": job["status"] == "completed" and not result["incomplete"],
        }
        job["result"] = result
        with self.store.db:
            self.store.put("jobs", job)

    async def _extract(
        self,
        path: Path,
        method: str,
        max_characters: int,
        timeout_seconds: int,
        transcription_model_path: str | None,
    ) -> dict[str, Any]:
        if method == "audio_transcription":
            return await self._whisper(
                path, max_characters, timeout_seconds, transcription_model_path
            )
        output = path.with_suffix(".json")
        output.unlink(missing_ok=True)
        environment = {
            **os.environ,
            "TEMP": str(path.parent),
            "TMP": str(path.parent),
            "TMPDIR": str(path.parent),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "teleloom.attachments",
            str(path),
            str(output),
            method,
            str(max_characters),
            str(timeout_seconds),
            transcription_model_path or "",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment,
            start_new_session=sys.platform != "win32",
            creationflags=0x08000000 if sys.platform == "win32" else 0,
        )
        baseline = self._disk_bytes(path.parent)
        deadline = time.monotonic() + timeout_seconds
        try:
            while process.returncode is None:
                current = self.store.get("jobs", path.parent.name)
                if current and current["status"] == "cancelled":
                    raise TeleloomError("job_cancelled", "Attachment job was cancelled.")
                if time.monotonic() >= deadline:
                    raise TeleloomError(
                        "attachment_timeout", "Local extraction exceeded its timeout."
                    )
                if self._disk_bytes(path.parent) > baseline + WORKER_DISK_LIMIT:
                    raise TeleloomError(
                        "attachment_disk_limit", "Local extractor exceeded its disk budget."
                    )
                try:
                    await asyncio.wait_for(process.wait(), timeout=0.05)
                except TimeoutError:
                    continue
            if not output.is_file() or output.stat().st_size > max_characters * 6 + 4096:
                raise TeleloomError(
                    "extraction_failed", "Local extractor returned no bounded result."
                )
            result: dict[str, Any] = json.loads(output.read_text(encoding="utf-8"))
            if "error" in result:
                raise TeleloomError(**result["error"])
            return result
        finally:
            if process.returncode is None:
                await _terminate(process)
            output.unlink(missing_ok=True)
            # Tesseract temporary files are owned by this job, never retained.
            for temporary in path.parent.glob("tess_*"):
                if temporary.is_file() and not temporary.is_symlink():
                    temporary.unlink()

    async def _whisper(
        self, path: Path, characters: int, timeout: int, model: str | None
    ) -> dict[str, Any]:
        from .runtime import fingerprint

        if not model or not _local_model(model):
            raise _engine_error("audio_transcription")
        model_path = _local_model(model)
        assert model_path is not None
        revision = fingerprint(_model_revision(model_path))
        if self.whisper_process and (
            self.whisper_model != model
            or self.whisper_process.returncode is not None
            or self.whisper_revision != revision
        ):
            await self.close()
        if self.whisper_process is None:
            self.whisper_process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "teleloom.attachments",
                "--whisper-worker",
                model,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=500_000 * 6 + 8192,
                env={
                    **os.environ,
                    "HF_HUB_OFFLINE": "1",
                    "TRANSFORMERS_OFFLINE": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
                start_new_session=sys.platform != "win32",
                creationflags=0x08000000 if sys.platform == "win32" else 0,
            )
            self.whisper_model = model
            self.whisper_revision = revision
        process = self.whisper_process
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write(
            (
                json.dumps({"path": str(path), "characters": characters, "timeout": timeout}) + "\n"
            ).encode()
        )
        await process.stdin.drain()
        response = asyncio.create_task(process.stdout.readline())
        deadline = time.monotonic() + timeout
        baseline = self._disk_bytes(path.parent)
        try:
            while not response.done():
                current = self.store.get("jobs", path.parent.name)
                if current and current["status"] in {"cancelled", "paused"}:
                    raise TeleloomError(
                        "job_cancelled", "Local transcription was cancelled or paused."
                    )
                if time.monotonic() >= deadline:
                    raise TeleloomError(
                        "attachment_timeout", "Local transcription exceeded its timeout."
                    )
                if self._disk_bytes(path.parent) > baseline + WORKER_DISK_LIMIT:
                    raise TeleloomError(
                        "attachment_disk_limit", "Local transcription exceeded its disk budget."
                    )
                await asyncio.wait({response}, timeout=0.05)
            raw = response.result()
            if not raw or len(raw) > characters * 6 + 4096:
                raise TeleloomError(
                    "extraction_failed", "Local transcription returned no bounded result."
                )
            result: dict[str, Any] = json.loads(raw)
            if "error" in result:
                raise TeleloomError(**result["error"])
            return result
        except BaseException:
            await self.close()
            raise
        finally:
            response.cancel()
            with suppress(asyncio.CancelledError):
                await response


async def _terminate(process: asyncio.subprocess.Process) -> None:
    if sys.platform == "win32":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=0x08000000,
        )
        await killer.wait()
    else:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


def _method(path: Path, metadata: dict[str, Any]) -> str:
    with path.open("rb") as stream:
        head = stream.read(64)
    mime = str(metadata.get("mime_type") or "application/octet-stream").split(";", 1)[0].lower()
    detected: str | None = None
    if head.startswith(b"%PDF-"):
        detected = "pdf_text"
    elif head.startswith(b"PK\x03\x04"):
        detected = "docx_text"
    elif head.startswith(
        (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"BM", b"II*\x00", b"MM\x00*")
    ) or (head.startswith(b"RIFF") and head[8:12] == b"WEBP"):
        detected = "image_ocr"
    elif (
        head.startswith((b"OggS", b"ID3", b"fLaC"))
        or (head.startswith(b"RIFF") and head[8:12] == b"WAVE")
        or head[4:8] == b"ftyp"
        or (len(head) > 1 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0)
    ):
        detected = "audio_transcription"
    elif mime in TEXT_MIMES and b"\x00" not in head and not head.startswith((b"MZ", b"\x7fELF")):
        detected = "text_utf8"
    advertised = (
        "pdf_text"
        if mime == "application/pdf"
        else "docx_text"
        if mime == DOCX_MIME
        else "image_ocr"
        if mime.startswith("image/")
        else "audio_transcription"
        if mime.startswith("audio/")
        else "text_utf8"
        if mime in TEXT_MIMES
        else None
    )
    if detected is None or (advertised is not None and detected != advertised):
        raise TeleloomError(
            "attachment_type_mismatch",
            "Attachment bytes do not match a supported declared media type.",
        )
    if detected == "docx_text":
        try:
            with zipfile.ZipFile(path) as archive:
                if "word/document.xml" not in archive.namelist():
                    raise TeleloomError(
                        "attachment_type_mismatch", "ZIP attachment is not a DOCX document."
                    )
        except zipfile.BadZipFile as exc:
            raise TeleloomError(
                "attachment_type_mismatch", "DOCX attachment has invalid ZIP structure."
            ) from exc
    return detected


class _DocumentTreeBuilder(ElementTree.TreeBuilder):
    def doctype(self, name: str, pubid: str | None, system: str | None) -> None:
        # The parser decodes XML before this callback, including UTF-16 documents.
        raise TeleloomError("attachment_type_mismatch", "Document XML entities are unsupported.")


def _extract_sync(
    path: Path,
    method: str,
    max_characters: int,
    timeout_seconds: int,
    transcription_model_path: str | None,
) -> dict[str, Any]:
    text = ""
    incomplete = False
    try:
        if method == "text_utf8":
            with path.open("rb") as stream:
                raw = stream.read((max_characters + 1) * 4)
                incomplete = bool(stream.read(1))
            # decode up to a codepoint boundary without accepting arbitrary binary files.
            text = codecs.getincrementaldecoder("utf-8-sig")(errors="strict").decode(
                raw, final=not incomplete
            )
            if "\x00" in text:
                raise TeleloomError(
                    "attachment_type_mismatch", "Text attachment contains binary NUL bytes."
                )
        elif method == "docx_text":
            with zipfile.ZipFile(path) as archive:
                info = archive.getinfo("word/document.xml")
                if info.file_size > 20_000_000:
                    raise TeleloomError(
                        "attachment_too_large", "DOCX decompressed text exceeds 20 MB."
                    )
                document = archive.read(info)
            root = ElementTree.fromstring(
                document, parser=ElementTree.XMLParser(target=_DocumentTreeBuilder())
            )
            paragraphs = []
            count = 0
            for paragraph in root.iter(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p"
            ):
                line = "".join(
                    node.text or ""
                    for node in paragraph.iter(
                        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
                    )
                )
                paragraphs.append(line)
                count += len(line) + 1
                if count > max_characters:
                    incomplete = True
                    break
            text = "\n".join(paragraphs)
        elif method == "pdf_text":
            pypdf = importlib.import_module("pypdf")
            reader = pypdf.PdfReader(path)
            if reader.is_encrypted:
                raise TeleloomError(
                    "attachment_encrypted", "Encrypted PDF extraction is unsupported."
                )
            pieces = []
            count = 0
            for position, page in enumerate(reader.pages):
                if position >= 100:
                    incomplete = True
                    break
                piece = page.extract_text() or ""
                pieces.append(piece)
                count += len(piece) + 1
                if count > max_characters:
                    incomplete = True
                    break
            text = "\n".join(pieces)
        elif method == "image_ocr":
            pillow: Any = importlib.import_module("PIL.Image")
            pytesseract = importlib.import_module("pytesseract")
            pillow.MAX_IMAGE_PIXELS = 20_000_000
            with pillow.open(path) as image:
                if image.width * image.height > 20_000_000:
                    raise TeleloomError(
                        "attachment_too_large", "OCR image exceeds 20 million pixels."
                    )
                text = pytesseract.image_to_string(image, timeout=timeout_seconds)
        elif method == "audio_transcription":
            model_path = _local_model(transcription_model_path)
            if model_path is None:
                raise _engine_error(method)
            whisper = importlib.import_module("faster_whisper")
            global _whisper_model
            if _whisper_model is None:
                _whisper_model = whisper.WhisperModel(
                    str(model_path),
                    device="cpu",
                    compute_type="int8",
                    cpu_threads=1,
                    num_workers=1,
                    local_files_only=True,
                )
            model = _whisper_model
            segments, _ = model.transcribe(str(path), beam_size=1, vad_filter=False)
            pieces = []
            count = 0
            for segment in segments:
                pieces.append(segment.text)
                count += len(segment.text) + 1
                if count > max_characters:
                    incomplete = True
                    break
            text = " ".join(pieces).strip()
        else:
            raise TeleloomError("attachment_type_mismatch", "Unsupported extraction method.")
    except ImportError as exc:
        raise _engine_error(method) from exc
    except (UnicodeDecodeError, ElementTree.ParseError, zipfile.BadZipFile) as exc:
        raise TeleloomError(
            "attachment_type_mismatch", "Attachment contains invalid supported document data."
        ) from exc
    return {"text": text[:max_characters], "truncated": incomplete or len(text) > max_characters}


def _worker() -> None:
    if len(sys.argv) == 3 and sys.argv[1] == "--whisper-worker":
        for line in sys.stdin:
            try:
                request = json.loads(line)
                for name in ("TEMP", "TMP", "TMPDIR"):
                    os.environ[name] = str(Path(request["path"]).parent)
                result = _extract_sync(
                    Path(request["path"]),
                    "audio_transcription",
                    request["characters"],
                    request["timeout"],
                    sys.argv[2],
                )
            except Exception as exc:
                error = (
                    exc
                    if isinstance(exc, TeleloomError)
                    else TeleloomError("extraction_failed", "Local transcription failed.")
                )
                result = {
                    "error": {
                        "code": error.code,
                        "message": error.message,
                        "details": error.details,
                    }
                }
            print(json.dumps(result), flush=True)
        return
    path, output, method, characters, timeout, model = sys.argv[1:]
    try:
        result = _extract_sync(Path(path), method, int(characters), int(timeout), model or None)
    except Exception as exc:
        error = (
            exc
            if isinstance(exc, TeleloomError)
            else TeleloomError(
                "extraction_failed", "Local extraction engine could not read this attachment."
            )
        )
        result = {"error": {"code": error.code, "message": error.message, "details": error.details}}
    target = Path(output)
    target.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    target.chmod(0o600)


_whisper_model: Any = None  # Only populated inside disposable/persistent worker processes.


if __name__ == "__main__":
    _worker()

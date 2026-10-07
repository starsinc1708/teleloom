"""Explicit bounded transcription, persisted upload receipts, and untrusted cached enrichment."""

import asyncio
import importlib.metadata
import json
from contextlib import suppress
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

import httpx

from .attachments import _installed, _local_model, _method, _model_revision
from .models import Message, TeleloomError, utcnow
from .secrets import Secrets

if TYPE_CHECKING:
    from .attachments import AttachmentManager
    from .jobs import Jobs

Provider = Literal["telegram", "local", "openai", "groq"]
EXTERNAL = {"openai", "groq"}


def source_version(message: Message) -> str:
    from .runtime import fingerprint

    return fingerprint(
        [
            message.profile_id,
            message.chat_id,
            message.id,
            message.date.isoformat(),
            message.edited_at.isoformat() if message.edited_at else None,
            message.text,
            message.media,
            message.deleted,
        ]
    )


class TranscriptionManager:
    def __init__(
        self, jobs: "Jobs", attachments: "AttachmentManager", credentials: Secrets
    ) -> None:
        self.jobs, self.store = jobs, jobs.store
        self.attachments, self.credentials = attachments, credentials
        self.lock = asyncio.Lock()
        with self.store.db:
            for job in self.store.jobs():
                if (
                    job["kind"] == "transcription"
                    and job["payload"].get("receipt", {}).get("status") == "sending"
                ):
                    job["payload"]["receipt"]["status"] = "unknown"
                    job["status"] = "needs_review"
                    job["error"] = {
                        "code": "transcription_unknown",
                        "message": "External request may have incurred cost. It will not be replayed.",
                    }
                    self.store.put("jobs", job)

    def check(
        self,
        profile_id: str,
        chat_id: str,
        provider: Provider,
        upload: bool,
        *,
        execute: bool = False,
    ) -> None:
        profile = self.jobs.settings.profile(profile_id)
        profile.require_read(chat_id)
        if execute and provider in EXTERNAL and self.jobs.settings.exposure_mode == "read-only":
            raise TeleloomError(
                "tool_not_allowed", "Read-only exposure excludes external AI uploads."
            )
        if chat_id not in profile.transcription_chats:
            raise TeleloomError(
                "transcription_not_allowed",
                "Owner must allow transcription for this exact chat through CLI.",
            )
        if provider == "telegram" and profile.kind != "user":
            raise TeleloomError(
                "capability_unavailable",
                "Telegram transcribeAudio is available only to user accounts; Premium, trial and group boost restrictions apply.",
            )
        if provider in EXTERNAL and (
            not upload or chat_id not in profile.transcription_external_chats
        ):
            raise TeleloomError(
                "external_upload_not_allowed",
                "External audio upload requires owner chat opt-in and allow_external_upload=true on this call.",
            )

    def engine(self, profile_id: str, provider: Provider) -> dict[str, Any]:
        config = self.jobs.settings.profile(profile_id).transcription
        if provider == "local":
            model = _local_model(config.local_model_path)
            if not model or not _installed("faster_whisper"):
                raise TeleloomError(
                    "engine_unavailable",
                    "Install teleloom[transcription] and configure a complete existing local model; no download is performed.",
                )
            try:
                version = importlib.metadata.version("faster-whisper")
            except importlib.metadata.PackageNotFoundError:
                version = "external-fixture"
            # Model path + complete file revisions bind cache and worker identity.
            files = _model_revision(model)
            return {
                "provider": provider,
                "model": str(model),
                "revision": files,
                "engine_version": version,
            }
        if provider == "telegram":
            return {"provider": provider, "model": "telegram", "engine_version": "server-managed"}
        endpoint = (
            "https://api.groq.com/openai/v1/audio/transcriptions"
            if provider == "groq"
            else config.openai_endpoint
        )
        if not endpoint:
            raise TeleloomError(
                "engine_unavailable",
                "Owner must configure the OpenAI-compatible transcription endpoint.",
            )
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        ):
            raise TeleloomError(
                "invalid_endpoint", "Use HTTPS or an explicit loopback HTTP transcription endpoint."
            )
        if (
            parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
        ):
            raise TeleloomError(
                "invalid_endpoint",
                "Transcription endpoint cannot contain credentials, query or fragment.",
            )
        endpoint = endpoint.rstrip("/")
        if not endpoint.endswith("/audio/transcriptions"):
            endpoint += "/audio/transcriptions"
        return {
            "provider": provider,
            "model": config.groq_model if provider == "groq" else config.openai_model,
            "endpoint": endpoint,
            "engine_version": "openai-compatible-v1",
        }

    def capabilities(
        self, profile_id: str, *, include_credential_status: bool = True
    ) -> dict[str, Any]:
        profile = self.jobs.settings.profile(profile_id)
        providers = {}
        for provider in ("telegram", "local", "openai", "groq"):
            try:
                engine = self.engine(profile_id, provider)
                available = provider != "telegram" or profile.kind == "user"
                providers[provider] = {
                    "available": available,
                    "model": "configured_local_model"
                    if provider == "local" and not include_credential_status
                    else engine["model"],
                    "account_restrictions": "User account; server enforces Premium/trial quota/duration/group boosts"
                    if provider == "telegram"
                    else None,
                    "credential_configured": bool(
                        self.credentials.get(profile_id, f"{provider}_transcription_key")
                    )
                    if provider in EXTERNAL and include_credential_status
                    else None,
                    "external_upload": provider in EXTERNAL,
                }
            except TeleloomError as exc:
                providers[provider] = {
                    "available": False,
                    "reason": exc.code,
                    "installation": exc.message,
                }
        return {
            "providers": providers,
            "allowed_chat_ids": profile.transcription_chats,
            "external_allowed_chat_ids": profile.transcription_external_chats,
            "external_daily_calls": profile.transcription.external_daily_calls,
            "external_daily_bytes": profile.transcription.external_daily_bytes,
            "automatic_model_download": False,
            "automatic_transcription_on_read": False,
            "cache_only_enrichment": True,
            "max_upload_bytes": 25_000_000,
        }

    def key(self, profile_id: str, message: Message, engine: dict[str, Any]) -> str:
        from .runtime import fingerprint

        return fingerprint(
            [
                profile_id,
                self.jobs.settings.profile(profile_id).generation,
                source_version(message),
                engine,
            ]
        )

    async def selected(self, profile_id: str, chat: str, message_id: str) -> Message:
        self.jobs.settings.profile(profile_id).require_read(chat)
        rows = await (await self.jobs.adapter(profile_id)).history(
            chat, before=None, since=None, until=None, limit=1, ids=[int(message_id)]
        )
        message = next(
            (
                row
                for row in rows
                if row.profile_id == profile_id
                and row.chat_id == chat
                and row.id == message_id
                and not row.deleted
            ),
            None,
        )
        if not message:
            raise TeleloomError(
                "message_not_found", "Selected audio message is unavailable in this exact chat."
            )
        if not message.media or not str(message.media.get("mime_type", "")).startswith(
            ("audio/", "video/")
        ):
            raise TeleloomError(
                "audio_not_found", "Selected message has no audio or video attachment."
            )
        return message

    async def start(
        self,
        profile_id: str,
        chat_id: str,
        message_id: str,
        *,
        provider: Provider = "local",
        allow_external_upload: bool = False,
        max_calls: int = 1,
        max_bytes: int = 10_000_000,
        max_characters: int = 32000,
        timeout_seconds: int = 30,
        retention_hours: int = 24,
    ) -> dict[str, Any]:
        from .runtime import number

        number(chat_id)
        number(message_id, positive=True)
        if (
            provider not in {"local", "telegram", "openai", "groq"}
            or not 0 <= max_calls <= 1
            or not 1 <= max_bytes <= 25_000_000
            or not 1 <= max_characters <= 32000
            or not 1 <= timeout_seconds <= 120
            or not 1 <= retention_hours <= 168
        ):
            raise TeleloomError(
                "invalid_limit",
                "Transcription requires a supported provider, 0..1 calls, 1..25MB, 1..32000 characters, 1..120 seconds and 1..168 retention hours.",
            )
        self.check(profile_id, chat_id, provider, allow_external_upload, execute=True)
        engine = self.engine(profile_id, provider)
        # ponytail: one start lock; use keyed locks if simultaneous source reads matter.
        async with self.lock:
            message = await self.selected(profile_id, chat_id, message_id)
            key = self.key(profile_id, message, engine)
            for job in self.store.jobs():
                if (
                    job["kind"] == "transcription"
                    and job["payload"]["cache_key"] == key
                    and (
                        job["status"] in {"queued", "running", "paused", "needs_review"}
                        or job["payload"].get("receipt", {}).get("status") in {"sending", "unknown"}
                    )
                ):
                    return {
                        "job_id": job["id"],
                        "status": job["status"],
                        "existing": True,
                        "receipt": job["payload"].get("receipt"),
                    }
            cache = self.store.state(f"transcript:{key}")
            if cache and cache["expires_at"] > utcnow().timestamp():
                return {
                    "job_id": cache["job_id"],
                    "status": "completed",
                    "existing": True,
                    "cached": True,
                }
            if max_calls == 0:
                raise TeleloomError(
                    "transcription_budget_exhausted", "Cache miss and the call budget is zero."
                )
            with self.store.db:
                job = self.jobs._job(
                    profile_id,
                    "transcription",
                    {
                        "chat_id": chat_id,
                        "message_id": message_id,
                        "message_ids": [message_id],
                        "engine": engine,
                        "cache_key": key,
                        "source_version": source_version(message),
                        "allow_external_upload": allow_external_upload,
                        "max_calls": max_calls,
                        "max_bytes": max_bytes,
                        "max_characters": max_characters,
                        "timeout_seconds": timeout_seconds,
                        "expires_at": (utcnow() + timedelta(hours=retention_hours)).isoformat(),
                        "receipt": {"status": "pending", "calls": 0, "bytes": 0},
                    },
                )
            return {
                "job_id": job["id"],
                "status": job["status"],
                "existing": False,
                "cached": False,
                "results_tool": "jobs_results",
            }

    def enrich(self, profile_id: str, value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                self.enrich(profile_id, item)
            return
        if not isinstance(value, dict):
            return
        for item in value.values():
            self.enrich(profile_id, item)
        if (
            value.get("profile_id") != profile_id
            or not {"id", "chat_id", "date", "text"} <= value.keys()
        ):
            return
        profile = self.jobs.settings.profile(profile_id)
        if (
            not profile.allows_read(value["chat_id"])
            or value["chat_id"] not in profile.transcription_chats
        ):
            return
        cache = self.cached(profile_id, Message.model_validate(value))
        if cache:
            value["transcript"] = cache["item"]

    def cached(self, profile_id: str, message: Message) -> dict[str, Any] | None:
        """Current version/model-bound enrichment and its retention/job provenance; no STT."""
        profile = self.jobs.settings.profile(profile_id)
        if (
            not profile.allows_read(message.chat_id)
            or message.chat_id not in profile.transcription_chats
        ):
            return None
        for provider in ("local", "telegram", "openai", "groq"):
            if provider in EXTERNAL and message.chat_id not in profile.transcription_external_chats:
                continue
            try:
                key = self.key(profile_id, message, self.engine(profile_id, provider))
            except TeleloomError:
                continue
            cache = self.store.state(f"transcript:{key}")
            if cache and cache["expires_at"] > utcnow().timestamp():
                return dict(cache)
        return None

    def discard(self, job: dict[str, Any]) -> None:
        self.attachments.discard(job)
        self.store.db.execute(
            "DELETE FROM state WHERE key=?", (f"transcript:{job['payload']['cache_key']}",)
        )

    async def cleanup_expired(self) -> None:
        with self.store.db:
            self.store.db.execute(
                "DELETE FROM state WHERE key LIKE 'transcript:%' AND json_extract(data,'$.expires_at')<=?",
                (utcnow().timestamp(),),
            )
            for job in self.store.expired_jobs("transcription", utcnow().isoformat()):
                if job["status"] in {"queued", "running", "paused"}:
                    job["status"] = "cancelled"
                self.discard(job)

    def save(self, job: dict[str, Any]) -> None:
        with self.store.db:
            self.store.put("jobs", job)

    def check_job(self, job: dict[str, Any], *, execute: bool = False) -> None:
        p, engine = job["payload"], job["payload"]["engine"]
        self.jobs._check_account(job["profile_id"], job["account"])
        self.jobs._check_exposure(job)
        self.check(
            job["profile_id"],
            p["chat_id"],
            engine["provider"],
            p["allow_external_upload"],
            execute=execute,
        )
        if engine != self.engine(job["profile_id"], engine["provider"]):
            raise TeleloomError(
                "engine_changed",
                "Owner's model/endpoint revision changed; start a new transcription.",
            )

    def reserve(self, job: dict[str, Any], size: int) -> None:
        profile = self.jobs.settings.profile(job["profile_id"])
        key = f"transcription_usage:{job['profile_id']}:{profile.generation}:{utcnow().date()}"
        with self.store.db:
            usage = self.store.state(key, {"calls": 0, "bytes": 0})
            if (
                usage["calls"] + 1 > profile.transcription.external_daily_calls
                or usage["bytes"] + size > profile.transcription.external_daily_bytes
            ):
                raise TeleloomError(
                    "transcription_budget_exhausted",
                    "Owner's external call/byte budget is exhausted; uncertain uploads also consume budget.",
                )
            usage["calls"] += 1
            usage["bytes"] += size
            self.store.set_state(key, usage)
            job["payload"]["receipt"] = {
                "status": "sending",
                "calls": 1,
                "bytes": size,
                "started_at": utcnow().isoformat(),
                "provider": job["payload"]["engine"]["provider"],
            }
            self.store.put("jobs", job)

    async def upload(
        self, job: dict[str, Any], audio: bytes, filename: str, mime: str
    ) -> dict[str, Any]:
        p, engine = job["payload"], job["payload"]["engine"]
        credential = self.credentials.require(
            job["profile_id"], f"{engine['provider']}_transcription_key"
        )
        self.check_job(job, execute=True)
        self.reserve(job, len(audio))
        # No retries or redirects: a disconnected upload can still incur a charge.
        async with (
            httpx.AsyncClient(timeout=p["timeout_seconds"], follow_redirects=False) as http,
            http.stream(
                "POST",
                engine["endpoint"],
                headers={"Authorization": f"Bearer {credential}"},
                files={"file": (filename, audio, mime)},
                data={"model": engine["model"], "response_format": "json"},
            ) as response,
        ):
            if 400 <= response.status_code < 500 and response.status_code != 408:
                p["receipt"]["status"] = "rejected"
                self.save(job)
                raise TeleloomError(
                    "transcription_rejected",
                    "External provider rejected the request.",
                    details={"http_status": response.status_code},
                )
            if response.status_code != 200:
                raise TeleloomError(
                    "transcription_unknown",
                    "External acceptance/cost is unknown. Request will not be replayed.",
                )
            raw = bytearray()
            async for chunk in response.aiter_bytes():
                raw.extend(chunk)
                if len(raw) > p["max_characters"] * 6 + 8192:
                    raise TeleloomError(
                        "transcription_unknown",
                        "Provider accepted upload but returned an oversized result; cost may have been incurred.",
                    )
            try:
                result = json.loads(raw)
                text = result["text"]
                if not isinstance(text, str):
                    raise ValueError
            except (ValueError, KeyError, TypeError):
                raise TeleloomError(
                    "transcription_unknown",
                    "Provider accepted upload but returned invalid transcription; cost may have been incurred.",
                ) from None
            return {
                "text": text[: p["max_characters"]],
                "truncated": len(text) > p["max_characters"],
            }

    async def step(self, job: dict[str, Any]) -> None:
        p = job["payload"]
        engine = p["engine"]
        self.check_job(job)
        if p["receipt"]["status"] in {"sending", "unknown"}:
            raise TeleloomError(
                "transcription_unknown", "External request cannot be automatically retried."
            )
        job["status"] = "running"
        self.save(job)
        directory = self.attachments._directory(job, create=True)
        path = directory / "audio.bin"
        if path.exists() and not path.is_symlink():
            path.unlink()
        operation = asyncio.create_task(self._process(job, path))
        try:
            while not operation.done():
                current = self.store.get("jobs", job["id"])
                if (
                    not current or current["status"] in {"cancelled", "paused"}
                ) and not operation.cancelling():
                    operation.cancel()
                await asyncio.wait({operation}, timeout=0.05)
            extracted, message = operation.result()
            extracted["truncated"] = (
                bool(extracted.get("truncated")) or len(extracted["text"]) > p["max_characters"]
            )
            extracted["text"] = extracted["text"][: p["max_characters"]]
            current = self.store.get("jobs", job["id"])
            if not current or current["status"] in {"cancelled", "paused"}:
                return
            self.jobs._check_account(job["profile_id"], job["account"])
            self.check(
                job["profile_id"], p["chat_id"], engine["provider"], p["allow_external_upload"]
            )
            item = {
                "profile_id": job["profile_id"],
                "chat_id": p["chat_id"],
                "message_id": p["message_id"],
                "text": extracted["text"],
                "provider": engine["provider"],
                "model": engine["model"],
                "source_version": p["source_version"],
                "untrusted": True,
                "enrichment": True,
                "link": message.link,
                "truncated": bool(extracted.get("truncated")),
            }
            p["receipt"]["status"] = "completed"
            job["result"] = {
                "items": [item],
                "source": "bot_updates"
                if self.jobs.settings.profile(job["profile_id"]).kind == "bot"
                else "telegram",
                "coverage": {"complete": not item["truncated"]},
                "incomplete": item["truncated"],
                "receipt": p["receipt"],
            }
            job["status"], job["progress"] = "completed", 1
            with self.store.db:
                self.store.put("jobs", job)
                self.store.set_state(
                    f"transcript:{p['cache_key']}",
                    {
                        "item": item,
                        "job_id": job["id"],
                        "expires_at": datetime.fromisoformat(p["expires_at"]).timestamp(),
                    },
                )
        except BaseException as exc:
            operation.cancel()
            with suppress(BaseException):
                await operation
            current = self.store.get("jobs", job["id"])
            if p["receipt"]["status"] == "sending" and current:
                current["payload"]["receipt"] = {**p["receipt"], "status": "unknown"}
                if current["status"] not in {"cancelled", "paused"}:
                    current["status"] = "needs_review"
                current["error"] = {
                    "code": "transcription_unknown",
                    "message": "Upload may have incurred cost; it will not be replayed.",
                }
                self.save(current)
                if not isinstance(exc, asyncio.CancelledError):
                    return
            if isinstance(exc, asyncio.CancelledError):
                if current and current["status"] in {"cancelled", "paused"}:
                    return
                raise
            if current and current["status"] not in {"cancelled", "paused"}:
                if isinstance(exc, TimeoutError):
                    exc = TeleloomError(
                        "transcription_timeout", "Selected transcription exceeded its timeout."
                    )
                current["status"] = (
                    "paused"
                    if isinstance(exc, TeleloomError) and exc.code == "tool_not_exposed"
                    else "failed"
                )
                current["payload"]["receipt"] = p["receipt"]
                current["error"] = {
                    "code": exc.code if isinstance(exc, TeleloomError) else "transcription_failed",
                    "message": exc.message
                    if isinstance(exc, TeleloomError)
                    else "Transcription failed within its limits.",
                }
                self.save(current)
        finally:
            path.unlink(missing_ok=True)

    async def _process(self, job: dict[str, Any], path: Any) -> tuple[dict[str, Any], Message]:
        p, engine = job["payload"], job["payload"]["engine"]
        async with asyncio.timeout(p["timeout_seconds"]):
            message = await self.selected(job["profile_id"], p["chat_id"], p["message_id"])
            if source_version(message) != p["source_version"]:
                raise TeleloomError(
                    "source_changed",
                    "Source edit/media version changed since selection; start a fresh job.",
                )
            self.check_job(job, execute=True)
            adapter = await self.jobs.adapter(job["profile_id"])
            if engine["provider"] == "telegram":
                p["receipt"]["calls"] = 1
                p["receipt"]["status"] = "sending"
                self.save(job)
                try:
                    result = await adapter.transcribe_audio(p["chat_id"], int(p["message_id"]))
                except TeleloomError as exc:
                    if exc.code in {
                        "premium_or_trial_required",
                        "telegram_audio_too_long",
                        "telegram_voice_required",
                        "telegram_transcription_failed",
                    }:
                        p["receipt"]["status"] = "rejected"
                    raise
                p["receipt"].update({key: value for key, value in result.items() if key != "text"})
                return result, message
            self.attachments._disk_available(path.parent, p["max_bytes"])
            metadata = await adapter.download_attachment(
                p["chat_id"], int(p["message_id"]), path, max_bytes=p["max_bytes"]
            )
            if (
                path.is_symlink()
                or path.is_junction()
                or not path.is_file()
                or path.stat().st_size > p["max_bytes"]
            ):
                raise TeleloomError(
                    "attachment_too_large", "Audio download did not produce a bounded owned file."
                )
            path.chmod(0o600)
            if _method(path, metadata) != "audio_transcription":
                raise TeleloomError(
                    "audio_not_found", "Attachment bytes are not supported audio/video."
                )
            fresh = await self.selected(job["profile_id"], p["chat_id"], p["message_id"])
            if source_version(fresh) != p["source_version"]:
                raise TeleloomError(
                    "source_changed", "Media changed during download; no upload was made."
                )
            self.check_job(job, execute=True)
            if engine["provider"] == "local":
                return await self.attachments._extract(
                    path,
                    "audio_transcription",
                    p["max_characters"],
                    p["timeout_seconds"],
                    engine["model"],
                ), message
            mime = metadata.get("mime_type") or (message.media or {}).get("mime_type", "audio/ogg")
            suffix = {
                "audio/ogg": "ogg",
                "audio/opus": "ogg",
                "audio/mpeg": "mp3",
                "audio/wav": "wav",
                "audio/x-wav": "wav",
                "audio/flac": "flac",
                "video/mp4": "mp4",
                "audio/mp4": "m4a",
                "video/webm": "webm",
                "audio/webm": "webm",
            }.get(mime)
            if not suffix:
                raise TeleloomError(
                    "audio_format_unsupported", "Provider upload needs a supported audio format."
                )
            return await self.upload(job, path.read_bytes(), f"audio.{suffix}", mime), message

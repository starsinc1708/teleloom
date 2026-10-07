"""Owner-selected incoming events. Hosts pull durable results; no outbound callbacks."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .config import Profile
from .models import Message, TeleloomError, utcnow
from .store import Store

if TYPE_CHECKING:
    from .jobs import Jobs

MAX_RETAINED = 1000


class EventFilter(BaseModel):
    """Exact observed facts; absent criteria impose no restriction."""

    model_config = ConfigDict(extra="forbid")
    sender_id: str | None = None
    topic_id: str | None = None
    mention_user_id: str | None = None
    kinds: list[Literal["new", "edit", "delete"]] | None = Field(
        default=None, min_length=1, max_length=3
    )
    observed_since: datetime | None = None
    observed_until: datetime | None = None


def matching(item: dict[str, Any], received: float, selection: dict[str, Any]) -> list[str] | None:
    """None means known mismatch; a nonempty list identifies unavailable facts."""
    if selection.get("kinds") and item["event"] not in selection["kinds"]:
        return None
    for key, outside in (
        ("observed_since", lambda bound: received < bound),
        ("observed_until", lambda bound: received >= bound),
    ):
        if selection.get(key) and outside(datetime.fromisoformat(selection[key]).timestamp()):
            return None
    unknown = []
    for key in ("sender_id", "topic_id"):
        if selection.get(key):
            if item["event"] == "delete" or item.get(key) is None:
                unknown.append(key)
            elif item[key] != selection[key]:
                return None
    if target := selection.get("mention_user_id"):
        entities = item.get("entities")
        if item["event"] == "delete" or entities is None:
            unknown.append("mention_user_id")
        elif any(
            entity.get("user_id", entity.get("user", {}).get("id")) == target
            and (
                entity.get("type") == "text_mention"
                or entity.get("_") in {"MessageEntityMentionName", "InputMessageEntityMentionName"}
            )
            for entity in entities
        ):
            pass
        elif any(
            entity.get("type") == "mention" or entity.get("_") == "MessageEntityMention"
            for entity in entities
        ):
            unknown.append("mention_user_id")
        else:
            return None
    return unknown


def ingest(store: Store, profile_id: str, profile: Profile, message: Message, kind: str) -> None:
    from .runtime import fingerprint

    if (
        message.chat_id not in profile.event_chats
        or not profile.allows_read(message.chat_id)
        or message.outgoing
        or message.profile_id != profile_id
    ):
        return
    now = utcnow()
    item = {**message.model_dump(mode="json"), "event": kind, "untrusted": True}
    item["text"] = item["text"][:32000]
    event_key = fingerprint([message.chat_id, message.id, kind, None if kind == "delete" else item])
    with store.db:
        store.db.execute(
            "INSERT OR IGNORE INTO event_journal(profile,generation,chat,received,expires,data,event_key) VALUES(?,?,?,?,?,?,?)",
            (
                profile_id,
                profile.generation,
                message.chat_id,
                now.timestamp(),
                (now + timedelta(hours=profile.event_retention_hours)).timestamp(),
                json.dumps(item),
                event_key,
            ),
        )
        prune(store, profile_id)


def prune(store: Store, profile_id: str) -> None:
    cutoff = store.db.execute(
        "SELECT seq FROM event_journal WHERE profile=? ORDER BY seq DESC LIMIT 1 OFFSET ?",
        (profile_id, MAX_RETAINED),
    ).fetchone()
    removed = store.db.execute(
        "SELECT MAX(seq) FROM event_journal WHERE profile=? AND (expires<=? OR seq<=?)",
        (profile_id, utcnow().timestamp(), cutoff[0] if cutoff else 0),
    ).fetchone()[0]
    if removed:
        key = f"event_floor:{profile_id}"
        store.set_state(key, max(removed, store.state(key, 0)))
        store.db.execute(
            "DELETE FROM event_journal WHERE profile=? AND (expires<=? OR seq<=?)",
            (profile_id, utcnow().timestamp(), cutoff[0] if cutoff else 0),
        )


class EventManager:
    def __init__(self, jobs: "Jobs") -> None:
        self.jobs, self.store = jobs, jobs.store
        self.epoch = uuid.uuid4().hex
        self.store.db.executescript("""
            CREATE TABLE IF NOT EXISTS event_journal(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL,
              generation TEXT NOT NULL, chat TEXT NOT NULL, received REAL NOT NULL,
              expires REAL NOT NULL, data TEXT NOT NULL, event_key TEXT NOT NULL,
              UNIQUE(profile,generation,event_key));
            CREATE INDEX IF NOT EXISTS event_scope ON event_journal(profile,generation,seq);
        """)

    def check(self, job: dict[str, Any]) -> None:
        settings = self.jobs.settings
        if (
            settings.exposure_mode == "selected"
            and "events_wait_start" not in settings.exposed_tools
        ):
            raise TeleloomError(
                "tool_not_exposed", "Owner has disabled the incoming event workflow."
            )
        profile = self.jobs.settings.profile(job["profile_id"])
        for chat in job["payload"]["chat_ids"]:
            profile.require_read(chat)
            if chat not in profile.event_chats:
                raise TeleloomError(
                    "events_not_allowed", "Owner must opt into this exact chat's incoming feed."
                )
        if profile.kind == "bot" and not profile.polling:
            raise TeleloomError(
                "events_unavailable",
                "Bot incoming feed requires explicit polling; existing webhooks are never removed.",
            )

    async def start(
        self,
        profile_id: str,
        chat_ids: list[str],
        *,
        mode: Literal["new", "settled"] = "new",
        timeout_seconds: float = 30,
        debounce_seconds: float = 1,
        max_events: int = 100,
        after_sequence: int | None = None,
        retention_hours: int = 24,
        filter: EventFilter | None = None,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        from .runtime import dates, fingerprint, number

        if not chat_ids or len(chat_ids) > 50 or len(set(chat_ids)) != len(chat_ids):
            raise TeleloomError("invalid_selection", "Select 1..50 unique exact chat IDs.")
        for chat in chat_ids:
            number(chat)
        if (
            mode not in {"new", "settled"}
            or not 0.05 <= timeout_seconds <= 3600
            or not 0 <= debounce_seconds <= 60
            or not 1 <= max_events <= 100
            or not 1 <= retention_hours <= 168
            or (after_sequence is not None and after_sequence < 0)
        ):
            raise TeleloomError(
                "invalid_limit", "Invalid bounded wait, debounce, event or retention limit."
            )
        selection = filter or EventFilter()
        for key in ("sender_id", "topic_id", "mention_user_id"):
            value = getattr(selection, key)
            if value is not None:
                number(value, positive=key != "sender_id")
        start, end = dates(selection.observed_since, selection.observed_until)
        selected_filter = selection.model_dump(mode="json", exclude_none=True)
        if selection.kinds:
            selected_filter["kinds"] = sorted(set(selection.kinds))
        if start:
            selected_filter["observed_since"] = start
        if end:
            selected_filter["observed_until"] = end
        self.check({"profile_id": profile_id, "payload": {"chat_ids": chat_ids}})
        profile = self.jobs.settings.profile(profile_id)
        scope = fingerprint(
            [
                profile_id,
                self.jobs._binding(profile_id),
                sorted(chat_ids),
                profile.read_policy(),
                selected_filter,
            ]
        )
        if cursor is not None and (
            len(cursor) != 32 or any(c not in "0123456789abcdef" for c in cursor)
        ):
            raise TeleloomError(
                "invalid_cursor", "Use the opaque event cursor returned in coverage."
            )
        saved = self.store.state(f"event_cursor:{cursor}") if cursor else None
        if cursor and (
            after_sequence is not None
            or not saved
            or saved["scope"] != scope
            or saved["expires_at"] <= utcnow().timestamp()
        ):
            raise TeleloomError(
                "invalid_cursor",
                "Event cursor expired or belongs to another account, policy, selection or filter.",
            )
        last = self.store.db.execute(
            "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name='event_journal'),0)"
        ).fetchone()[0]
        payload: dict[str, Any] = {
            "chat_ids": chat_ids,
            "mode": mode,
            "timeout_seconds": timeout_seconds,
            "debounce_seconds": debounce_seconds,
            "max_events": max_events,
            "after_sequence": after_sequence if after_sequence is not None else last,
            "deadline": utcnow().timestamp() + timeout_seconds,
            "epoch": saved["epoch"] if saved else self.epoch,
            "filter": selected_filter,
            "cursor_scope": scope,
            "after_sequence_by_chat": saved["positions"]
            if saved
            else {
                chat: after_sequence if after_sequence is not None else last for chat in chat_ids
            },
            "expires_at": (utcnow() + timedelta(hours=retention_hours)).isoformat(),
        }
        await self.jobs.adapter(profile_id)
        with self.store.db:
            job = self.jobs._job(profile_id, "events", payload)
        return {
            "job_id": job["id"],
            "status": job["status"],
            "after_sequence": min(payload["after_sequence_by_chat"].values()),
            "filter": selected_filter,
            "delivery": "local_pull",
            "results_tool": "jobs_results",
        }

    async def cleanup_expired(self) -> None:
        with self.store.db:
            self.store.db.execute(
                "DELETE FROM state WHERE key LIKE 'event_cursor:%' AND json_extract(data,'$.expires_at')<=?",
                (utcnow().timestamp(),),
            )
            for row in self.store.db.execute(
                "SELECT DISTINCT profile FROM event_journal"
            ).fetchall():
                prune(self.store, row[0])
            for job in self.store.expired_jobs("events", utcnow().isoformat()):
                self.discard(job)

    def discard(self, job: dict[str, Any]) -> None:
        job["result"] = {
            "items": [],
            "coverage": {"retained": False},
            "incomplete": True,
            "cleaned": True,
        }
        if job["status"] in {"queued", "running", "paused"}:
            job["status"] = "cancelled"
        self.store.put("jobs", job)
        self.store.db.execute(
            "DELETE FROM state WHERE key LIKE 'reading_results:%' AND json_extract(data,'$.job_id')=?",
            (job["id"],),
        )

    async def step(self, job: dict[str, Any]) -> None:
        self.check(job)
        p = job["payload"]
        chats = p["chat_ids"]
        positions = p.get(
            "after_sequence_by_chat", {chat: p["after_sequence"] for chat in chats}
        ).copy()
        selection = p.get("filter", {})
        placeholders = ",".join("?" for _ in chats)
        rows = self.store.db.execute(
            f"SELECT * FROM event_journal WHERE profile=? AND generation=? AND chat IN ({placeholders}) AND seq>? AND expires>? ORDER BY seq",
            (
                job["profile_id"],
                job["account"]["generation"],
                *chats,
                min(positions.values()),
                utcnow().timestamp(),
            ),
        ).fetchall()
        candidates = [
            (row, json.loads(row["data"])) for row in rows if row["seq"] > positions[row["chat"]]
        ]
        evaluated = [
            (row, item, matching(item, row["received"], selection)) for row, item in candidates
        ]
        matches = [row for row, _, unknown in evaluated if unknown == []]
        latest = {row["chat"]: row["received"] for row in matches}
        quiet = [
            chat
            for chat, received in latest.items()
            if utcnow().timestamp() >= received + p["debounce_seconds"]
        ]
        deferred = (
            [chat for chat in latest if chat not in quiet]
            if quiet and p["mode"] == "settled"
            else []
        )
        eligible = [row for row in matches if row["chat"] not in deferred]
        reason = None
        if eligible and p["mode"] == "new":
            reason = "new"
        elif len(eligible) > p["max_events"]:
            reason = "event_budget"
        elif eligible and quiet:
            reason = "settled"
        elif utcnow().timestamp() >= p["deadline"]:
            reason = "timeout"
        if reason is None:
            job["next_run"] = min(p["deadline"], utcnow().timestamp() + 0.1)
        else:
            selected: list[dict[str, Any]] = []
            blocked: set[str] = set()
            pending_first = []
            unknown_facts: dict[str, int] = {}
            skipped_unknown = skipped_nonmatching = inspected = 0
            for row, item, unknown in evaluated:
                chat = row["chat"]
                if chat in blocked:
                    continue
                if unknown == [] and (chat in deferred or len(selected) >= p["max_events"]):
                    blocked.add(chat)
                    pending_first.append(row["seq"])
                    continue
                inspected += 1
                positions[chat] = row["seq"]
                if unknown is None:
                    skipped_nonmatching += 1
                elif unknown:
                    skipped_unknown += 1
                    for fact in unknown:
                        unknown_facts[fact] = unknown_facts.get(fact, 0) + 1
                else:
                    selected.append(
                        {
                            **item,
                            "sequence": row["seq"],
                            "observed_at": datetime.fromtimestamp(row["received"], UTC).isoformat(),
                            "publication_at": item["date"] if item["event"] != "delete" else None,
                            "unknown_facts": [
                                "sender_id",
                                "topic_id",
                                "mention_user_id",
                                "publication_at",
                                "direction",
                            ]
                            if item["event"] == "delete"
                            else [
                                key for key in ("sender_id", "topic_id") if item.get(key) is None
                            ],
                        }
                    )
            # The scalar remains conservative across independently advancing chats.
            next_sequence = min([max(positions.values()), *[seq - 1 for seq in pending_first]])
            floor = self.store.state(f"event_floor:{job['profile_id']}", 0)
            gap = self.epoch != p["epoch"] or any(
                position < floor
                for position in p.get(
                    "after_sequence_by_chat", {chat: p["after_sequence"] for chat in chats}
                ).values()
            )
            polling = self.store.state(f"polling:{job['profile_id']}", {})
            gap = gap or polling.get("status") == "error"
            cursor = uuid.uuid4().hex
            job["result"] = {
                "items": selected,
                "coverage": {
                    "reason": reason,
                    "returned": len(selected),
                    "gap": gap,
                    "polling_error": polling.get("code")
                    if polling.get("status") == "error"
                    else None,
                    "next_sequence": next_sequence,
                    "next_sequence_by_chat": positions,
                    "next_cursor": cursor,
                    "filter": selection,
                    "inspected": inspected,
                    "skipped_nonmatching": skipped_nonmatching,
                    "skipped_unknown": skipped_unknown,
                    "unknown_facts": unknown_facts,
                    "deferred_chat_ids": deferred,
                },
                "incomplete": gap
                or bool(blocked)
                or bool(skipped_unknown)
                or (reason == "timeout" and bool(matches) and p["mode"] == "settled"),
                "source": "incoming_updates",
                "delivery": "local_pull",
                "warnings": [
                    "Offline updates may be missing. Hosts must poll jobs_status/results; no callback delivery or Telegram acknowledgement."
                ],
            }
            job["progress"] = len(selected)
            job["status"] = "completed"
        with self.store.db:
            if reason is not None:
                self.store.set_state(
                    f"event_cursor:{cursor}",
                    {
                        "scope": p.get("cursor_scope"),
                        "positions": positions,
                        "epoch": self.epoch,
                        "expires_at": datetime.fromisoformat(p["expires_at"]).timestamp(),
                    },
                )
            self.store.put("jobs", job)

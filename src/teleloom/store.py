import json
import sqlite3
from pathlib import Path
from typing import Any

from .config import private_dir
from .models import Chat, Message


def literal_query(query: str) -> str:
    # Search literal terms, not arbitrary FTS syntax.
    return " AND ".join('"' + term.replace('"', '""') + '"' for term in query.split()[:32])


class Store:
    def __init__(self, directory: Path) -> None:
        private_dir(directory)
        self.db = sqlite3.connect(directory / "workspace.sqlite", timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=ON;
            CREATE TABLE IF NOT EXISTS chats (
              profile TEXT NOT NULL, id TEXT NOT NULL, data TEXT NOT NULL,
              PRIMARY KEY(profile,id));
            CREATE TABLE IF NOT EXISTS messages (
              rowid INTEGER PRIMARY KEY, profile TEXT NOT NULL, chat TEXT NOT NULL,
              id TEXT NOT NULL, date TEXT NOT NULL, text TEXT NOT NULL, data TEXT NOT NULL,
              deleted INTEGER NOT NULL DEFAULT 0, UNIQUE(profile,chat,id));
            CREATE INDEX IF NOT EXISTS message_range ON messages(profile,chat,date);
            CREATE VIRTUAL TABLE IF NOT EXISTS message_fts USING fts5(text, content=messages, content_rowid=rowid);
            CREATE TRIGGER IF NOT EXISTS message_insert AFTER INSERT ON messages BEGIN
              INSERT INTO message_fts(rowid,text) VALUES(new.rowid,new.text); END;
            CREATE TRIGGER IF NOT EXISTS message_update AFTER UPDATE ON messages BEGIN
              INSERT INTO message_fts(message_fts,rowid,text) VALUES('delete',old.rowid,old.text);
              INSERT INTO message_fts(rowid,text) VALUES(new.rowid,new.text); END;
            CREATE TRIGGER IF NOT EXISTS message_delete AFTER DELETE ON messages BEGIN
              INSERT INTO message_fts(message_fts,rowid,text) VALUES('delete',old.rowid,old.text); END;
            CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS plans (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, data TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS job_status ON jobs(json_extract(data,'$.status'));
            CREATE INDEX IF NOT EXISTS job_kind ON jobs(json_extract(data,'$.kind'));
            CREATE TABLE IF NOT EXISTS deliveries (
              job TEXT NOT NULL, position INTEGER NOT NULL, data TEXT NOT NULL,
              PRIMARY KEY(job,position));
            CREATE TABLE IF NOT EXISTS bot_pending (
              profile TEXT NOT NULL, chat TEXT NOT NULL, id TEXT NOT NULL,
              update_id INTEGER NOT NULL, PRIMARY KEY(profile,chat,id));
            PRAGMA user_version=1;
        """)

    def close(self) -> None:
        self.db.close()

    def bind_profile(self, profile: str, generation: str) -> None:
        """Retain previous account data in an inaccessible archive namespace on replacement."""
        key = f"profile_generation:{profile}"
        previous = self.state(key)
        with self.db:
            if previous and previous != generation:
                archived = f"{profile}:archived:{previous}"
                for table in ("chats", "messages", "bot_pending"):
                    self.db.execute(
                        f"UPDATE {table} SET profile=? WHERE profile=?", (archived, profile)
                    )
                for row in self.db.execute("SELECT key,data FROM state").fetchall():
                    parts = row["key"].split(":")
                    if len(parts) > 1 and parts[1] == profile:
                        parts[1] = archived
                        self.set_state(":".join(parts), json.loads(row["data"]))
                        self.db.execute("DELETE FROM state WHERE key=?", (row["key"],))
            self.set_state(key, generation)

    def state(self, key: str, default: Any = None) -> Any:
        row = self.db.execute("SELECT data FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_state(self, key: str, data: Any) -> None:
        self.db.execute(
            "INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET data=excluded.data",
            (key, json.dumps(data)),
        )

    def chat(self, profile: str, chat: Chat) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO chats VALUES(?,?,?) ON CONFLICT(profile,id) DO UPDATE SET data=excluded.data",
                (profile, chat.id, chat.model_dump_json()),
            )

    def chats(self, profile: str) -> list[Chat]:
        return [
            Chat.model_validate_json(row[0])
            for row in self.db.execute(
                "SELECT data FROM chats WHERE profile=? ORDER BY id", (profile,)
            )
        ]

    def save_messages(
        self,
        messages: list[Message],
        checkpoint: tuple[str, Any] | None = None,
        pending_update_id: int | None = None,
    ) -> None:
        with self.db:
            for message in messages:
                self.db.execute(
                    """INSERT INTO messages(profile,chat,id,date,text,data,deleted)
                  VALUES(?,?,?,?,?,?,?) ON CONFLICT(profile,chat,id) DO UPDATE SET
                  date=excluded.date,text=excluded.text,data=excluded.data,deleted=excluded.deleted""",
                    (
                        message.profile_id,
                        message.chat_id,
                        message.id,
                        message.date.isoformat(),
                        message.text,
                        message.model_dump_json(),
                        int(message.deleted),
                    ),
                )
                if pending_update_id is not None and not message.outgoing:
                    self.db.execute(
                        "INSERT INTO bot_pending VALUES(?,?,?,?) ON CONFLICT(profile,chat,id) DO UPDATE SET update_id=excluded.update_id",
                        (message.profile_id, message.chat_id, message.id, pending_update_id),
                    )
            if checkpoint:
                self.set_state(*checkpoint)

    def delete_messages(self, profile: str, chat: str | None, ids: list[str]) -> None:
        # Without a peer, Telegram message IDs may collide across channels: do not guess.
        if chat is None:
            with self.db:
                self.set_state(
                    f"gap:{profile}",
                    "A deletion update had no chat identity; index may contain stale messages.",
                )
            return
        with self.db:
            for id_ in ids:
                row = self.db.execute(
                    "SELECT data FROM messages WHERE profile=? AND chat=? AND id=?",
                    (profile, chat, id_),
                ).fetchone()
                if row:
                    message = Message.model_validate_json(row[0])
                    message.deleted = True
                    message.text = ""
                    self.db.execute(
                        "UPDATE messages SET deleted=1,text='',data=? WHERE profile=? AND chat=? AND id=?",
                        (message.model_dump_json(), profile, chat, id_),
                    )

    def messages(
        self,
        profile: str,
        chat: str,
        *,
        before: int | None = None,
        since: str | None = None,
        until: str | None = None,
        query: str | None = None,
        limit: int = 100,
        ids: list[int] | None = None,
        after: int | None = None,
        incoming_only: bool = False,
        unprocessed_only: bool = False,
    ) -> list[Message]:
        clauses = ["m.profile=?", "m.chat=?", "m.deleted=0"]
        values: list[Any] = [profile, chat]
        if after is not None:
            clauses.append("CAST(m.id AS INTEGER)>?")
            values.append(after)
        if incoming_only:
            clauses.append("json_extract(m.data,'$.outgoing')=0")
        if unprocessed_only:
            clauses.append(
                "EXISTS (SELECT 1 FROM bot_pending p WHERE p.profile=m.profile AND p.chat=m.chat AND p.id=m.id)"
            )
        if ids:
            clauses.append("CAST(m.id AS INTEGER) IN (" + ",".join("?" for _ in ids) + ")")
            values.extend(ids)
        if before is not None:
            clauses.append("CAST(m.id AS INTEGER)<?")
            values.append(before)
        for date, operator in ((since, ">="), (until, "<")):
            if date:
                clauses.append(f"m.date {operator} ?")
                values.append(date)
        join = ""
        if query:
            join = "JOIN message_fts ON message_fts.rowid=m.rowid"
            clauses.append("message_fts MATCH ?")
            values.append(literal_query(query))
        sql = f"SELECT m.data FROM messages m {join} WHERE {' AND '.join(clauses)} ORDER BY CAST(m.id AS INTEGER) DESC LIMIT ?"
        values.append(limit)
        return [Message.model_validate_json(row[0]) for row in self.db.execute(sql, values)]

    def local_search(
        self,
        profile: str,
        chats: list[str],
        query: str,
        *,
        since: str | None,
        until: str | None,
        limit: int,
    ) -> dict[str, Any]:
        clauses = [
            "m.profile=?",
            "m.deleted=0",
            "m.chat IN (" + ",".join("?" for _ in chats) + ")",
            "message_fts MATCH ?",
        ]
        values: list[Any] = [profile, *chats, literal_query(query)]
        for date, operator in ((since, ">="), (until, "<")):
            if date:
                clauses.append(f"m.date {operator} ?")
                values.append(date)
        rows = self.db.execute(
            "SELECT m.data,bm25(message_fts) AS rank,"
            "snippet(message_fts,0,'','','…',32) AS snippet "
            "FROM messages m JOIN message_fts ON message_fts.rowid=m.rowid "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY rank ASC,m.date DESC,CAST(m.chat AS INTEGER) ASC,CAST(m.id AS INTEGER) DESC LIMIT ?",
            [*values, limit],
        ).fetchall()
        coverage = self.db.execute(
            "SELECT chat,COUNT(*) AS messages,MIN(date) AS earliest,MAX(date) AS latest "
            "FROM messages WHERE profile=? AND deleted=0 "
            "AND chat IN (" + ",".join("?" for _ in chats) + ") GROUP BY chat",
            [profile, *chats],
        ).fetchall()
        return {
            "items": [
                {
                    **Message.model_validate_json(row["data"]).model_dump(mode="json"),
                    "rank": row["rank"],
                    "snippet": row["snippet"],
                }
                for row in rows
            ],
            "chats": {
                row["chat"]: {
                    "indexed_messages": row["messages"],
                    "earliest_date": row["earliest"],
                    "latest_date": row["latest"],
                }
                for row in coverage
            },
        }

    def get(self, table: str, id_: str) -> dict[str, Any] | None:
        if table not in {"jobs", "plans"}:
            raise ValueError("Unknown record table")
        row = self.db.execute(f"SELECT data FROM {table} WHERE id=?", (id_,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, table: str, record: dict[str, Any]) -> None:
        if table not in {"jobs", "plans"}:
            raise ValueError("Unknown record table")
        self.db.execute(
            f"INSERT INTO {table} VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
            (record["id"], json.dumps(record)),
        )

    def jobs(self) -> list[dict[str, Any]]:
        return [
            json.loads(row[0]) for row in self.db.execute("SELECT data FROM jobs ORDER BY rowid")
        ]

    def active_jobs(self) -> list[dict[str, Any]]:
        """Read only scheduling metadata; load each chosen job at its step boundary."""
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT id, json_extract(data,'$.kind') AS kind, "
                "json_extract(data,'$.next_run') AS next_run, "
                "json_extract(data,'$.payload.deadline_at') AS deadline_at "
                "FROM jobs WHERE json_extract(data,'$.status') IN ('queued','running') ORDER BY rowid"
            )
        ]

    def job_summaries(self, profile: str) -> list[dict[str, Any]]:
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT json_object('id',id, 'kind',json_extract(data,'$.kind'), "
                "'status',json_extract(data,'$.status'), 'progress',json_extract(data,'$.progress'), "
                "'created_at',json_extract(data,'$.created_at'), 'error',json_extract(data,'$.error')) "
                "FROM jobs WHERE json_extract(data,'$.profile_id')=? ORDER BY rowid",
                (profile,),
            )
        ]

    def expired_jobs(self, kind: str, at: str) -> list[dict[str, Any]]:
        """Only retained expiring jobs need their full record for cleanup."""
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT data FROM jobs WHERE json_extract(data,'$.kind')=? "
                "AND json_extract(data,'$.payload.expires_at')<=? "
                "AND COALESCE(json_extract(data,'$.result.cleaned'),0)=0 ORDER BY rowid",
                (kind, at),
            )
        ]

    def retained_exports(self, after: int) -> list[tuple[int, dict[str, Any]]]:
        """Bound background revalidation without decoding unrelated job originals."""
        return [
            (row[0], json.loads(row[1]))
            for row in self.db.execute(
                "SELECT rowid,data FROM jobs WHERE json_extract(data,'$.kind')='export' "
                "AND json_extract(data,'$.payload.source.kind')='frozen_evidence' "
                "AND ((json_extract(data,'$.payload.invalidated') IS NULL "
                "AND COALESCE(json_extract(data,'$.payload.pin.released'),0)=0 "
                "AND json_extract(data,'$.status')!='cancelled') "
                "OR json_array_length(json_extract(data,'$.payload.cleanup.remaining'))>0) "
                "AND rowid>? ORDER BY rowid LIMIT 8",
                (after,),
            )
        ]

    def deliveries(self, job: str) -> list[dict[str, Any]]:
        return [
            json.loads(row[0])
            for row in self.db.execute(
                "SELECT data FROM deliveries WHERE job=? ORDER BY position", (job,)
            )
        ]

    def put_delivery(self, job: str, position: int, data: dict[str, Any]) -> None:
        self.db.execute(
            "INSERT INTO deliveries VALUES(?,?,?) ON CONFLICT(job,position) DO UPDATE SET data=excluded.data",
            (job, position, json.dumps(data)),
        )

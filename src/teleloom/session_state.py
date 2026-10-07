"""Telethon's memory session with only peer/update metadata persisted locally."""

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout
from platformdirs import user_data_path
from telethon import types, utils
from telethon.sessions import StringSession

from .config import private_dir
from .models import TeleloomError
from .store import Store


def lock_directory() -> Path:
    # Independent of TELELOOM_DATA_DIR: the same OS credential may serve two installations.
    return user_data_path("teleloom", appauthor=False) / "session-locks"


def acquire_session(session: Any) -> FileLock:
    if not session.auth_key:
        raise TeleloomError("auth_required", "Authenticate the saved profile using the owner CLI.")
    directory = lock_directory()
    private_dir(directory)
    identity = hashlib.sha256(session.auth_key.key).hexdigest()
    lock = FileLock(directory / (identity + ".lock"))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        raise TeleloomError(
            "session_in_use", "This Telegram session already has a connection owner."
        ) from None
    return lock


class SavedMetadataSession(StringSession):
    """Auth keys remain in memory/keyring; native Telethon resolves restored InputPeers."""

    def __init__(self, value: str, store: Store, profile: str, generation: str) -> None:
        super().__init__(value)
        self.store = store
        key_hash = hashlib.sha256(self.auth_key.key).hexdigest() if self.auth_key else "new"
        self.key = f"session_metadata:{profile}:{generation}:{key_hash}"
        self.metadata = store.state(self.key, {"entities": {}, "updates": {}})
        for marked, access_hash in self.metadata["entities"].items():
            id_, peer = utils.resolve_id(int(marked))
            entity = (
                types.InputPeerUser(id_, access_hash)
                if peer is types.PeerUser
                else types.InputPeerChannel(id_, access_hash)
                if peer is types.PeerChannel
                else types.InputPeerChat(id_)
            )
            super().process_entities([entity])
        for id_, state in self.metadata["updates"].items():
            super().set_update_state(
                int(id_),
                types.updates.State(
                    pts=state["pts"],
                    qts=state["qts"],
                    date=datetime.fromisoformat(state["date"]),
                    seq=state["seq"],
                    unread_count=state["unread_count"],
                ),
            )

    def process_entities(self, tlo: Any) -> None:
        super().process_entities(tlo)
        entities = (
            list(tlo)
            if utils.is_list_like(tlo)
            else [tlo, *getattr(tlo, "users", []), *getattr(tlo, "chats", [])]
        )
        for entity in entities:
            try:
                peer = utils.get_input_peer(entity, allow_self=False)
                marked = utils.get_peer_id(peer)
            except TypeError:
                continue
            row = self.get_entity_rows_by_id(marked)
            if row:
                self.metadata["entities"][str(row[0])] = row[1]
        # ponytail: retain 10000 recent peer/update identities; use LRU if large accounts need more.
        self.metadata["entities"] = dict(list(self.metadata["entities"].items())[-10000:])
        self._persist()

    def set_update_state(self, entity_id: int, state: Any) -> None:
        super().set_update_state(entity_id, state)
        self.metadata["updates"][str(entity_id)] = {
            "pts": state.pts,
            "qts": state.qts,
            "date": state.date.isoformat(),
            "seq": state.seq,
            "unread_count": state.unread_count,
        }
        self.metadata["updates"] = dict(list(self.metadata["updates"].items())[-10000:])
        self._persist()

    def _persist(self) -> None:
        with self.store.db:
            self.store.set_state(self.key, self.metadata)

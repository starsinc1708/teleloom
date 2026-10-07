from typing import Any

from .models import Chat


def membership(
    folder: dict[str, Any], chats: list[Chat]
) -> tuple[list[Chat], list[dict[str, Any]]]:
    """Evaluate the owner's filter, including explicit exceptions and unknown state."""
    pins = folder.get("pinned_chat_ids", [])
    explicit = set(folder.get("included_chat_ids", [])) | set(pins)
    excluded = set(folder.get("excluded_chat_ids", []))
    rules = {} if folder.get("shared") else folder.get("rules", {})
    available = {chat.id: chat for chat in chats}
    unavailable = [
        {
            "chat_id": id_,
            "error": {
                "code": "peer_unavailable",
                "message": "Explicit member is absent from the accessible dialog snapshot.",
            },
        }
        for id_ in sorted(explicit - excluded - available.keys(), key=int)
    ]
    members = []
    for chat in available.values():
        if chat.id in excluded:
            continue
        if chat.id in explicit:
            members.append(chat)
            continue
        unknown = False
        if chat.kind == "channel":
            matches = bool(rules.get("broadcasts"))
        elif chat.kind in {"group", "supergroup"}:
            matches = bool(rules.get("groups"))
        elif chat.kind in {"private", "user", "bot"}:
            if chat.is_bot is None:
                unknown = any(rules.get(k) for k in ("bots", "contacts", "non_contacts"))
                matches = unknown
            elif chat.is_bot:
                matches = bool(rules.get("bots"))
            elif chat.is_contact is None:
                unknown = bool(rules.get("contacts") or rules.get("non_contacts"))
                matches = unknown
            else:
                matches = bool(rules.get("contacts" if chat.is_contact else "non_contacts"))
        else:
            matches = False
        if not matches:
            continue
        mention = chat.unread_mentions_count > 0
        if rules.get("exclude_archived"):
            if chat.archived is True:
                continue
            unknown |= chat.archived is None
        if rules.get("exclude_read") and not (chat.unread_count or chat.unread_mark or mention):
            continue
        if rules.get("exclude_muted"):
            if chat.muted is True and not (mention and chat.archived is False):
                if not mention or chat.archived is True:
                    continue
                unknown = True
            unknown |= chat.muted is None
        if unknown:
            unavailable.append(
                {
                    "chat_id": chat.id,
                    "error": {
                        "code": "membership_unknown",
                        "message": "Dialog state required to evaluate the filter is unavailable.",
                    },
                }
            )
        else:
            members.append(chat)
    rank = {id_: i for i, id_ in enumerate(pins)}
    members.sort(key=lambda c: (rank.get(c.id, len(rank)), int(c.id)))
    return members, unavailable

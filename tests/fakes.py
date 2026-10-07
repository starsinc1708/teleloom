from datetime import UTC, datetime, timedelta

from teleloom.models import Chat, Message, TeleloomError


class TelegramAPI:
    """External Telegram API fake; storage/queue/transport remain real."""

    def __init__(self, profile, config, store, credentials):
        self.profile = profile
        self.config = config
        self.store = store
        self.sent = []
        self.ack = []
        self.send_error = None
        now = datetime.now(UTC)
        self.rows = [
            Message(
                profile_id=profile,
                chat_id="100",
                id=str(i),
                date=now - timedelta(minutes=6 - i),
                text=f"Decision {i}",
            )
            for i in range(1, 6)
        ]

    async def start(self):
        pass

    async def close(self):
        pass

    async def folders(self):
        return []

    async def chats(self):
        return [
            Chat(
                id="100",
                title="Engineering",
                unread_count=3,
                read_inbox_max_id="2",
                top_message_id="5",
            )
        ]

    async def resolve(self, target):
        if target in {"100", "Engineering"}:
            return (await self.chats())[0]
        if target == "200":
            return Chat(id="200", title="Other")
        raise TeleloomError("chat_not_found", "Unknown target")

    async def history(self, chat, *, before, since, until, limit, query=None, ids=None):
        rows = [
            row
            for row in self.rows
            if row.chat_id == chat
            and (before is None or int(row.id) < before)
            and (since is None or row.date >= since)
            and (until is None or row.date < until)
            and (not query or query.lower() in row.text.lower())
            and (not ids or int(row.id) in ids)
        ]
        return sorted(rows, key=lambda row: int(row.id), reverse=True)[:limit]

    async def acknowledge(self, chat, through, update_watermark=None):
        self.ack.append((chat, through))

    async def send(self, chat, text, reply_to, random_id):
        self.sent.append((chat, text, reply_to, random_id))
        if self.send_error:
            raise self.send_error
        return str(len(self.sent) + 100)


def data(result):
    if hasattr(result, "structuredContent"):
        return result.structuredContent
    return result[1]

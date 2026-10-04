"""Local cache of the messages in Clangy's channels, so a mention's context doesn't wait for Discord."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from clangy.messages import Attachment, IncomingMessage

VERSION = 1
SCHEMA = """
create table if not exists messages (
    id integer primary key,
    channel_id integer not null,
    parent_id integer,
    guild_id integer not null,
    author text not null,
    text text not null,
    created text not null,
    attachments text not null,
    reply_to integer
);
create index if not exists messages_channel on messages (channel_id, created);
"""


class MessageCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        if self.db.execute("pragma user_version").fetchone()[0] != VERSION:
            # Only a cache: a new schema starts it over, and catching up fills it again
            self.db.executescript(f"drop table if exists messages; pragma user_version = {VERSION};")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add(self, msg: IncomingMessage) -> None:
        """Stores a message and the one it replies to; a reply never overwrites what the cache has of the latter."""
        with self.db:
            self.db.execute("insert or replace into messages values (?, ?, ?, ?, ?, ?, ?, ?, ?)", _row(msg))
            if msg.replied_to and not msg.replied_to.from_self:
                self.db.execute(
                    "insert or ignore into messages values (?, ?, ?, ?, ?, ?, ?, ?, ?)", _row(msg.replied_to)
                )

    def has(self, message_id: int) -> bool:
        return self.db.execute("select 1 from messages where id = ?", (message_id,)).fetchone() is not None

    def edit(self, message_id: int, text: str) -> None:
        with self.db:
            self.db.execute("update messages set text = ? where id = ?", (text, message_id))

    def delete(self, message_id: int) -> None:
        with self.db:
            self.db.execute("delete from messages where id = ?", (message_id,))

    def get(self, message_id: int) -> IncomingMessage | None:
        row = self.db.execute("select * from messages where id = ?", (message_id,)).fetchone()
        return _message(row) if row else None

    def before(
        self, channel_id: int, before_id: int | None, after: datetime | None, limit: int | None
    ) -> list[IncomingMessage]:
        """The newest `limit` (None: all) messages of a channel after a time (None: from its start) and before a
        message, oldest first, with the messages they reply to."""
        rows = self.db.execute(
            "select * from messages where channel_id = ? and created >= ? and id < ? order by created desc limit ?",
            (channel_id, _utc(after) if after else "", before_id or 2**63 - 1, -1 if limit is None else limit),
        ).fetchall()
        messages = []
        for row in reversed(rows):
            msg = _message(row)
            msg.replied_to = self.get(row[-1]) if row[-1] else None
            messages.append(msg)
        return messages

    def prune(self, older_than: datetime) -> None:
        with self.db:
            self.db.execute("delete from messages where created < ?", (_utc(older_than),))


def _utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def _row(msg: IncomingMessage) -> tuple:
    attachments = json.dumps([vars(a) for a in msg.attachments])
    reply_to = msg.replied_to.id if msg.replied_to else None
    return (
        msg.id,
        msg.channel_id,
        msg.parent_id,
        msg.guild_id,
        msg.author,
        msg.text,
        _utc(msg.created),
        attachments,
        reply_to,
    )


def _message(row: tuple) -> IncomingMessage:
    """The message without the one it replies to."""
    message_id, channel_id, parent_id, guild_id, author, text, created, attachments, _ = row
    return IncomingMessage(
        id=message_id,
        channel_id=channel_id,
        parent_id=parent_id,
        guild_id=guild_id,
        author=author,
        text=text,
        created=datetime.fromisoformat(created),
        attachments=[Attachment(**a) for a in json.loads(attachments)],
    )

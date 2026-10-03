"""Local cache of the messages in Clangy's channels, so a mention's context doesn't wait for Discord."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from clangy.messages import Attachment, IncomingMessage

SCHEMA = """
create table if not exists messages (
    id integer primary key,
    channel_id integer not null,
    parent_id integer,
    guild_id integer not null,
    author text not null,
    text text not null,
    created text not null,
    attachments text not null
);
create index if not exists messages_channel on messages (channel_id, created);
"""


class MessageCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add(self, msg: IncomingMessage) -> None:
        attachments = json.dumps([vars(a) for a in msg.attachments])
        with self.db:
            self.db.execute(
                "insert or replace into messages values (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    msg.id,
                    msg.channel_id,
                    msg.parent_id,
                    msg.guild_id,
                    msg.author,
                    msg.text,
                    _utc(msg.created),
                    attachments,
                ),
            )

    def has(self, message_id: int) -> bool:
        return self.db.execute("select 1 from messages where id = ?", (message_id,)).fetchone() is not None

    def edit(self, message_id: int, text: str) -> None:
        with self.db:
            self.db.execute("update messages set text = ? where id = ?", (text, message_id))

    def delete(self, message_id: int) -> None:
        with self.db:
            self.db.execute("delete from messages where id = ?", (message_id,))

    def before(self, channel_id: int, before_id: int | None, after: datetime, limit: int) -> list[IncomingMessage]:
        """The newest `limit` messages of a channel after a time and before a message, oldest first."""
        rows = self.db.execute(
            "select * from messages where channel_id = ? and created >= ? and id < ? order by created desc limit ?",
            (channel_id, _utc(after), before_id or 2**63 - 1, limit),
        ).fetchall()
        return [_message(row) for row in reversed(rows)]

    def prune(self, older_than: datetime) -> None:
        with self.db:
            self.db.execute("delete from messages where created < ?", (_utc(older_than),))


def _utc(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat()


def _message(row: tuple) -> IncomingMessage:
    message_id, channel_id, parent_id, guild_id, author, text, created, attachments = row
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

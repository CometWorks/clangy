import sqlite3
from datetime import datetime, timedelta, timezone

from clangy.cache import MessageCache
from clangy.messages import Attachment, IncomingMessage

T0 = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def msg(message_id: int, minutes: int, text: str = "hi", channel_id: int = 1) -> IncomingMessage:
    return IncomingMessage(
        id=message_id, channel_id=channel_id, parent_id=None, guild_id=9, author="Alice", text=text,
        created=T0 + timedelta(minutes=minutes), attachments=[Attachment(5, "a.log", 3, "https://cdn/a.log")],
    )  # fmt: skip


def test_cache_round_trip_edit_delete_and_window(tmp_path):
    cache = MessageCache(tmp_path / "m.sqlite")
    for i in range(5):
        cache.add(msg(100 + i, i, f"m{i}"))
    cache.add(msg(200, 3, "other channel", channel_id=2))
    cache.edit(101, "edited")
    cache.delete(102)
    found = cache.before(1, 104, T0 + timedelta(minutes=1), limit=10)
    assert [m.text for m in found] == ["edited", "m3"]
    assert found[0].attachments == [Attachment(5, "a.log", 3, "https://cdn/a.log")]
    assert found[0].created == T0 + timedelta(minutes=1) and found[0].guild_id == 9
    assert [m.id for m in cache.before(1, None, T0, limit=2)] == [103, 104], "the newest, oldest first"
    assert cache.has(100) and not cache.has(102)
    cache.prune(T0 + timedelta(minutes=2))
    assert not cache.has(101) and cache.has(103)
    cache.close()
    assert MessageCache(tmp_path / "m.sqlite").has(104), "persisted"


def test_replies_keep_the_message_they_reply_to(tmp_path):
    cache = MessageCache(tmp_path / "m.sqlite")
    cache.add(msg(100, 0, "cached"))
    reply = msg(101, 5, "reply")
    reply.replied_to = msg(100, 0, "stale copy sent with the reply")
    cache.add(reply)
    old_reply = msg(102, 6, "reply to an old one")
    old_reply.replied_to = msg(50, -600, "old")
    cache.add(old_reply)
    own_reply = msg(103, 7, "reply to Clangy")
    own_reply.replied_to = msg(51, 1, "Clangy's answer")
    own_reply.replied_to.from_self = True
    cache.add(own_reply)
    found = cache.before(1, None, T0 + timedelta(minutes=1), limit=None)
    assert [(m.text, m.replied_to and m.replied_to.text) for m in found] == [
        ("reply", "cached"),
        ("reply to an old one", "old"),
        ("reply to Clangy", None),
    ]
    assert [m.id for m in cache.before(1, None, None, limit=None)] == [50, 100, 101, 102, 103]


def test_old_schema_is_dropped(tmp_path):
    path = tmp_path / "m.sqlite"
    old = sqlite3.connect(path)
    old.execute("create table messages (id integer primary key, channel_id integer, text text)")
    old.execute("insert into messages values (1, 1, 'old')")
    old.commit()
    old.close()
    cache = MessageCache(path)
    assert not cache.has(1)
    cache.add(msg(100, 0))
    assert cache.get(100) is not None

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

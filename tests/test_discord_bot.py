from datetime import datetime, timezone
from unittest.mock import MagicMock

import discord

from clangy.discord_bot import incoming


def make_message(channel, author_id: int = 5, mentions=()) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.id = 42
    message.channel = channel
    message.guild.id = 1
    message.author.id = author_id
    message.author.display_name = "Alice"
    message.clean_content = "@Clangy help"
    message.created_at = datetime(2026, 10, 3, tzinfo=timezone.utc)
    message.mentions = list(mentions)
    attachment = MagicMock(id=7, filename="info.log", size=10, url="https://cdn/info.log")
    message.attachments = [attachment]
    message.type = discord.MessageType.default
    message.reference = None
    return message


def test_forum_starter_message():
    bot = MagicMock(spec=discord.ClientUser, id=99)
    thread = MagicMock(spec=discord.Thread, id=42, parent_id=10)
    thread.name = "Crash"
    msg = incoming(make_message(thread, mentions=[bot]), bot)
    assert (msg.channel_id, msg.parent_id, msg.starts_thread, msg.thread_name) == (42, 10, True, "Crash")
    assert msg.mentions_bot and not msg.from_self
    assert msg.attachments[0].filename == "info.log"


def test_channel_message_from_self():
    bot = MagicMock(spec=discord.ClientUser, id=5)
    channel = MagicMock(spec=discord.TextChannel, id=40)
    msg = incoming(make_message(channel), bot)
    assert (msg.channel_id, msg.parent_id, msg.starts_thread) == (40, None, False)
    assert msg.from_self and not msg.mentions_bot


def test_replies_and_thread_starters_carry_the_replied_message():
    bot = MagicMock(spec=discord.ClientUser, id=99)
    channel = MagicMock(spec=discord.TextChannel, id=40)
    for kind in (discord.MessageType.reply, discord.MessageType.thread_starter_message):
        message = make_message(channel)
        message.type = kind
        message.reference = MagicMock(resolved=make_message(channel, author_id=6))
        message.reference.resolved.id = 41
        assert incoming(message, bot).replied_to.id == 41
    deleted = make_message(channel)
    deleted.type = discord.MessageType.reply
    deleted.reference = MagicMock(resolved=MagicMock(spec=discord.DeletedReferencedMessage))
    assert incoming(deleted, bot).replied_to is None
    assert incoming(make_message(channel), bot).replied_to is None


async def test_edit_events_skip_own_and_unknown_messages():
    from clangy.discord_bot import ClangyClient

    client = MagicMock(spec=ClangyClient)
    client.user = MagicMock(spec=discord.ClientUser, id=99)
    client.core = MagicMock()
    client.core.knows.side_effect = lambda message_id: message_id == 42
    thread = MagicMock(spec=discord.Thread, id=42, parent_id=10)
    thread.name = "Crash"

    def payload(message_id: int, author_id: int, data_extra: dict) -> MagicMock:
        event = MagicMock(spec=discord.RawMessageUpdateEvent, message_id=message_id)
        event.data = {"author": {"id": str(author_id)}, **data_extra}
        event.message = make_message(thread, author_id=author_id)
        return event

    for event in (payload(42, 99, {"content": "x"}), payload(7, 5, {"content": "x"}), payload(42, 5, {})):
        await ClangyClient.on_raw_message_edit(client, event)
    client.core.on_edit.assert_not_called()
    await ClangyClient.on_raw_message_edit(client, payload(42, 5, {"content": "@Clangy help"}))
    client.core.on_edit.assert_called_once()

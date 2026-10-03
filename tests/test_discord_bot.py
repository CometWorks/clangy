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

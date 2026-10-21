from datetime import datetime, timezone
from unittest.mock import MagicMock

import discord

from clangy.discord_bot import emoji_name, incoming


def make_message(channel, author_id: int = 5, mentions=(), role_mentions=()) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.id = 42
    message.channel = channel
    message.guild.id = 1
    message.author.id = author_id
    message.author.display_name = "Alice"
    message.clean_content = "@Clangy help"
    message.created_at = datetime(2026, 10, 3, tzinfo=timezone.utc)
    message.mentions = list(mentions)
    message.guild.self_role.id = 77
    message.raw_role_mentions = list(role_mentions)
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


def test_forum_tags_reactions_and_custom_emojis():
    bot = MagicMock(spec=discord.ClientUser, id=99)
    thread = MagicMock(spec=discord.Thread, id=42, parent_id=10)
    thread.applied_tags = [MagicMock(spec=discord.ForumTag), MagicMock(spec=discord.ForumTag)]
    thread.applied_tags[0].name, thread.applied_tags[1].name = "Pulsar", "Magnetar"
    message = make_message(thread)
    message.clean_content = "Thanks <:pepe_thanks:123> <a:spin:456> :)"
    custom = discord.PartialEmoji(name="pepe_thanks", id=123)
    message.reactions = [MagicMock(emoji="🙏", count=1), MagicMock(emoji=custom, count=2)]
    msg = incoming(message, bot)
    assert msg.thread_tags == ["Pulsar", "Magnetar"]
    assert msg.text == "Thanks :pepe_thanks: :spin: :)"
    assert msg.reactions == {"🙏": 1, ":pepe_thanks:": 2}
    assert emoji_name(discord.PartialEmoji(name="🙏")) == "🙏"


async def test_reaction_events_reach_the_core():
    from clangy.discord_bot import ClangyClient

    client = MagicMock(spec=ClangyClient)
    client.core = MagicMock()
    event = MagicMock(spec=discord.RawReactionActionEvent, message_id=42, emoji=discord.PartialEmoji(name="👍"))
    event.member.display_name = "Alice"
    await ClangyClient.on_raw_reaction_add(client, event)
    event.member = None
    await ClangyClient.on_raw_reaction_remove(client, event)
    assert client.core.on_reaction.call_args_list == [((42, "👍", 1, "Alice"),), ((42, "👍", -1, ""),)]


def test_a_ping_of_the_bots_role_mentions_the_bot():
    bot = MagicMock(spec=discord.ClientUser, id=99)
    channel = MagicMock(spec=discord.TextChannel, id=40)
    assert incoming(make_message(channel, role_mentions=[77]), bot).mentions_bot
    assert not incoming(make_message(channel, role_mentions=[78]), bot).mentions_bot


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


def chat_with(thread: MagicMock, parent: MagicMock):
    from clangy.discord_bot import DiscordChat

    thread.parent = parent
    for channel in (thread, parent):
        if not isinstance(channel, discord.ForumChannel):
            channel.get_partial_message.return_value = MagicMock(spec=discord.PartialMessage)
    client = MagicMock()
    client.get_channel.side_effect = {thread.id: thread, parent.id: parent}.get
    return DiscordChat(client, MagicMock(), 0)


async def test_reaction_on_a_forum_post_reopens_it_and_tags_it():
    forum = MagicMock(spec=discord.ForumChannel, id=10)
    tags = [MagicMock(spec=discord.ForumTag), MagicMock(spec=discord.ForumTag)]
    tags[0].name, tags[1].name = "SE1", "Completed"
    forum.available_tags = tags
    thread = MagicMock(spec=discord.Thread, id=42, parent_id=10, archived=True, applied_tags=[tags[0]])
    chat = chat_with(thread, forum)
    assert await chat.react_to_start(42, "✅") == 42
    thread.edit.assert_awaited_once_with(archived=False)
    thread.get_partial_message.assert_called_once_with(42)
    thread.get_partial_message.return_value.add_reaction.assert_awaited_once_with("✅")
    assert await chat.add_tag(42, ("completed", "Done")) == "Completed"
    thread.add_tags.assert_awaited_once_with(tags[1])
    assert await chat.add_tag(42, ("Fixed",)) is None


async def test_reaction_on_a_thread_from_a_channel_message():
    channel = MagicMock(spec=discord.TextChannel, id=10)
    thread = MagicMock(spec=discord.Thread, id=42, parent_id=10, archived=True)
    chat = chat_with(thread, channel)
    assert await chat.react_to_start(42, "✅") == 10
    channel.get_partial_message.return_value.add_reaction.assert_awaited_once_with("✅")
    thread.edit.assert_not_called()
    assert await chat.add_tag(42, ("Completed",)) is None

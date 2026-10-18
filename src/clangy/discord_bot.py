"""discord.py adapter: turns gateway events into core messages and implements the Chat protocol."""

import logging
import re
from datetime import datetime
from pathlib import Path

import aiohttp
import discord

from clangy.config import Config
from clangy.core import Attachment, Clangy, HistoryUnavailable, IncomingMessage

log = logging.getLogger(__name__)

NO_PINGS = discord.AllowedMentions.none()


class DiscordChat:
    def __init__(self, client: discord.Client, http: aiohttp.ClientSession, max_bytes: int):
        self.client = client
        self.http = http
        self.max_bytes = max_bytes

    @property
    def bot_name(self) -> str:
        assert self.client.user is not None
        return self.client.user.display_name

    async def _channel(self, channel_id: int) -> discord.abc.Messageable:
        channel = self.client.get_channel(channel_id) or await self.client.fetch_channel(channel_id)
        assert isinstance(channel, discord.abc.Messageable)
        return channel

    async def send(
        self, channel_id: int, content: str, files: list[Path] | None = None, ping: int | None = None
    ) -> int:
        channel = await self._channel(channel_id)
        uploads = [discord.File(f) for f in files or []]
        mentions = NO_PINGS
        if ping is not None:
            mentions = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=ping)])
        message = await channel.send(content, files=uploads, allowed_mentions=mentions)
        return message.id

    async def edit(self, channel_id: int, message_id: int, content: str, files: list[Path] | None = None) -> None:
        channel = await self._channel(channel_id)
        assert isinstance(channel, (discord.TextChannel, discord.Thread))
        message = channel.get_partial_message(message_id)
        if files:
            await message.edit(content=content, attachments=[discord.File(f) for f in files], allowed_mentions=NO_PINGS)
        else:
            await message.edit(content=content, allowed_mentions=NO_PINGS)

    async def create_thread(self, channel_id: int, message_id: int, name: str) -> int:
        channel = await self._channel(channel_id)
        assert isinstance(channel, discord.TextChannel)
        thread = await channel.get_partial_message(message_id).create_thread(name=name[:100])
        return thread.id

    async def history(
        self, channel_id: int, before_id: int | None, limit: int | None, after: datetime | None
    ) -> list[IncomingMessage]:
        channel = await self._channel(channel_id)
        assert self.client.user is not None
        before = discord.Object(before_id) if before_id else None
        newest_first = channel.history(limit=limit, before=before, after=after, oldest_first=False)
        try:
            return [incoming(m, self.client.user) async for m in newest_first][::-1]
        except discord.HTTPException as e:
            raise HistoryUnavailable(f"can't read the history of channel {channel_id}: {e}") from e

    async def thread_parent(self, thread_id: int) -> int | None:
        channel = self.client.get_channel(thread_id) or await self.client.fetch_channel(thread_id)
        return channel.parent_id if isinstance(channel, discord.Thread) else None

    async def download(self, attachment: Attachment, path: Path) -> None:
        size = 0
        async with self.http.get(attachment.url) as response:
            response.raise_for_status()
            with open(path, "wb") as f:
                async for chunk in response.content.iter_chunked(1 << 16):
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise ValueError(f"attachment {attachment.filename} is over the size limit")
                    f.write(chunk)


REPLY_TYPES = (discord.MessageType.reply, discord.MessageType.thread_starter_message)
CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")


def emoji_name(emoji: discord.PartialEmoji | discord.Emoji | str) -> str:
    """A unicode emoji as it is, a custom one as `:name:`."""
    if isinstance(emoji, str):
        return emoji
    return f":{emoji.name}:" if emoji.id else emoji.name


def incoming(message: discord.Message, bot: discord.ClientUser) -> IncomingMessage:
    channel = message.channel
    # Discord sends the replied message along, one level deep. A thread started from a channel message opens
    # with an empty message of its own that points to it.
    replied = message.reference.resolved if message.type in REPLY_TYPES and message.reference else None
    in_thread = isinstance(channel, discord.Thread)
    # Discord's autocomplete offers the bot's managed role under the bot's name, so a ping of it counts too
    role = message.guild.self_role if message.guild else None
    return IncomingMessage(
        id=message.id,
        channel_id=channel.id,
        parent_id=channel.parent_id if in_thread else None,
        guild_id=message.guild.id if message.guild else 0,
        author=message.author.display_name,
        # clean_content leaves custom emojis as <:name:id>
        text=CUSTOM_EMOJI.sub(r":\1:", message.clean_content),
        created=message.created_at,
        mentions_bot=bot in message.mentions or role is not None and role.id in message.raw_role_mentions,
        from_self=message.author.id == bot.id,
        # The starter message of a forum post has the same id as the thread
        starts_thread=in_thread and message.id == channel.id,
        thread_name=channel.name if in_thread else "",
        thread_tags=[t.name for t in channel.applied_tags] if in_thread else [],
        attachments=[Attachment(a.id, a.filename, a.size, a.url) for a in message.attachments],
        reactions={emoji_name(r.emoji): r.count for r in message.reactions},
        replied_to=incoming(replied, bot) if isinstance(replied, discord.Message) else None,
    )


class ClangyClient(discord.Client):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(intents=intents, allowed_mentions=NO_PINGS)
        self.config = config
        self.core: Clangy | None = None

    async def setup_hook(self) -> None:
        http = aiohttp.ClientSession()
        self.core = Clangy(self.config, DiscordChat(self, http, self.config.attachment_max_bytes))
        await self.core.load()
        self.loop.create_task(self.core.monitor_backends())
        self.loop.create_task(self.core.watch_outbox())
        self.loop.create_task(self.core.watch_tracker())

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)
        assert self.core is not None
        # Also after reconnects, which can lose gateway events
        await self.core.catch_up()

    async def on_message(self, message: discord.Message) -> None:
        assert self.core is not None and self.user is not None
        try:
            await self.core.on_message(incoming(message, self.user))
        except Exception:
            # One bad message must not take the gateway handler down
            log.exception("Failed to handle message %s", message.id)

    async def on_raw_message_edit(self, payload: discord.RawMessageUpdateEvent) -> None:
        assert self.core is not None and self.user is not None
        # Clangy edits its progress messages every minute; skip those and anything outside its threads
        author = payload.data.get("author", {}).get("id")
        if not self.core.knows(payload.message_id) or author is None or int(author) == self.user.id:
            return
        if "content" not in payload.data:
            return  # embeds being added, not an edit by the user
        self.core.on_edit(incoming(payload.message, self.user))

    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        assert self.core is not None
        user = payload.member.display_name if payload.member else "Someone"
        self.core.on_reaction(payload.message_id, emoji_name(payload.emoji), 1, user)

    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        assert self.core is not None
        self.core.on_reaction(payload.message_id, emoji_name(payload.emoji), -1, "")

    async def on_raw_message_delete(self, payload: discord.RawMessageDeleteEvent) -> None:
        assert self.core is not None
        self.core.on_delete(payload.message_id)

    async def on_raw_bulk_message_delete(self, payload: discord.RawBulkMessageDeleteEvent) -> None:
        assert self.core is not None
        for message_id in payload.message_ids:
            self.core.on_delete(message_id)

    async def close(self) -> None:
        if self.core is not None:
            await self.core.close()
        await super().close()

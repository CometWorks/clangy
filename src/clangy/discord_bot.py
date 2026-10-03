"""discord.py adapter: turns gateway events into core messages and implements the Chat protocol."""

import logging
from pathlib import Path

import aiohttp
import discord

from clangy.config import Config
from clangy.core import Attachment, Clangy, IncomingMessage

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

    async def send(self, channel_id: int, content: str) -> int:
        message = await (await self._channel(channel_id)).send(content, allowed_mentions=NO_PINGS)
        return message.id

    async def edit(self, channel_id: int, message_id: int, content: str) -> None:
        channel = await self._channel(channel_id)
        assert isinstance(channel, (discord.TextChannel, discord.Thread))
        await channel.get_partial_message(message_id).edit(content=content, allowed_mentions=NO_PINGS)

    async def create_thread(self, channel_id: int, message_id: int, name: str) -> int:
        channel = await self._channel(channel_id)
        assert isinstance(channel, discord.TextChannel)
        thread = await channel.get_partial_message(message_id).create_thread(name=name[:100])
        return thread.id

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


def incoming(message: discord.Message, bot: discord.ClientUser) -> IncomingMessage:
    channel = message.channel
    in_thread = isinstance(channel, discord.Thread)
    return IncomingMessage(
        id=message.id,
        channel_id=channel.id,
        parent_id=channel.parent_id if in_thread else None,
        guild_id=message.guild.id if message.guild else 0,
        author=message.author.display_name,
        text=message.clean_content,
        created=message.created_at,
        mentions_bot=bot in message.mentions,
        from_self=message.author.id == bot.id,
        # The starter message of a forum post has the same id as the thread
        starts_thread=in_thread and message.id == channel.id,
        thread_name=channel.name if in_thread else "",
        attachments=[Attachment(a.id, a.filename, a.size, a.url) for a in message.attachments],
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

    async def on_ready(self) -> None:
        log.info("Logged in as %s", self.user)

    async def on_message(self, message: discord.Message) -> None:
        assert self.core is not None and self.user is not None
        try:
            await self.core.on_message(incoming(message, self.user))
        except Exception:
            # One bad message must not take the gateway handler down
            log.exception("Failed to handle message %s", message.id)

    async def close(self) -> None:
        if self.core is not None:
            await self.core.close()
        await super().close()

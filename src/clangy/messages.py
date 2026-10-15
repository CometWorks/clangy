"""Discord messages as the core sees them."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Attachment:
    id: int
    filename: str
    size: int
    url: str


@dataclass
class IncomingMessage:
    id: int
    channel_id: int
    """The thread for messages in threads, otherwise the channel."""
    parent_id: int | None
    """Parent channel of the thread, None outside threads."""
    guild_id: int
    author: str
    text: str
    """Message text with mentions resolved to names."""
    created: datetime
    mentions_bot: bool = False
    from_self: bool = False
    starts_thread: bool = False
    """First message of a forum post."""
    thread_name: str = ""
    thread_tags: list[str] = field(default_factory=list)
    """Names of the tags applied to the forum post."""
    attachments: list[Attachment] = field(default_factory=list)
    reactions: dict[str, int] = field(default_factory=dict)
    """Count of each reaction emoji, custom ones as `:name:`."""
    replied_to: "IncomingMessage | None" = None
    """The message this one replies to; a thread started from a channel message points to that message."""

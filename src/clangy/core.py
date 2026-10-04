"""Discord independent core: conversations, triggers, runs, progress and retries."""

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Protocol

import aiohttp
from pydantic import BaseModel, Field

from clangy import text
from clangy.backend import Backend
from clangy.cache import MessageCache
from clangy.config import ChannelConfig, Config, Game, SandboxConfig
from clangy.files import FileError, Outbox, safe_filename
from clangy.messages import Attachment, IncomingMessage
from clangy.proxy import HostServer, ProxyRun, RunStats, TcpRelay, ToolError, ToolReply
from clangy.runner import PACKAGE_DIR, RunPaths, RunResult, RunSpec, free_port, run_pi, write_pi_config
from clangy.store import Store, StoreError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = PACKAGE_DIR / "prompts" / "system.md"
GUIDE_DIR = PACKAGE_DIR / "guide"
WORKSPACE_DIR = PACKAGE_DIR / "workspace"
"""pyproject.toml and uv.lock of the sandboxes' shared Python environment."""
ANSWER_TOKENS = 500
FIRST_RUN_TOKENS = 3000
"""System prompt and tool definitions, processed only on the first run of a conversation."""
CACHE_AGE = timedelta(days=2)


class HistoryUnavailable(RuntimeError):
    """The chat before a mention can't be read, so there's no context to answer from."""


class Chat(Protocol):
    bot_name: str

    async def send(self, channel_id: int, content: str, files: list[Path] | None = None) -> int: ...

    async def edit(self, channel_id: int, message_id: int, content: str, files: list[Path] | None = None) -> None: ...

    async def create_thread(self, channel_id: int, message_id: int, name: str) -> int: ...

    async def history(
        self, channel_id: int, before_id: int | None, limit: int | None, after: datetime | None
    ) -> list[IncomingMessage]:
        """The newest `limit` (None: all) channel messages after a time (None: from the start) and before a message
        (None: now), oldest first. Raises HistoryUnavailable."""
        ...

    async def download(self, attachment: Attachment, path: Path) -> None: ...


class PendingMessage(BaseModel):
    id: int
    author: str
    text: str
    created: datetime
    attachments: list[str] = Field(default_factory=list)
    reference: bool = False
    """Earlier chat that came along with a mention as context, not something to answer."""
    reply_to_author: str = ""
    """Author of the message this one replies to."""
    own: bool = False
    """Clangy's earlier answer, read back from the thread."""


class KnownMessage(BaseModel):
    """A message of the conversation, kept to notice edits and deletions."""

    author: str
    text: str
    created: datetime


Status = Literal["idle", "queued", "running", "backend_error", "failed"]


class Conversation(BaseModel):
    thread_id: int
    channel_id: int
    guild_id: int
    backend: str = ""
    """The backend of the last run, its cache holds the conversation. Empty until the first run."""
    context_tokens: int = 0
    """Size of the conversation after its last answer, what another backend would have to read."""
    game: Game | None = None
    status: Status = "idle"
    pending: list[PendingMessage] = Field(default_factory=list)
    progress_message_id: int | None = None
    retries: int = 0
    runs: int = 0
    host_notes: list[str] = Field(default_factory=list)
    known: dict[int, KnownMessage] = Field(default_factory=dict)
    sent_files: list[str] = Field(default_factory=list)
    """Work folder paths of the files already sent, each file goes out at most once."""
    title: str = ""
    """The forum post's title as last told to the model."""

    @property
    def key(self) -> str:
        return str(self.thread_id)

    @property
    def url(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.thread_id}"


RunAgent = Callable[[RunSpec, int, float, Callable[[], None]], Awaitable[RunResult]]


@dataclass
class Progress:
    eta: float = 0.0
    deadline: float | None = None
    ahead: int = 0
    """Runs before this one in the backend's queue."""
    retry: int = 0
    steps: int = 0
    """Model turns so far."""
    started: float = field(default_factory=lambda: time.time())

    def render(self, max_retries: int) -> str:
        message = text.progress(self.eta, self.deadline, self.ahead, time.time())
        return f"{message} (retry {self.retry}/{max_retries})" if self.retry else message


def knowledge_note_start(game: Game) -> str:
    return f"[host] This conversation is about {game.upper()}."


def skill_frontmatter(path: Path) -> tuple[str, str]:
    """name and description from a SKILL.md's frontmatter."""
    lines = path.read_text(errors="replace").splitlines()
    if not lines or lines[0].strip() != "---":
        return "", ""
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, _, value = line.partition(":")
        if key in ("name", "description"):
            fields[key] = value.strip().strip("\"'")
    return fields.get("name", ""), fields.get("description", "")


def proxy_relay(sandbox: SandboxConfig, paths: RunPaths) -> TcpRelay | contextlib.nullcontext:
    """Relays the run's proxy socket to the configured proxy, if any."""
    return TcpRelay(paths.proxy_socket, sandbox.proxy) if sandbox.proxy else contextlib.nullcontext()


def said_something(msg: IncomingMessage) -> bool:
    """False for the empty message a thread started from a channel message opens with."""
    return bool(msg.text or msg.attachments)


def human_size(size: int) -> str:
    return f"{size / 1024**2:.1f} MB" if size >= 1024**2 else f"{max(1, size // 1024)} KB"


class Clangy:
    def __init__(self, config: Config, chat: Chat, run_agent: RunAgent = run_pi):
        self.config = config
        self.chat = chat
        self.run_agent = run_agent
        self.store = Store(config.data_dir / "store")
        self.backends = {b.name: Backend(b) for b in config.backends}
        self.conversations: dict[int, Conversation] = {}
        self._workers: dict[int, asyncio.Task] = {}
        self._outboxes: dict[int, Outbox] = {}
        self.cache = MessageCache(config.data_dir / "messages.sqlite")
        self._live_since = datetime.now(timezone.utc)
        """Since when the cache has every message of Clangy's channels and their threads."""
        self._covered_since: dict[int, datetime] = {}
        """Channels whose cache reaches further back, from catching up or fetching."""
        self._caught_up: set[int] = set()
        """Conversations that have every message since the last (re)connect."""
        self._message_threads: dict[int, int] = {}
        """Message id to conversation, for edits and deletions."""
        self._in_run: dict[int, set[int]] = {}
        """Messages the running answer of each conversation works from."""
        self._first_answer: dict[int, asyncio.Task] = {}
        """Delayed first answers to new forum threads."""
        self._wake: dict[int, bool] = {}
        self._python_lock = asyncio.Lock()
        self._http: aiohttp.ClientSession | None = None

    @property
    def http(self) -> aiohttp.ClientSession:
        if self._http is None:
            # No connection reuse: llama.cpp drops idle keep-alive connections after 5 s, and a request racing
            # that close would look like a lost backend. A connect per request is nothing next to inference.
            self._http = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(force_close=True),
                timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
            )
        return self._http

    async def close(self) -> None:
        for task in [*self._workers.values(), *self._first_answer.values()]:
            task.cancel()
        if self._http is not None:
            await self._http.close()
        self.cache.close()

    # Persistence

    def conversation_dir(self, thread_id: int) -> Path:
        return self.config.data_dir / "conversations" / str(thread_id)

    def files_dir(self, thread_id: int) -> Path:
        """Every file received from and sent to the conversation, kept out of the sandbox's reach."""
        return self.conversation_dir(thread_id) / "files"

    def save(self, conv: Conversation) -> None:
        path = self.conversation_dir(conv.thread_id) / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(conv.model_dump_json(indent=2))
        tmp.replace(path)

    def expire(self) -> None:
        """Deletes the messages and conversations not touched for retention_days, except their metrics.jsonl."""
        now = datetime.now(timezone.utc)
        self._prune_cache(now)
        cutoff = (now - timedelta(days=self.config.retention_days)).timestamp()
        expired = []
        for folder in sorted((self.config.data_dir / "conversations").glob("*/")):
            state = folder / "state.json"
            contents = [path for path in folder.iterdir() if path.name != "metrics.jsonl"]
            if not contents or (state if state.exists() else folder).stat().st_mtime >= cutoff:
                continue
            # state.json goes last, so an interrupted cleanup is picked up again on the next start
            for path in sorted(contents, key=lambda path: path == state):
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
            with contextlib.suppress(OSError):
                folder.rmdir()  # Only if no metrics.jsonl kept it
            expired.append(folder.name)
        for record in (self.store.root / "records").glob("*.md"):
            if record.stat().st_mtime < cutoff:
                record.unlink()
                expired.append(f"record {record.stem}")
        if expired:
            log.info("Deleted after %s days: %s", self.config.retention_days, ", ".join(expired))

    def _prune_cache(self, now: datetime) -> None:
        self.cache.prune(now - min(CACHE_AGE, timedelta(days=self.config.retention_days)))

    async def load(self) -> None:
        """Expires old data, then loads persisted conversations. Runs cut short by a restart are reported as failed,
        never resumed."""
        self.expire()
        for path in sorted((self.config.data_dir / "conversations").glob("*/state.json")):
            conv = Conversation.model_validate_json(path.read_text())
            self.conversations[conv.thread_id] = conv
            self._message_threads.update(dict.fromkeys(conv.known, conv.thread_id))
            if conv.status not in ("queued", "running"):
                continue
            self._restore_session(conv)
            conv.status = "idle"
            self.save(conv)
            if conv.progress_message_id:
                await self.chat.edit(conv.thread_id, conv.progress_message_id, text.failed(self.chat.bot_name))
                conv.progress_message_id = None
                self.save(conv)

    # Incoming messages

    async def on_message(self, msg: IncomingMessage) -> None:
        if msg.from_self:
            return
        if self._watched(msg):
            self.cache.add(msg)
        conv = self.conversations.get(msg.channel_id)
        new = conv is None
        if conv is None:
            try:
                conv = await self._start_conversation(msg)
            except HistoryUnavailable as e:
                log.error("Not answering message %s without its context: %s", msg.id, e)
                return
        channel = self.config.channel(conv.channel_id) if conv else None
        if conv is None or channel is None:
            return
        if not new:
            await self._read_missed(conv, channel, msg)
        if channel.mode != "mention" and msg.thread_name and msg.thread_name != conv.title:
            # A forum post's title is often the question itself
            renamed = " is now" if conv.title else ""
            conv.host_notes.append(f"[host] The title of the forum post{renamed}: {msg.thread_name}")
            conv.title = msg.thread_name
        await self._add_message(conv, channel, msg)
        if msg.mentions_bot:
            self._cancel_first_answer(conv)
            conv.retries = 0
            self.trigger(conv)
        elif new and channel.initial_response or conv.thread_id in self._first_answer:
            self._schedule_first_answer(conv, channel.initial_delay)
        self.save(conv)

    async def _start_conversation(self, msg: IncomingMessage) -> Conversation | None:
        """Starts a conversation for new forum posts and for mentions in mention-mode channels."""
        if msg.parent_id is not None:
            channel = self.config.channel(msg.parent_id)
            if channel is None:
                return None
            if msg.starts_thread and channel.mode != "mention":
                return self._new_conversation(msg.channel_id, channel, msg.guild_id)
            if not msg.mentions_bot:
                return None
            # A mention in a thread Clangy doesn't know yet, e.g. one started before it ran
            earlier = await self._history(channel, msg)
            conv = self._new_conversation(msg.channel_id, channel, msg.guild_id)
        else:
            channel = self.config.channel(msg.channel_id)
            if channel is None or channel.mode != "mention" or not msg.mentions_bot:
                return None
            earlier = await self._history(channel, msg)
            name = " ".join(msg.text.split())[:90] or f"Question from {msg.author}"
            thread_id = await self.chat.create_thread(msg.channel_id, msg.id, name)
            conv = self._new_conversation(thread_id, channel, msg.guild_id)
        for message in earlier:
            await self._add_message(conv, channel, message, reference=True)
        return conv

    async def _read_missed(self, conv: Conversation, channel: ChannelConfig, msg: IncomingMessage) -> None:
        """Queues what was posted in a known thread while Clangy was offline, before the message that just came in."""
        if conv.thread_id in self._caught_up:
            return
        # Before the await, so messages arriving meanwhile don't read the same again
        self._caught_up.add(conv.thread_id)
        newest = max((m.created for m in conv.known.values()), default=None)
        try:
            missed = await self.chat.history(conv.thread_id, msg.id, None, newest)
        except HistoryUnavailable as e:
            log.warning("Couldn't read the messages missed in thread %s: %s", conv.thread_id, e)
            return
        for message in missed:
            if not message.from_self and message.id not in conv.known and said_something(message):
                await self._add_message(conv, channel, message)

    def _watched(self, msg: IncomingMessage) -> bool:
        """Whether the message is in one of Clangy's channels or their threads."""
        return any(c is not None and self.config.channel(c) for c in (msg.channel_id, msg.parent_id))

    @staticmethod
    def context_start(channel: ChannelConfig, now: datetime) -> datetime:
        """A mention's context reaches back to midnight UTC, but at least history_hours."""
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return min(midnight, now - timedelta(hours=channel.history_hours))

    async def _history(self, channel: ChannelConfig, msg: IncomingMessage) -> list[IncomingMessage]:
        """The chat before a mention, plus the older messages it replies to. A thread is one conversation, so that's
        all of it, read from Discord. A channel's comes from the cache, and Discord fills in what isn't cached yet."""
        if msg.parent_id is not None:
            earlier = self._own_answers_only(await self.chat.history(msg.channel_id, msg.id, None, None))
        else:
            start = self.context_start(channel, msg.created)
            if start < self._covered_since.get(msg.channel_id, self._live_since):
                await self._fetch_history(msg.channel_id, msg.id, channel.history_messages, start)
            earlier = self.cache.before(msg.channel_id, msg.id, start, channel.history_messages)
        ids = {m.id for m in earlier} | {msg.id}
        replied = {r.id: r for m in [*earlier, msg] if (r := m.replied_to) and r.id not in ids and not r.from_self}
        return [m for m in sorted([*replied.values(), *earlier], key=lambda m: m.created) if said_something(m)]

    def _own_answers_only(self, messages: list[IncomingMessage]) -> list[IncomingMessage]:
        """Drops Clangy's own messages except its answers, without their footer. An answer's last part ends with the
        footer, its other parts come right before it. v1 answers have no footer, so they are dropped too."""
        footer = text.footer(self.chat.bot_name)
        kept = []
        in_answer = False
        for m in reversed(messages):
            if not m.from_self:
                kept.append(m)
                in_answer = False
            elif m.text.startswith(text.STATUS_MARKS):
                in_answer = False
            elif in_answer or m.text.endswith(footer):
                kept.append(replace(m, text=m.text.removesuffix(footer).rstrip()))
                in_answer = True
        return kept[::-1]

    async def _fetch_history(self, channel_id: int, before_id: int | None, limit: int, start: datetime) -> None:
        for message in await self.chat.history(channel_id, before_id, limit, start):
            if not message.from_self:
                self.cache.add(message)
        self._covered_since[channel_id] = start

    async def catch_up(self) -> None:
        """Fills the mention channels' context windows after a (re)connect; Discord may have dropped events."""
        now = datetime.now(timezone.utc)
        self._live_since = now
        self._covered_since.clear()
        self._caught_up.clear()
        self._prune_cache(now)
        for channel in self.config.channels:
            if channel.mode != "mention":
                continue
            try:
                await self._fetch_history(channel.id, None, channel.history_messages, self.context_start(channel, now))
            except HistoryUnavailable as e:
                log.warning("Couldn't catch up on channel %s: %s", channel.id, e)

    async def _add_message(
        self, conv: Conversation, channel: ChannelConfig, msg: IncomingMessage, reference: bool = False
    ) -> None:
        """Queues a message for the next run and remembers it for edits and deletions."""
        if channel.mode == "record" and not reference:
            files = "".join(f"\n- attachment: {a.filename}" for a in msg.attachments)
            self._record(conv, msg.author, msg.created, msg.text + files, msg.thread_name)
        conv.known[msg.id] = KnownMessage(author=msg.author, text=msg.text, created=msg.created)
        self._message_threads[msg.id] = conv.thread_id
        pending = await self._pending(conv, msg)
        pending.reference = reference
        conv.pending.append(pending)

    def _schedule_first_answer(self, conv: Conversation, delay: float) -> None:
        """(Re)starts the wait before the first answer to a new forum thread; any activity restarts it."""
        self._cancel_first_answer(conv)
        if delay <= 0:
            self.trigger(conv)
            return
        self._first_answer[conv.thread_id] = asyncio.create_task(self._first_answer_after(conv, delay))

    def _cancel_first_answer(self, conv: Conversation) -> None:
        if task := self._first_answer.pop(conv.thread_id, None):
            task.cancel()

    async def _first_answer_after(self, conv: Conversation, delay: float) -> None:
        await asyncio.sleep(delay)
        del self._first_answer[conv.thread_id]
        if conv.pending:
            self.trigger(conv)

    def _queued(self, conv: Conversation, message_id: int) -> PendingMessage | None:
        """The message if it waits for the next run; None once a run took it or the model saw it."""
        if message_id in self._in_run.get(conv.thread_id, set()):
            return None
        return next((m for m in conv.pending if m.id == message_id), None)

    def knows(self, message_id: int) -> bool:
        """Whether the message is cached or belongs to one of Clangy's conversations."""
        return message_id in self._message_threads or self.cache.has(message_id)

    def on_edit(self, msg: IncomingMessage) -> None:
        """Applies an edit to the next run; a running answer keeps working from the old text."""
        if not msg.from_self:
            self.cache.edit(msg.id, msg.text)
        conv = self.conversations.get(self._message_threads.get(msg.id, 0))
        known = conv.known.get(msg.id) if conv else None
        if conv is None or known is None or msg.from_self or known.text == msg.text:
            return
        known.text = msg.text
        if queued := self._queued(conv, msg.id):
            queued.text = msg.text
        else:
            conv.host_notes.append(
                f"[host] {known.author} edited their message from {known.created:%Y-%m-%d %H:%M} UTC, "
                f"it now reads: {msg.text}"
            )
        self._after_change(conv, known, f"edited a message, it now reads: {msg.text}")

    def on_delete(self, message_id: int) -> None:
        """Applies a deletion to the next run; a running answer keeps working from the old text."""
        self.cache.delete(message_id)
        conv = self.conversations.get(self._message_threads.pop(message_id, 0))
        known = conv.known.pop(message_id, None) if conv else None
        if conv is None or known is None:
            return
        if queued := self._queued(conv, message_id):
            conv.pending.remove(queued)
        else:
            excerpt = " ".join(known.text.split())[:200]
            conv.host_notes.append(
                f"[host] {known.author} deleted their message from {known.created:%Y-%m-%d %H:%M} UTC "
                f'("{excerpt}"). Disregard only that message, the rest of the conversation still stands.'
            )
        self._after_change(conv, known, f"deleted their message from {known.created:%Y-%m-%d %H:%M} UTC")

    def _after_change(self, conv: Conversation, known: KnownMessage, record: str) -> None:
        channel = self.config.channel(conv.channel_id)
        if channel is not None and channel.mode == "record":
            self._record(conv, known.author, datetime.now(timezone.utc), record)
        if channel is not None and conv.thread_id in self._first_answer:
            self._schedule_first_answer(conv, channel.initial_delay)
        self.save(conv)

    def _new_conversation(self, thread_id: int, channel: ChannelConfig, guild_id: int) -> Conversation:
        conv = Conversation(thread_id=thread_id, channel_id=channel.id, guild_id=guild_id, game=channel.game)
        self._caught_up.add(thread_id)
        if channel.game:
            conv.host_notes.append(self._knowledge_note(channel.game))
        self.conversations[thread_id] = conv
        return conv

    async def _pending(self, conv: Conversation, msg: IncomingMessage) -> PendingMessage:
        notes = []
        folder = self.files_dir(conv.thread_id) / "received"
        for attachment in msg.attachments:
            name = f"{msg.id}-{safe_filename(attachment.filename)}"
            if attachment.size > self.config.attachment_max_bytes:
                limit = human_size(self.config.attachment_max_bytes)
                notes.append(f"{attachment.filename} ({human_size(attachment.size)}) not saved, over the {limit} limit")
                continue
            folder.mkdir(parents=True, exist_ok=True)
            try:
                await self.chat.download(attachment, folder / name)
            except (aiohttp.ClientError, OSError, ValueError) as e:
                # Discord's attachment links expire, an older message from the cache may have a dead one
                log.warning("Download of %s failed: %r", attachment.filename, e)
                notes.append(f"{attachment.filename} could not be downloaded")
                continue
            notes.append(f"attachments/{name} ({human_size(attachment.size)})")
        replied = msg.replied_to.author if msg.replied_to else ""
        return PendingMessage(
            id=msg.id,
            author=msg.author,
            text=msg.text,
            created=msg.created,
            attachments=notes,
            reply_to_author=replied,
            own=msg.from_self,
        )

    def _record(self, conv: Conversation, author: str, when: datetime, text: str, title: str = "") -> None:
        entry = f"**{author}** ({when:%Y-%m-%d %H:%M} UTC): {text}"
        self.store.append_record(conv.thread_id, title or str(conv.thread_id), conv.url, entry)

    # Runs

    def trigger(self, conv: Conversation) -> None:
        """Requests a run; one run per conversation at a time, a trigger during a run queues another one."""
        self._wake[conv.thread_id] = True
        worker = self._workers.get(conv.thread_id)
        if worker is None or worker.done():
            self._workers[conv.thread_id] = asyncio.create_task(self._work(conv))

    async def _work(self, conv: Conversation) -> None:
        while self._wake.pop(conv.thread_id, False):
            try:
                await self._run(conv)
            except Exception:
                # Keeps the bot alive; the conversation is left in a retryable state
                log.exception("Run failed for conversation %s", conv.thread_id)
                conv.status = "idle"
                if conv.progress_message_id is not None:
                    await self.chat.edit(conv.thread_id, conv.progress_message_id, text.failed(self.chat.bot_name))
                    conv.progress_message_id = None
                self.save(conv)

    async def wait_idle(self) -> None:
        """Waits until no runs are in progress (for tests)."""
        while running := [t for t in [*self._workers.values(), *self._first_answer.values()] if not t.done()]:
            await asyncio.gather(*running)

    async def _run(self, conv: Conversation) -> None:
        progress = Progress(retry=conv.retries if conv.status == "backend_error" else 0)
        prompt = self._prompt(conv)
        first_run = not RunPaths(self._work_dir(conv)).session.exists()
        prompt_tokens = len(prompt) // 4 + (FIRST_RUN_TOKENS if first_run else 0)
        backend, reason = await self._choose_backend(conv, prompt_tokens)
        if backend is None:
            conv.status = "backend_error"
            await self._show(conv, text.backend_lost(conv.retries, self.config.max_retries))
            self.save(conv)
            return
        timing: dict = {}
        if reason:
            log.info(
                "Conversation %s moves from %s to %s: %s", conv.thread_id, conv.backend or "-", backend.name, reason
            )
            timing["backend_switch"] = {"from": conv.backend or None, "reason": reason}
            prompt_tokens += conv.context_tokens
            conv.backend = backend.name
        conv.status = "queued"
        progress.eta = time.time() + backend.estimate(conv.key, prompt_tokens, ANSWER_TOKENS)
        progress.ahead = backend.ahead_of(conv.key)
        timing |= {"predicted_seconds": round(progress.eta - progress.started, 1), "prompt_estimate": prompt_tokens}
        shown = progress.render(self.config.max_retries)
        await self._show(conv, shown)
        updater = asyncio.create_task(self._update_progress(conv, backend, progress, shown))
        consumed = {m.id for m in conv.pending}
        self._in_run[conv.thread_id] = consumed
        delivered = list(conv.host_notes)
        self._snapshot_session(conv)
        files = self.files_dir(conv.thread_id)
        outbox = Outbox(
            work=self._work_dir(conv),
            staging=files / "outbox",
            sent=set(conv.sent_files),
            total_bytes=self.config.answer_max_bytes,
            markers=self.config.secret_markers,
        )
        self._outboxes[conv.thread_id] = outbox
        try:
            await backend.acquire(conv.key)
            started = time.monotonic()
            timing["queue_seconds"] = round(time.time() - progress.started, 1)
            try:
                conv.status = "running"
                self.save(conv)
                progress.eta = time.time() + backend.estimate(conv.key, prompt_tokens, ANSWER_TOKENS)
                progress.deadline = time.time() + self.config.run_deadline
                result, stats = await self._execute(conv, backend, prompt, progress, delivered)
            finally:
                timing["run_seconds"] = round(time.monotonic() - started, 1)
                await backend.release(conv.key)
        finally:
            updater.cancel()
        if result.answer is not None:
            backend.record_run(timing["run_seconds"])
        timing["total_seconds"] = round(time.time() - progress.started, 1)
        timing["steps"] = progress.steps
        self._log_metrics(conv, result, stats, timing)
        del self._outboxes[conv.thread_id]
        del self._in_run[conv.thread_id]
        await self._finish(conv, backend, result, stats, consumed, delivered, outbox)

    async def _choose_backend(self, conv: Conversation, new_tokens: int) -> tuple[Backend | None, str | None]:
        """The backend for the next run, and why it isn't the conversation's current one.

        Runs go to the most preferred backend (lowest priority) that works and has a free slot. A conversation stays
        on its backend, whose cache holds it, unless a more preferred one is free, its backend failed, or another
        backend is faster by switch_margin even after reading the whole conversation. Less preferred backends count
        for that only while the conversation's backend is fully busy. Ties go to the backend listed first.
        """
        now = time.monotonic()
        available = [b for b in self.backends.values() if b.available(now)]
        if not available:
            # A llama.cpp server may be back already, e.g. when a mention comes before the next health check
            for backend in self.backends.values():
                if backend.config.kind == "llamacpp" and await backend.check_health(self.http):
                    available.append(backend)
        if not available:
            return None, None
        current = self.backends.get(conv.backend)

        def eta(backend: Backend) -> float:
            tokens = new_tokens if backend is current else conv.context_tokens + new_tokens
            return backend.estimate(conv.key, tokens, ANSWER_TOKENS)

        def free(backend: Backend) -> bool:
            return backend.ahead_of(conv.key) == 0

        if current in available:
            preferred = [b for b in available if b.config.priority < current.config.priority and free(b)]
            if preferred:
                return min(preferred, key=lambda b: (b.config.priority, eta(b))), "priority"
            busy = not free(current)
            others = [
                b for b in available if b is not current and (b.config.priority == current.config.priority or busy)
            ]
            faster = [b for b in others if eta(b) + self.config.switch_margin < eta(current)]
            if faster:
                return min(faster, key=lambda b: (eta(b), b.config.priority)), "balance"
            return current, None
        free_ones = [b for b in available if free(b)]
        if free_ones:
            best = min(free_ones, key=lambda b: (b.config.priority, eta(b)))
        else:
            best = min(available, key=lambda b: (eta(b), b.config.priority))
        return best, "new" if current is None else "failure"

    async def _finish(
        self,
        conv: Conversation,
        backend: Backend,
        result: RunResult,
        stats: RunStats,
        consumed: set[int],
        delivered: list[str],
        outbox: Outbox,
    ) -> None:
        bot = self.chat.bot_name
        if result.answer is not None and not stats.backend_failed:
            conv.pending = [m for m in conv.pending if m.id not in consumed]
            conv.host_notes = [n for n in conv.host_notes if n not in delivered]
            conv.status = "idle"
            conv.retries = 0
            conv.runs += 1
            if last := next((r for r in reversed(stats.per_request) if r.get("prompt_tokens")), None):
                conv.context_tokens = last["prompt_tokens"] + last.get("completion_tokens", 0)
            attachments = outbox.commit(self.files_dir(conv.thread_id) / "sent" / f"{conv.runs:04d}")
            conv.sent_files = sorted(outbox.sent)
            self.save(conv)
            await self._post_answer(conv, self._guard(result.answer), attachments)
            return
        shutil.rmtree(outbox.staging, ignore_errors=True)
        self._restore_session(conv)
        if stats.backend_failed:
            now = time.monotonic()
            backend.mark_failed(now, stats.retry_after)
            if conv.retries >= self.config.max_retries:
                conv.status = "failed"
                message = text.backend_gave_up(bot)
            else:
                conv.status = "backend_error"
                message = text.backend_lost(conv.retries, self.config.max_retries)
                if any(b.available(now) for b in self.backends.values()):
                    # Another backend takes over right away, even though it has to read the whole conversation
                    conv.retries += 1
                    self.trigger(conv)
        else:
            log.warning("Run failed for conversation %s: %s", conv.thread_id, result.error or "timeout")
            conv.status = "idle"
            message = text.timed_out(bot) if result.timed_out else text.failed(bot)
        await self._show(conv, message)
        if conv.status != "backend_error":
            conv.progress_message_id = None
        self.save(conv)

    async def _execute(
        self, conv: Conversation, backend: Backend, prompt: str, progress: Progress, delivered: list[str]
    ) -> tuple[RunResult, RunStats]:
        """Runs the agent; if it records the game version, runs again with the knowledge mounted."""
        game = conv.game
        stats = RunStats()
        tools: list[str] = []
        venv = await self.python_env()
        for _ in range(2):
            paths, read_only = self._prepare_work_dir(conv)
            port = free_port()
            write_pi_config(paths, backend.config, port)
            slot = await backend.take_slot(self.http, conv.key, time.monotonic())
            run = ProxyRun(backend=backend, session=self.http, tools=self._tool_handler(conv), slot=slot)
            run.stats = stats
            spec = RunSpec(
                paths=paths,
                backend=backend.config,
                sandbox=self.config.sandbox,
                prompt=prompt,
                read_only=read_only,
                proxy_port=free_port() if self.config.sandbox.proxy else None,
                venv=venv,
            )
            remaining = max(1.0, (progress.deadline or 0) - time.time())

            def on_turn() -> None:
                progress.steps += 1

            async with HostServer(run, paths.socket):
                async with proxy_relay(self.config.sandbox, paths):
                    result = await self.run_agent(spec, port, remaining, on_turn)
            tools += result.tools
            result.tools = tools
            if conv.game == game or result.timed_out or stats.backend_failed:
                return result, stats
            game = conv.game
            note = next(n for n in reversed(conv.host_notes) if n.startswith(knowledge_note_start(game)))
            delivered.append(note)
            # The text of the run that called set_game is never shown, so the model must answer again
            prompt = f"{note} Now write your answer to the user's messages; don't refer to earlier drafts."
        return result, stats

    def _prompt(self, conv: Conversation) -> str:
        now = datetime.now(timezone.utc)
        lines = [f"[host] {now:%Y-%m-%d %H:%M} UTC"]
        lines += conv.host_notes
        reference = [m for m in conv.pending if m.reference]
        if reference:
            lines.append(
                "[host] Earlier messages, for reference only. Don't answer them; "
                "use them only where the messages after them refer to them:"
            )
            lines += self._message_lines(reference)
            lines.append("[host] End of the earlier messages.")
        lines += self._message_lines([m for m in conv.pending if not m.reference])
        return "\n".join(lines)

    @staticmethod
    def _message_lines(messages: list[PendingMessage]) -> list[str]:
        lines = []
        for m in messages:
            note = " (you)" if m.own else f" (reply to {m.reply_to_author})" if m.reply_to_author else ""
            lines.append(f"{m.author}{note}: {m.text}")
            lines += [f"[host] Attachment from {m.author}: {a}" for a in m.attachments]
        return lines

    def _knowledge_note(self, game: Game) -> str:
        """Tells the model the game and its skills, like pi's own skill list, without touching the system prompt."""
        lines = [f"{knowledge_note_start(game)} Its knowledge is under knowledge/. Don't mention this note."]
        skills = self.skills(game)
        if skills:
            lines.append(
                "Skills: read the matching SKILL.md before answering and follow it; "
                "run its commands from the skill's folder."
            )
            lines += [f"- {name}: {description} ({path})" for name, description, path in skills]
        return "\n".join(lines)

    def skills(self, game: Game) -> list[tuple[str, str, str]]:
        """Name, description and work folder path of every SKILL.md in the game's knowledge."""
        found = []
        for link, target in self.knowledge_links(game).items():
            if target is None or not link.startswith("knowledge/"):
                continue
            for depth in ("", "*/", "*/*/"):
                for skill_file in sorted(target.glob(f"{depth}SKILL.md")):
                    name, description = skill_frontmatter(skill_file)
                    path = f"{link}/{skill_file.relative_to(target).as_posix()}"
                    found.append((name or skill_file.parent.name, description, path))
        return found

    # Work folder and sandbox

    def _work_dir(self, conv: Conversation) -> Path:
        return self.conversation_dir(conv.thread_id) / "work"

    def knowledge_links(self, game: Game | None) -> dict[str, Path | None]:
        """Work folder links of the mounts and the game's knowledge; the other game's links map to None."""
        links: dict[str, Path | None] = {s.link: None for sources in self.config.knowledge.values() for s in sources}
        sources = [("common", s) for s in self.config.mounts]
        sources += [(game, s) for s in self.config.knowledge.get(game, [])] if game else []
        for folder, source in sources:
            target = source.path or self.config.data_dir / "knowledge" / "current" / folder / source.link
            # Resolved per run, so a run keeps one snapshot even if the daily sync swaps the tree
            links[source.link] = target.resolve() if target.exists() else None
        return links

    def _prepare_work_dir(self, conv: Conversation) -> tuple[RunPaths, list[Path]]:
        """Refreshes the work folder's links and system prompt; returns the paths the sandbox may read."""
        paths = RunPaths(self._work_dir(conv))
        paths.pi_dir.mkdir(parents=True, exist_ok=True)
        received = self.files_dir(conv.thread_id) / "received"
        received.mkdir(parents=True, exist_ok=True)
        links = {
            "attachments": received.resolve(),
            "store": self.store.root.resolve(),
            "guide": GUIDE_DIR,
            **self.knowledge_links(conv.game),
        }
        for name, target in links.items():
            link = paths.work / name
            if link.is_symlink():
                link.unlink()
            if target is not None:
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target, target_is_directory=True)
        channel = self.config.channel(conv.channel_id)
        system = SYSTEM_PROMPT.read_text()
        if channel is not None and channel.prompt:
            system += f"\n# Channel\n{channel.prompt.strip()}\n"
        paths.system_prompt.write_text(system)
        return paths, [target for target in links.values() if target is not None]

    async def python_env(self) -> Path | None:
        """The sandboxes' shared Python environment, mounted read-only and first on their PATH. Built on first use.

        It lives in a folder named after the lock and the interpreter, since a venv holds absolute paths and can't be
        moved: a changed lock or a new Python gets a new folder while runs on the old one go on. uv may hardlink
        from its cache, the sandboxes can't write to it. Bytecode is compiled up front, a read-only venv can't
        cache it.
        """
        interpreter = shutil.which(self.config.sandbox.python)
        if interpreter is None:
            log.warning("No Python environment: %s not found", self.config.sandbox.python)
            return None
        interpreter = os.path.realpath(interpreter)
        lock = (WORKSPACE_DIR / "uv.lock").read_bytes()
        folder = self.config.data_dir / "python" / hashlib.sha256(lock + interpreter.encode()).hexdigest()[:16]
        async with self._python_lock:
            if (folder / ".complete").exists():
                return folder / ".venv"
            shutil.rmtree(folder, ignore_errors=True)
            folder.mkdir(parents=True)
            for name in ("pyproject.toml", "uv.lock"):
                shutil.copyfile(WORKSPACE_DIR / name, folder / name)
            command = [
                self.config.sandbox.uv,
                "sync",
                "--frozen",
                "--compile-bytecode",
                "--quiet",
                "--python",
                interpreter,
            ]
            env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
            try:
                process = await asyncio.create_subprocess_exec(
                    *command, cwd=folder, env=env, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
                )
                output, _ = await process.communicate()
            except OSError as e:
                log.warning("No Python environment: %r", e)
                return None
            if process.returncode != 0:
                log.warning("No Python environment: %s", output.decode(errors="replace")[-1000:])
                return None
            (folder / ".complete").touch()
            log.info("Built the Python environment %s", folder)
        return folder / ".venv"

    def _snapshot_session(self, conv: Conversation) -> None:
        session = RunPaths(self._work_dir(conv)).session
        backup = session.with_suffix(".bak")
        if session.exists():
            shutil.copy2(session, backup)
        else:
            backup.unlink(missing_ok=True)

    def _restore_session(self, conv: Conversation) -> None:
        """Drops whatever a failed run added to the pi session."""
        session = RunPaths(self._work_dir(conv)).session
        backup = session.with_suffix(".bak")
        if backup.exists():
            shutil.copy2(backup, session)
        else:
            session.unlink(missing_ok=True)

    # Host tools

    def _tool_handler(self, conv: Conversation) -> Callable[[str, dict], Awaitable[ToolReply | str]]:
        async def handle(name: str, params: dict) -> ToolReply | str:
            try:
                return self._tool(conv, name, params)
            except (StoreError, FileError, KeyError, TypeError) as e:
                raise ToolError(str(e)) from e

        return handle

    def _tool(self, conv: Conversation, name: str, params: dict) -> ToolReply | str:
        meta = {"thread": conv.url}
        if conv.game:
            meta["game"] = conv.game
        files = self.files_dir(conv.thread_id)
        if any(files.glob("*/*")):
            meta["files"] = str(files)
        match name:
            case "set_game":
                return self._set_game(conv, params["game"])
            case "attach_files":
                return self._outboxes[conv.thread_id].add(params["paths"], params.get("archive"))
            case "ticket_create" | "note_create" | "plan_create":
                doc_id = self.store.create(name.removesuffix("_create"), params["title"], params["body"], meta)
                return f"Created {doc_id}."
            case "ticket_add_report":
                self.store.add_report(params["id"], params["body"], meta)
                return f"Added the report to {params['id']}."
        raise ToolError(f"unknown tool: {name}")

    def _set_game(self, conv: Conversation, game: str) -> ToolReply | str:
        if game not in ("se1", "se2"):
            raise ToolError("game must be se1 or se2")
        if conv.game == game:
            return "Already recorded, the knowledge is under knowledge/."
        conv.game = game
        conv.host_notes.append(self._knowledge_note(game))
        self.save(conv)
        # The run ends here and starts again with the knowledge mounted
        return ToolReply("Recorded. The knowledge is being mounted.", stop=True)

    # Output

    def _guard(self, answer: str) -> str:
        lowered = answer.lower()
        if any(marker.lower() in lowered for marker in self.config.secret_markers):
            log.warning("Answer withheld, it mentioned a secret marker")
            return text.refused()
        return answer

    async def _show(self, conv: Conversation, content: str, files: list[Path] | None = None) -> None:
        if conv.progress_message_id is None:
            conv.progress_message_id = await self.chat.send(conv.thread_id, content, files)
            self.save(conv)
        else:
            await self.chat.edit(conv.thread_id, conv.progress_message_id, content, files)

    async def _post_answer(self, conv: Conversation, answer: str, files: list[Path]) -> None:
        """Posts the answer, replacing the progress message; the files go with the last part."""
        parts = text.answer_parts(answer, text.footer(self.chat.bot_name))
        await self._show(conv, parts[0], files if len(parts) == 1 else None)
        conv.progress_message_id = None
        self.save(conv)
        for i, part in enumerate(parts[1:], start=2):
            await self.chat.send(conv.thread_id, part, files if i == len(parts) else None)

    async def _update_progress(self, conv: Conversation, backend: Backend, progress: Progress, shown: str) -> None:
        while True:
            await asyncio.sleep(self.config.progress_interval)
            progress.ahead = backend.ahead_of(conv.key)
            current = progress.render(self.config.max_retries)
            if current != shown:
                await self.chat.edit(conv.thread_id, conv.progress_message_id, current)
                shown = current

    def _log_metrics(self, conv: Conversation, result: RunResult, stats: RunStats, timing: dict) -> None:
        """Appends the run's timing and token counts to the conversation's metrics.jsonl."""
        record = {
            "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "thread_id": conv.thread_id,
            "channel_id": conv.channel_id,
            "game": conv.game,
            "backend": conv.backend,
            "model": self.backends[conv.backend].config.model,
            "answered_by": sorted({r["model"] for r in stats.per_request if r.get("model")}),
            "run": conv.runs + 1,
            "ok": result.answer is not None,
            "timed_out": result.timed_out,
            "backend_failed": stats.backend_failed,
            "error": result.error,
            "answer_chars": len(result.answer or ""),
            "tools": result.tools,
            **timing,
            "prompt_tokens": stats.prompt_tokens,
            "cached_tokens": stats.cached_tokens,
            "completion_tokens": stats.completion_tokens,
            "requests": stats.per_request,
        }
        with open(self.conversation_dir(conv.thread_id) / "metrics.jsonl", "a") as f:
            f.write(json.dumps(record) + "\n")

    # Backend recovery

    async def monitor_backends(self) -> None:
        """Checks failed backends and retries the conversations waiting for one."""
        while True:
            await asyncio.sleep(self.config.health_interval)
            await self.retry_failed()

    async def retry_failed(self) -> None:
        for backend in self.backends.values():
            if not backend.healthy and backend.config.kind == "llamacpp":
                await backend.check_health(self.http)
        now = time.monotonic()
        if not any(b.available(now) for b in self.backends.values()):
            return
        for conv in self.conversations.values():
            if conv.status != "backend_error":
                continue
            if conv.thread_id in self._workers and not self._workers[conv.thread_id].done():
                continue
            conv.retries += 1
            self.save(conv)
            self.trigger(conv)

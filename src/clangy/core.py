"""Discord independent core: conversations, triggers, runs, progress and retries."""

import asyncio
import json
import logging
import shutil
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol

import aiohttp
from pydantic import BaseModel, Field

from clangy import text
from clangy.backend import Backend
from clangy.config import ChannelConfig, Config, Game
from clangy.files import FileError, Outbox, safe_filename
from clangy.proxy import HostServer, ProxyRun, RunStats, ToolError
from clangy.runner import PACKAGE_DIR, RunPaths, RunResult, RunSpec, free_port, run_pi, write_pi_config
from clangy.store import Store, StoreError

log = logging.getLogger(__name__)

SYSTEM_PROMPT = PACKAGE_DIR / "prompts" / "system.md"
GUIDE_DIR = PACKAGE_DIR / "guide"
ANSWER_TOKENS = 500
FIRST_RUN_TOKENS = 3000
"""System prompt and tool definitions, processed only on the first run of a conversation."""


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
    attachments: list[Attachment] = field(default_factory=list)


class Chat(Protocol):
    bot_name: str

    async def send(self, channel_id: int, content: str, files: list[Path] | None = None) -> int: ...

    async def edit(self, channel_id: int, message_id: int, content: str, files: list[Path] | None = None) -> None: ...

    async def create_thread(self, channel_id: int, message_id: int, name: str) -> int: ...

    async def download(self, attachment: Attachment, path: Path) -> None: ...


class PendingMessage(BaseModel):
    author: str
    text: str
    created: datetime
    attachments: list[str] = Field(default_factory=list)


Status = Literal["idle", "queued", "running", "backend_error", "failed"]


class Conversation(BaseModel):
    thread_id: int
    channel_id: int
    guild_id: int
    backend: str
    game: Game | None = None
    status: Status = "idle"
    pending: list[PendingMessage] = Field(default_factory=list)
    progress_message_id: int | None = None
    retries: int = 0
    runs: int = 0
    host_notes: list[str] = Field(default_factory=list)
    sent_files: list[str] = Field(default_factory=list)
    """Work folder paths of the files already sent, each file goes out at most once."""

    @property
    def key(self) -> str:
        return str(self.thread_id)

    @property
    def url(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.thread_id}"


RunAgent = Callable[[RunSpec, int, float, Callable[[str], None]], Awaitable[RunResult]]


QUEUED = "waiting in queue"


@dataclass
class Progress:
    state: str = QUEUED
    eta: float | None = None
    deadline: float | None = None
    retry: int = 0
    steps: int = 0
    """Model turns so far."""
    started: float = field(default_factory=lambda: time.time())

    def render(self, max_retries: int) -> str:
        now = time.time()
        message = text.progress(self.state, self.steps, now - self.started, self.eta, self.deadline, now)
        return f"{message} (retry {self.retry}/{max_retries})" if self.retry else message


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
        self._wake: dict[int, bool] = {}
        self._next_backend = 0
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
        for task in self._workers.values():
            task.cancel()
        if self._http is not None:
            await self._http.close()

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

    async def load(self) -> None:
        """Loads persisted conversations. Runs cut short by a restart are reported as failed, never resumed."""
        for path in sorted((self.config.data_dir / "conversations").glob("*/state.json")):
            conv = Conversation.model_validate_json(path.read_text())
            self.conversations[conv.thread_id] = conv
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
        conv = self.conversations.get(msg.channel_id)
        trigger = msg.mentions_bot
        if conv is None:
            conv, trigger = await self._start_conversation(msg)
            if conv is None:
                return
        channel = self.config.channel(conv.channel_id)
        if channel is not None and channel.mode == "record":
            self._record(conv, msg)
        conv.pending.append(await self._pending(conv, msg))
        if trigger:
            conv.retries = 0
            self.trigger(conv)
        self.save(conv)

    async def _start_conversation(self, msg: IncomingMessage) -> tuple[Conversation | None, bool]:
        """Starts a conversation for new forum posts and for mentions in mention-mode channels."""
        if msg.parent_id is not None:
            channel = self.config.channel(msg.parent_id)
            if channel is None or channel.mode == "mention" or not msg.starts_thread:
                return None, False
            return self._new_conversation(msg.channel_id, channel, msg.guild_id), channel.initial_response
        channel = self.config.channel(msg.channel_id)
        if channel is None or channel.mode != "mention" or not msg.mentions_bot:
            return None, False
        name = " ".join(msg.text.split())[:90] or f"Question from {msg.author}"
        thread_id = await self.chat.create_thread(msg.channel_id, msg.id, name)
        return self._new_conversation(thread_id, channel, msg.guild_id), True

    def _new_conversation(self, thread_id: int, channel: ChannelConfig, guild_id: int) -> Conversation:
        backend = self.config.backends[self._next_backend % len(self.config.backends)].name
        self._next_backend += 1
        conv = Conversation(
            thread_id=thread_id, channel_id=channel.id, guild_id=guild_id, backend=backend, game=channel.game
        )
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
            await self.chat.download(attachment, folder / name)
            notes.append(f"attachments/{name} ({human_size(attachment.size)})")
        return PendingMessage(author=msg.author, text=msg.text, created=msg.created, attachments=notes)

    def _record(self, conv: Conversation, msg: IncomingMessage) -> None:
        files = "".join(f"\n- attachment: {a.filename}" for a in msg.attachments)
        entry = f"**{msg.author}** ({msg.created:%Y-%m-%d %H:%M} UTC): {msg.text}{files}"
        self.store.append_record(conv.thread_id, msg.thread_name or str(conv.thread_id), conv.url, entry)

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
        while running := [t for t in self._workers.values() if not t.done()]:
            await asyncio.gather(*running)

    async def _run(self, conv: Conversation) -> None:
        backend = self.backends[conv.backend]
        progress = Progress(retry=conv.retries if conv.status == "backend_error" else 0)
        conv.status = "queued"
        prompt = self._prompt(conv)
        first_run = not RunPaths(self._work_dir(conv)).session.exists()
        prompt_tokens = len(prompt) // 4 + (FIRST_RUN_TOKENS if first_run else 0)
        progress.eta = time.time() + backend.estimate(conv.key, prompt_tokens, ANSWER_TOKENS)
        timing = {"predicted_seconds": round(progress.eta - progress.started, 1), "prompt_estimate": prompt_tokens}
        shown = progress.render(self.config.max_retries)
        await self._show(conv, shown)
        updater = asyncio.create_task(self._update_progress(conv, progress, shown))
        consumed = len(conv.pending)
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
                progress.state = "reading"
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
        await self._finish(conv, result, stats, consumed, delivered, outbox)

    async def _finish(
        self,
        conv: Conversation,
        result: RunResult,
        stats: RunStats,
        consumed: int,
        delivered: list[str],
        outbox: Outbox,
    ) -> None:
        bot = self.chat.bot_name
        if result.answer is not None and not stats.backend_failed:
            del conv.pending[:consumed]
            conv.host_notes = [n for n in conv.host_notes if n not in delivered]
            conv.status = "idle"
            conv.retries = 0
            conv.runs += 1
            attachments = outbox.commit(self.files_dir(conv.thread_id) / "sent" / f"{conv.runs:04d}")
            conv.sent_files = sorted(outbox.sent)
            self.save(conv)
            await self._post_answer(conv, self._guard(result.answer), attachments)
            return
        shutil.rmtree(outbox.staging, ignore_errors=True)
        self._restore_session(conv)
        if stats.backend_failed:
            if conv.retries >= self.config.max_retries:
                conv.status = "failed"
                message = text.backend_gave_up(bot)
            else:
                conv.status = "backend_error"
                message = text.backend_lost(conv.retries, self.config.max_retries)
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
            )
            remaining = max(1.0, (progress.deadline or 0) - time.time())

            def on_state(state: str) -> None:
                if state == "reading":
                    progress.steps += 1
                progress.state = state

            async with HostServer(run, paths.socket):
                result = await self.run_agent(spec, port, remaining, on_state)
            tools += result.tools
            result.tools = tools
            if conv.game == game or result.timed_out or stats.backend_failed:
                return result, stats
            game = conv.game
            delivered.append(conv.host_notes[-1])
            # The text of the run that called set_game is never shown, so the model must answer again
            prompt = (
                f"{conv.host_notes[-1]} Now write your answer to the user's messages; don't refer to earlier drafts."
            )
        return result, stats

    def _prompt(self, conv: Conversation) -> str:
        now = datetime.now(timezone.utc)
        lines = [f"[host] {now:%Y-%m-%d %H:%M} UTC"]
        lines += conv.host_notes
        for m in conv.pending:
            lines.append(f"{m.author}: {m.text}")
            lines += [f"[host] Attachment from {m.author}: {a}" for a in m.attachments]
        return "\n".join(lines)

    def _knowledge_note(self, game: Game) -> str:
        return f"[host] This conversation is about {game.upper()}. Its knowledge is under knowledge/. Don't mention this note."

    # Work folder and sandbox

    def _work_dir(self, conv: Conversation) -> Path:
        return self.conversation_dir(conv.thread_id) / "work"

    def knowledge_links(self, game: Game | None) -> dict[str, Path | None]:
        """Work folder links of the game's knowledge; the other game's links map to None, to be removed."""
        links: dict[str, Path | None] = {s.link: None for sources in self.config.knowledge.values() for s in sources}
        for source in self.config.knowledge.get(game, []) if game else []:
            target = source.path or self.config.data_dir / "knowledge" / "current" / game / source.link
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

    def _tool_handler(self, conv: Conversation) -> Callable[[str, dict], Awaitable[str]]:
        async def handle(name: str, params: dict) -> str:
            try:
                return self._tool(conv, name, params)
            except (StoreError, FileError, KeyError, TypeError) as e:
                raise ToolError(str(e)) from e

        return handle

    def _tool(self, conv: Conversation, name: str, params: dict) -> str:
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

    def _set_game(self, conv: Conversation, game: str) -> str:
        if game not in ("se1", "se2"):
            raise ToolError("game must be se1 or se2")
        if conv.game == game:
            return "Already recorded, the knowledge is under knowledge/."
        conv.game = game
        conv.host_notes.append(self._knowledge_note(game))
        self.save(conv)
        return "Recorded. Stop now and write nothing; you will continue right away with the knowledge mounted."

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

    async def _update_progress(self, conv: Conversation, progress: Progress, shown: str) -> None:
        backend = self.backends[conv.backend]
        while True:
            await asyncio.sleep(self.config.progress_interval)
            if progress.state.startswith(QUEUED):
                position = backend.queue_position(conv.key)
                progress.state = f"{QUEUED}, position {position}" if position > 1 else QUEUED
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
        """Retries conversations that lost their backend once it is reachable again."""
        while True:
            await asyncio.sleep(self.config.health_interval)
            await self.retry_failed()

    async def retry_failed(self) -> None:
        waiting = [c for c in self.conversations.values() if c.status == "backend_error"]
        for name in {c.backend for c in waiting}:
            if not await self.backends[name].check_health(self.http):
                continue
            for conv in (c for c in waiting if c.backend == name):
                if conv.thread_id in self._workers and not self._workers[conv.thread_id].done():
                    continue
                conv.retries += 1
                self.save(conv)
                self.trigger(conv)

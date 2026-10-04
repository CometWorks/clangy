"""Per-run host server on a unix socket: the sandbox's only way out.

It forwards chat completions to the conversation's pinned backend, holding the API key on the host,
and serves the host-side tools (tickets, notes, plans, game version).
"""

import asyncio
import json
import logging
import os
import socket
import time
from datetime import datetime, timezone
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

from clangy.backend import Backend

log = logging.getLogger(__name__)


@dataclass
class ToolReply:
    text: str
    stop: bool = False
    """Ends the agent's run after this tool call, e.g. to start it again with new mounts."""


ToolHandler = Callable[[str, dict], Awaitable[ToolReply | str]]
FAILED_STATUSES = {408, 429}
"""Besides 5xx: the backend timed out or is rate limited, another backend may answer."""
MIN_SPEED_SAMPLE = 500
"""Prompt tokens a request must process to count as a speed sample."""


class ToolError(ValueError):
    """Error message returned to the model as a failed tool call."""


@dataclass
class RunStats:
    requests: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    backend_failed: bool = False
    retry_after: float | None = None
    """Longest Retry-After the backend sent with a failure."""
    per_request: list[dict] = field(default_factory=list)


@dataclass
class ProxyRun:
    backend: Backend
    session: aiohttp.ClientSession
    tools: ToolHandler
    slot: int | None = None
    stats: RunStats = field(default_factory=RunStats)


def _usage_from_event(event: dict, stats: dict) -> None:
    usage = event.get("usage") or {}
    timings = event.get("timings") or {}
    if usage:
        stats["prompt_tokens"] = usage.get("prompt_tokens", 0)
        stats["completion_tokens"] = usage.get("completion_tokens", 0)
        stats["cached_tokens"] = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
    if event.get("model"):
        stats["model"] = event["model"]
    if timings:
        stats["timings"] = timings
        if "cache_n" in timings:
            stats["cached_tokens"] = timings["cache_n"]


def _stream_error(event: dict) -> str | None:
    """An error sent inside a stream that started with HTTP 200, as OpenRouter does when a provider fails."""
    if error := event.get("error"):
        return f"stream error: {json.dumps(error)[:500]}"
    if any(choice.get("finish_reason") == "error" for choice in event.get("choices") or []):
        return "stream error: finish_reason error"
    return None


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


def speed_samples(timings: dict) -> tuple[float | None, float | None]:
    """Prompt and generation speeds worth averaging; short prompts measure mostly fixed overhead."""
    prompt = timings.get("prompt_per_second") if timings.get("prompt_n", 0) >= MIN_SPEED_SAMPLE else None
    generation = (
        timings.get("predicted_per_second") if timings.get("predicted_n", 0) >= MIN_SPEED_SAMPLE // 10 else None
    )
    return prompt, generation


@contextmanager
def short_path(path: Path) -> Iterator[str]:
    """Unix socket paths are limited to 107 bytes; going through a directory fd keeps any path short."""
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        yield f"/proc/self/fd/{fd}/{path.name}"
    finally:
        os.close(fd)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


class TcpRelay:
    """Relays connections from a unix socket to a TCP address: the sandbox's way to the filtering proxy."""

    def __init__(self, socket_path: Path, address: str):
        self.socket_path = socket_path
        host, _, port = address.rpartition(":")
        self.host, self.port = host, int(port)
        self._server: asyncio.Server | None = None

    async def __aenter__(self) -> "TcpRelay":
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with short_path(self.socket_path) as path:
            sock.bind(path)
        self._server = await asyncio.start_unix_server(self._handle, sock=sock)
        return self

    async def __aexit__(self, *exc) -> None:
        assert self._server is not None
        self._server.close()
        self.socket_path.unlink(missing_ok=True)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            upstream_reader, upstream_writer = await asyncio.open_connection(self.host, self.port)
        except OSError as e:
            log.warning("Proxy %s:%d unreachable: %r", self.host, self.port, e)
            writer.close()
            return
        await asyncio.gather(_pipe(reader, upstream_writer), _pipe(upstream_reader, writer))


class HostServer:
    def __init__(self, run: ProxyRun, socket_path: Path):
        self.run = run
        self.socket_path = socket_path
        app = web.Application(client_max_size=64 * 1024**2)
        app.router.add_post("/v1/chat/completions", self._chat)
        app.router.add_post("/tools/{name}", self._tool)
        self._runner = web.AppRunner(app, access_log=None)

    async def __aenter__(self) -> "HostServer":
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        await self._runner.setup()
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        with short_path(self.socket_path) as path:
            sock.bind(path)
        await web.SockSite(self._runner, sock).start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._runner.cleanup()
        self.socket_path.unlink(missing_ok=True)

    async def _tool(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            params = await request.json()
            reply = await self.run.tools(name, params)
        except (ToolError, json.JSONDecodeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        if isinstance(reply, str):
            reply = ToolReply(reply)
        return web.json_response({"text": reply.text, "stop": reply.stop})

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        run = self.run
        backend = run.backend.config
        if run.stats.backend_failed:
            # The run's output is dropped anyway; don't spend more requests on a failed backend
            return web.json_response({"error": {"message": "backend failed earlier in this run"}}, status=502)
        body = await request.json()
        body["model"] = backend.model
        body.update(backend.extra_body)
        if run.slot is not None:
            body["id_slot"] = run.slot
            body["cache_prompt"] = True
        headers = {"Content-Type": "application/json"}
        if run.backend.api_key:
            headers["Authorization"] = f"Bearer {run.backend.api_key}"

        started = time.monotonic()
        stats: dict = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "slot": run.slot}
        response: web.StreamResponse | None = None
        first_token = None
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(backend.first_byte_timeout) as limit:
                async with run.session.post(f"{backend.url}/chat/completions", json=body, headers=headers) as upstream:
                    stats["status"] = upstream.status
                    response = web.StreamResponse(
                        status=upstream.status, headers={"Content-Type": upstream.content_type}
                    )
                    await response.prepare(request)
                    if upstream.status in FAILED_STATUSES or upstream.status >= 500:
                        self._failed(stats, f"HTTP {upstream.status}", upstream.headers.get("Retry-After"))
                    async for line in upstream.content:
                        await response.write(line)
                        # Only data counts: OpenRouter sends keep-alive comments while a stuck provider is silent
                        if not line.startswith(b"data: {"):
                            continue
                        if first_token is None:
                            first_token = time.monotonic()
                        stall = backend.stall_timeout
                        limit.reschedule(loop.time() + stall if stall else None)
                        event = json.loads(line[6:])
                        _usage_from_event(event, stats)
                        if error := _stream_error(event):
                            self._failed(stats, error)
                    await response.write_eof()
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("Backend %s failed: %r", backend.name, e)
            self._failed(stats, repr(e))
            self._record(stats, started, first_token)
            if response is None:
                return web.json_response({"error": {"message": f"backend unreachable: {e!r}"}}, status=502)
            with suppress(ConnectionError):
                # Ends pi's turn with an error instead of a cut-off answer it would take as complete
                await response.write(_sse({"error": {"message": f"backend failed: {stats['error']}"}}))
            return response
        self._record(stats, started, first_token)
        return response

    def _failed(self, stats: dict, error: str, retry_after: str | None = None) -> None:
        self.run.stats.backend_failed = True
        stats["error"] = error
        try:
            seconds = float(retry_after) if retry_after else None
        except ValueError:
            seconds = None
        if seconds is not None:
            self.run.stats.retry_after = max(self.run.stats.retry_after or 0.0, seconds)

    def _record(self, stats: dict, started: float, first_token: float | None) -> None:
        s = self.run.stats
        s.requests += 1
        s.prompt_tokens += stats.get("prompt_tokens", 0)
        s.cached_tokens += stats.get("cached_tokens", 0)
        s.completion_tokens += stats.get("completion_tokens", 0)
        stats["seconds"] = round(time.monotonic() - started, 3)
        if first_token is not None:
            stats["first_token_seconds"] = round(first_token - started, 3)
        s.per_request.append(stats)
        self.run.backend.record_speed(*speed_samples(stats.get("timings", {})))

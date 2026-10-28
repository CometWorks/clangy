"""Per-run host server on a unix socket: the sandbox's only way out.

It forwards model requests (chat completions or Anthropic messages) to the run's backend, holding the API key on
the host, and serves the host-side tools (tickets, notes, plans, game version).
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


ToolHandler = Callable[[str, dict], Awaitable[str]]
FAILED_STATUSES = {401, 402, 403, 404, 408, 429}
"""Besides 5xx: the backend can't serve now (key, credit or model unavailable, timeout, rate limit), unlike a
request it rejects (400). Another backend may answer."""
ANTHROPIC_BETA = "anthropic-beta"
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
    refusal: str | None = None
    """The category of a refusal by the model, `unknown` if it gave none."""
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


def _anthropic_usage_from_event(event: dict, stats: dict) -> None:
    """message_start carries the input tokens, message_delta the output tokens. Cached input isn't in input_tokens."""
    message = event.get("message") or {}
    usage = event.get("usage") or message.get("usage") or {}
    if "input_tokens" in usage:
        cached = usage.get("cache_read_input_tokens") or 0
        stats["prompt_tokens"] = usage["input_tokens"] + cached + (usage.get("cache_creation_input_tokens") or 0)
        stats["cached_tokens"] = cached
    if "output_tokens" in usage:
        stats["completion_tokens"] = usage["output_tokens"]
    if message.get("model"):
        stats["model"] = message["model"]


def _refusal(event: dict) -> str | None:
    """The category of an Anthropic refusal, which ends the message with stop_reason refusal."""
    delta = event.get("delta") or {}
    if event.get("type") != "message_delta" or delta.get("stop_reason") != "refusal":
        return None
    details = delta.get("stop_details") or event.get("stop_details") or {}
    return details.get("category") or "unknown"


def _stream_error(event: dict) -> str | None:
    """An error sent inside a stream that started with HTTP 200, as OpenRouter does when a provider fails and
    Anthropic when it's overloaded."""
    if error := event.get("error"):
        return f"stream error: {json.dumps(error)[:500]}"
    if any(choice.get("finish_reason") == "error" for choice in event.get("choices") or []):
        return "stream error: finish_reason error"
    return None


def _sse_error(message: str, anthropic: bool) -> bytes:
    """An error event that ends pi's turn, in the stream format of the backend's API."""
    if anthropic:
        payload = {"type": "error", "error": {"type": "api_error", "message": message}}
        return f"event: error\ndata: {json.dumps(payload)}\n\n".encode()
    return f"data: {json.dumps({'error': {'message': message}})}\n\n".encode()


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
        app.router.add_post("/v1/messages", self._chat)
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
        return web.json_response({"text": reply})

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
        headers = {"Content-Type": "application/json", **backend.headers, **run.backend.auth_headers()}
        # pi's API version and betas, which its request body is written for; the config's betas are added
        if version := request.headers.get("anthropic-version"):
            headers["anthropic-version"] = version
        betas = [headers.get(ANTHROPIC_BETA), request.headers.get(ANTHROPIC_BETA)]
        if any(betas):
            headers[ANTHROPIC_BETA] = ",".join(b for b in betas if b)
        anthropic = backend.kind == "anthropic"
        usage_from_event = _anthropic_usage_from_event if anthropic else _usage_from_event
        url = backend.url + request.path.removeprefix("/v1")

        started = time.monotonic()
        stats: dict = {"time": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), "slot": run.slot}
        response: web.StreamResponse | None = None
        first_token = None
        loop = asyncio.get_running_loop()
        try:
            async with asyncio.timeout(backend.first_byte_timeout) as limit:
                async with run.session.post(url, json=body, headers=headers) as upstream:
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
                        event = json.loads(line[6:])
                        if event.get("type") == "ping":
                            continue  # Anthropic's keep-alive
                        if first_token is None:
                            first_token = time.monotonic()
                        stall = backend.stall_timeout
                        limit.reschedule(loop.time() + stall if stall else None)
                        usage_from_event(event, stats)
                        if anthropic and (refusal := _refusal(event)):
                            stats["refusal"] = run.stats.refusal = refusal
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
                await response.write(_sse_error(f"backend failed: {stats['error']}", anthropic))
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

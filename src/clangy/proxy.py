"""Per-run host server on a unix socket: the sandbox's only way out.

It forwards chat completions to the conversation's pinned backend, holding the API key on the host,
and serves the host-side tools (tickets, notes, plans, game version).
"""

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp
from aiohttp import web

from clangy.backend import Backend

log = logging.getLogger(__name__)

ToolHandler = Callable[[str, dict], Awaitable[str]]


class ToolError(ValueError):
    """Error message returned to the model as a failed tool call."""


@dataclass
class RunStats:
    requests: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0
    completion_tokens: int = 0
    backend_failed: bool = False
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
    if timings:
        stats["prompt_tps"] = timings.get("prompt_per_second")
        stats["gen_tps"] = timings.get("predicted_per_second")
        if "cache_n" in timings:
            stats["cached_tokens"] = timings["cache_n"]


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
        await web.UnixSite(self._runner, str(self.socket_path)).start()
        return self

    async def __aexit__(self, *exc) -> None:
        await self._runner.cleanup()
        self.socket_path.unlink(missing_ok=True)

    async def _tool(self, request: web.Request) -> web.Response:
        name = request.match_info["name"]
        try:
            params = await request.json()
            text = await self.run.tools(name, params)
        except (ToolError, json.JSONDecodeError) as e:
            return web.json_response({"error": str(e)}, status=400)
        return web.json_response({"text": text})

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        run = self.run
        backend = run.backend.config
        body = await request.json()
        body["model"] = backend.model
        if run.slot is not None:
            body["id_slot"] = run.slot
            body["cache_prompt"] = True
        headers = {"Content-Type": "application/json"}
        if run.backend.api_key:
            headers["Authorization"] = f"Bearer {run.backend.api_key}"

        started = time.monotonic()
        stats: dict = {}
        response: web.StreamResponse | None = None
        try:
            async with run.session.post(f"{backend.url}/chat/completions", json=body, headers=headers) as upstream:
                response = web.StreamResponse(status=upstream.status, headers={"Content-Type": upstream.content_type})
                await response.prepare(request)
                if upstream.status >= 500:
                    run.stats.backend_failed = True
                first_token = None
                async for line in upstream.content:
                    await response.write(line)
                    if first_token is None:
                        first_token = time.monotonic()
                    if line.startswith(b"data: {"):
                        _usage_from_event(json.loads(line[6:]), stats)
                await response.write_eof()
        except (aiohttp.ClientError, TimeoutError) as e:
            log.warning("Backend %s failed: %r", backend.name, e)
            run.stats.backend_failed = True
            if response is None:
                return web.json_response({"error": {"message": f"backend unreachable: {e}"}}, status=502)
            return response
        self._record(stats, started, first_token)
        return response

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
        self.run.backend.record_speed(stats.get("prompt_tps"), stats.get("gen_tps"))

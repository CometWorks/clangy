"""Offline stand-ins for Discord and the inference backend."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from clangy.core import Attachment


def sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


@dataclass
class Reply:
    text: str = ""
    tool: str | None = None
    arguments: dict = field(default_factory=dict)


class FakeBackend:
    """Scripted OpenAI compatible server with the llama.cpp extras Clangy uses."""

    def __init__(self) -> None:
        self.replies: list[Reply] = []
        self.requests: list[dict] = []
        self.headers: list[dict] = []
        self.slot_actions: list[tuple[int, str, str]] = []
        self.status = 200
        self.prompt_tokens = 100
        self.cached_tokens = 80
        self._runner: web.AppRunner | None = None
        self.port = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._chat)
        app.router.add_get("/v1/models", self._models)
        app.router.add_post("/slots/{slot}", self._slot)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self.port)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    async def _models(self, request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "test-model"}]})

    async def _slot(self, request: web.Request) -> web.Response:
        body = await request.json()
        self.slot_actions.append((int(request.match_info["slot"]), request.query["action"], body["filename"]))
        return web.json_response({})

    async def _chat(self, request: web.Request) -> web.StreamResponse:
        body = await request.json()
        self.requests.append(body)
        self.headers.append(dict(request.headers))
        if self.status != 200:
            return web.json_response({"error": {"message": "unavailable"}}, status=self.status)
        reply = self.replies.pop(0) if self.replies else Reply("Hello from the fake backend.")
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        chunk = {"id": "c", "object": "chat.completion.chunk", "created": 0, "model": body["model"]}
        if reply.tool:
            call = {"index": 0, "id": f"call_{len(self.requests)}", "type": "function"}
            call["function"] = {"name": reply.tool, "arguments": json.dumps(reply.arguments)}
            delta: dict = {"role": "assistant", "content": None, "tool_calls": [call]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": reply.text}
            finish = "stop"
        await response.write(sse({**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}))
        await response.write(sse({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}))
        usage = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": 7,
            "total_tokens": self.prompt_tokens + 7,
            "prompt_tokens_details": {"cached_tokens": self.cached_tokens},
        }
        timings = {"prompt_per_second": 500.0, "predicted_per_second": 50.0}
        await response.write(sse({**chunk, "choices": [], "usage": usage, "timings": timings}))
        await response.write(b"data: [DONE]\n\n")
        return response


@dataclass
class Sent:
    channel_id: int
    message_id: int
    content: str
    edits: list[str] = field(default_factory=list)

    @property
    def current(self) -> str:
        return self.edits[-1] if self.edits else self.content


class FakeChat:
    bot_name = "Clangy"

    def __init__(self) -> None:
        self.messages: dict[int, Sent] = {}
        self.threads: list[tuple[int, int, str]] = []
        self.files: dict[str, bytes] = {}
        self._next_id = 9000

    def in_channel(self, channel_id: int) -> list[Sent]:
        return [m for m in self.messages.values() if m.channel_id == channel_id]

    async def send(self, channel_id: int, content: str) -> int:
        self._next_id += 1
        self.messages[self._next_id] = Sent(channel_id, self._next_id, content)
        return self._next_id

    async def edit(self, channel_id: int, message_id: int, content: str) -> None:
        assert self.messages[message_id].channel_id == channel_id
        self.messages[message_id].edits.append(content)

    async def create_thread(self, channel_id: int, message_id: int, name: str) -> int:
        self.threads.append((channel_id, message_id, name))
        return message_id

    async def download(self, attachment: Attachment, path: Path) -> None:
        path.write_bytes(self.files[attachment.url])

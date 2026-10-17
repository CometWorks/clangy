"""Offline stand-ins for Discord, the inference backend and Vikunja."""

import asyncio
import http.server
import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from aiohttp import web

from clangy.core import Attachment, HistoryUnavailable


def sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


@dataclass
class Reply:
    text: str = ""
    reasoning: str = ""
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
        self.retry_after: str | None = None
        self.stall_first = 0.0
        """Seconds before the first data, with keep-alive comments if `keepalive`."""
        self.keepalive = False
        self.stall_mid = 0.0
        """Seconds of silence after the first chunk."""
        self.stream_error = False
        """Fails inside the stream after HTTP 200, like OpenRouter when a provider fails."""
        self.answering_model: str | None = None
        self.prompt_tokens = 100
        self.cached_tokens = 80
        self.processed_tokens = 1000
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
            headers = {"Retry-After": self.retry_after} if self.retry_after else None
            return web.json_response(
                {"error": {"code": self.status, "message": "unavailable"}}, status=self.status, headers=headers
            )
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        if self.keepalive:
            await response.prepare(request)
            for _ in range(int(self.stall_first / 0.05)):
                await response.write(b": OPENROUTER PROCESSING\n\n")
                await asyncio.sleep(0.05)
        else:
            await asyncio.sleep(self.stall_first)
            await response.prepare(request)
        chunk = {
            "id": "c",
            "object": "chat.completion.chunk",
            "created": 0,
            "model": self.answering_model or body["model"],
        }
        if self.stream_error:
            error = {"code": 502, "message": "Provider returned error"}
            await response.write(
                sse({**chunk, "error": error, "choices": [{"index": 0, "delta": {}, "finish_reason": "error"}]})
            )
            return response
        reply = self.replies.pop(0) if self.replies else Reply("Hello from the fake backend.")
        if reply.tool:
            call = {"index": 0, "id": f"call_{len(self.requests)}", "type": "function"}
            call["function"] = {"name": reply.tool, "arguments": json.dumps(reply.arguments)}
            delta: dict = {"role": "assistant", "content": None, "tool_calls": [call]}
            finish = "tool_calls"
        else:
            delta = {"role": "assistant", "content": reply.text}
            finish = "stop"
        if reply.reasoning:
            thought = {"role": "assistant", "content": None, "reasoning_content": reply.reasoning}
            await response.write(sse({**chunk, "choices": [{"index": 0, "delta": thought, "finish_reason": None}]}))
        await response.write(sse({**chunk, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}))
        await asyncio.sleep(self.stall_mid)
        await response.write(sse({**chunk, "choices": [{"index": 0, "delta": {}, "finish_reason": finish}]}))
        usage = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": 7,
            "total_tokens": self.prompt_tokens + 7,
            "prompt_tokens_details": {"cached_tokens": self.cached_tokens},
        }
        timings = {"prompt_n": self.processed_tokens, "predicted_n": 100}
        timings |= {"prompt_per_second": 500.0, "predicted_per_second": 50.0}
        await response.write(sse({**chunk, "choices": [], "usage": usage, "timings": timings}))
        await response.write(b"data: [DONE]\n\n")
        return response


@dataclass
class Sent:
    channel_id: int
    message_id: int
    content: str
    edits: list[str] = field(default_factory=list)
    files: list[bytes] = field(default_factory=list)
    filenames: list[str] = field(default_factory=list)

    @property
    def current(self) -> str:
        return self.edits[-1] if self.edits else self.content

    def attach(self, files: list[Path] | None) -> None:
        for file in files or []:
            self.files.append(file.read_bytes())
            self.filenames.append(file.name)


class FakeChat:
    bot_name = "Clangy"

    def __init__(self) -> None:
        self.messages: dict[int, Sent] = {}
        self.threads: list[tuple[int, int, str]] = []
        self.files: dict[str, bytes] = {}
        self.channel_history: dict[int, list] = {}
        self.history_calls: list[tuple] = []
        self.unreadable: set[int] = set()
        self.parents: dict[int, int] = {}
        """Thread id to channel id, for thread_parent."""
        self._next_id = 9000

    def in_channel(self, channel_id: int) -> list[Sent]:
        return [m for m in self.messages.values() if m.channel_id == channel_id]

    async def send(self, channel_id: int, content: str, files: list[Path] | None = None) -> int:
        self._next_id += 1
        self.messages[self._next_id] = sent = Sent(channel_id, self._next_id, content)
        sent.attach(files)
        return self._next_id

    async def edit(self, channel_id: int, message_id: int, content: str, files: list[Path] | None = None) -> None:
        sent = self.messages[message_id]
        assert sent.channel_id == channel_id
        sent.edits.append(content)
        sent.attach(files)

    async def create_thread(self, channel_id: int, message_id: int, name: str) -> int:
        self.threads.append((channel_id, message_id, name))
        return message_id

    async def history(self, channel_id: int, before_id: int | None, limit: int | None, after) -> list:
        self.history_calls.append((channel_id, before_id, limit, after))
        if channel_id in self.unreadable:
            raise HistoryUnavailable(f"no access to {channel_id}")
        messages = [
            m
            for m in self.channel_history.get(channel_id, [])
            if (before_id is None or m.id < before_id) and (after is None or m.created >= after)
        ]
        return messages[-limit:] if limit else messages

    async def thread_parent(self, thread_id: int) -> int | None:
        return self.parents.get(thread_id)

    async def download(self, attachment: Attachment, path: Path) -> None:
        if attachment.url not in self.files:
            raise ValueError("404")
        path.write_bytes(self.files[attachment.url])


class FakeSquid:
    """Answers CONNECT requests like a squid with a domain allowlist."""

    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.requests: list[str] = []
        self._server: asyncio.Server | None = None
        self.port = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        assert self._server is not None
        self._server.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = (await reader.readline()).decode()
        target = line.split()[1] if len(line.split()) > 1 else ""
        self.requests.append(target)
        host = target.rsplit(":", 1)[0]
        status = "200 Connection established" if host in self.allowed else "403 Forbidden"
        writer.write(f"HTTP/1.1 {status}\r\n\r\n".encode())
        await writer.drain()
        writer.close()


class FakeVikunja:
    """The part of Vikunja's API the tracker uses, on a local port, in a thread of its own."""

    def __init__(self, project: int = 8) -> None:
        self.project = project
        self.tasks: dict[int, dict] = {}
        self.comments: dict[int, list[dict]] = {}
        self.uploads: list[tuple[int, str, bytes]] = []
        self.labels = [{"id": 5, "title": "se1"}, {"id": 6, "title": "se2"}, {"id": 7, "title": "cancelled"}]
        self.labels.append({"id": 8, "title": "note"})
        self.auth: set[str] = set()
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:
                pass

            def do_GET(self) -> None:
                self._reply(fake.handle("GET", self.path, b"", self.headers))

            def do_PUT(self) -> None:
                self._reply(fake.handle("PUT", self.path, self._body(), self.headers))

            def do_DELETE(self) -> None:
                self._reply(fake.handle("DELETE", self.path, b"", self.headers))

            def do_POST(self) -> None:
                self._reply(fake.handle("POST", self.path, self._body(), self.headers))

            def _body(self) -> bytes:
                return self.rfile.read(int(self.headers.get("Content-Length", 0)))

            def _reply(self, result: tuple[int, object]) -> None:
                status, body = result
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def add(self, title: str, description: str = "", done: bool = False, labels: tuple[str, ...] = ()) -> dict:
        task_id = len(self.tasks) + 1
        task = {
            "id": task_id,
            "project_id": self.project,
            "title": title,
            "description": description,
            "done": done,
            "labels": [l for l in self.labels if l["title"] in labels],
            "attachments": [],
            "related_tasks": {},
        }
        self.tasks[task_id] = task
        self.comments[task_id] = []
        return task

    def handle(self, method: str, url: str, body: bytes, headers) -> tuple[int, object]:
        self.auth.add(headers.get("Authorization", ""))
        path = url.split("?")[0].removeprefix("/api/v1")
        parts = path.strip("/").split("/")
        match method, parts:
            case "GET", ["tasks"]:
                page = int(re.search(r"[?&]page=(\d+)", url)[1])
                return 200, list(self.tasks.values())[(page - 1) * 250 : page * 250]
            case "PUT", ["projects", project, "tasks"] if int(project) == self.project:
                data = json.loads(body)
                return 200, self.add(data["title"], data["description"])
            case "PUT", ["projects", _, "tasks"]:
                return 403, {"code": 3001, "message": "Forbidden"}
            case "GET", ["tasks", task_id] if int(task_id) in self.tasks:
                return 200, self.tasks[int(task_id)]
            case "POST", ["tasks", task_id] if int(task_id) in self.tasks:
                self.tasks[int(task_id)].update(json.loads(body))
                return 200, self.tasks[int(task_id)]
            case "GET", ["tasks", task_id, "comments"]:
                return 200, self.comments[int(task_id)]
            case "PUT", ["tasks", task_id, "comments"]:
                comment = {"comment": json.loads(body)["comment"], "author": {"username": "bot-clangy"}}
                comment["created"] = f"2026-10-17T12:00:{len(self.comments[int(task_id)]):02d}Z"
                self.comments[int(task_id)].append(comment)
                return 201, comment
            case "PUT", ["tasks", task_id, "attachments"]:
                name = re.search(rb'filename="([^"]*)"', body)[1].decode()
                content = body.split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n--", 1)[0]
                self.uploads.append((int(task_id), name, content))
                attachment = {"id": len(self.uploads), "file": {"name": name, "size": len(content)}}
                self.tasks[int(task_id)]["attachments"].append(attachment)
                return 200, {"success": [{"file": {"name": name}}]}
            case "DELETE", ["tasks", task_id, "attachments", attachment_id]:
                task = self.tasks[int(task_id)]
                task["attachments"] = [a for a in task["attachments"] if a["id"] != int(attachment_id)]
                return 200, {"message": "Successfully deleted."}
            case "PUT", ["tasks", task_id, "relations"]:
                data = json.loads(body)
                task, other = self.tasks[int(task_id)], self.tasks[data["other_task_id"]]
                for a, b in ((task, other), (other, task)):
                    a["related_tasks"].setdefault(data["relation_kind"], []).append(
                        {"id": b["id"], "title": b["title"]}
                    )
                return 201, data
            case "GET", ["labels"]:
                return 200, self.labels
            case "PUT", ["tasks", task_id, "labels"]:
                label_id = json.loads(body)["label_id"]
                self.tasks[int(task_id)]["labels"] += [l for l in self.labels if l["id"] == label_id]
                return 201, {"label_id": label_id}
        return 404, {"code": 1, "message": f"no route {method} {path}"}

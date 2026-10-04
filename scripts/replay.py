"""Replays a short SE1 support thread through Clangy and the real pi against a live backend, then prints timings.

uv run python scripts/replay.py --url http://polaris:8080/v1 --data /tmp/replay \
    --skills ~/ws/se/se1/skills --books ~/ws/se/se1/se-dev-books --info-log ~/.config/Pulsar/Legacy/info.log
"""

import argparse
import asyncio
import json
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).parent.parent))

from clangy.config import BackendConfig, ChannelConfig, Config, KnowledgeSource, SandboxConfig  # noqa: E402
from clangy.core import Attachment, Clangy, IncomingMessage  # noqa: E402
from clangy.knowledge import sync  # noqa: E402
from clangy.timings import load, summarize  # noqa: E402
from tests.fakes import FakeChat  # noqa: E402

THREAD = 5000
FORUM = 10


class PrintingChat(FakeChat):
    """Prints what Clangy would post on Discord, with the time since the start."""

    started = time.time()

    def _print(self, verb: str, content: str) -> None:
        print(f"[{time.time() - self.started:6.1f}s] {verb} {content!r}", flush=True)

    async def send(self, channel_id: int, content: str, files: list[Path] | None = None) -> int:
        self._print("SEND", content)
        return await super().send(channel_id, content, files)

    async def edit(self, channel_id: int, message_id: int, content: str, files: list[Path] | None = None) -> None:
        self._print("EDIT", content)
        await super().edit(channel_id, message_id, content, files)


def message(number: int, text: str, attachments: list[Attachment]) -> IncomingMessage:
    return IncomingMessage(
        id=THREAD + number,
        channel_id=THREAD,
        parent_id=FORUM,
        guild_id=1,
        author="Alice",
        text=text,
        created=datetime.now(timezone.utc),
        mentions_bot=number > 0,
        starts_thread=number == 0,
        thread_name="Game crashes",
        attachments=attachments,
    )


async def model_id(url: str) -> str:
    async with aiohttp.ClientSession() as http:
        async with http.get(f"{url}/models") as r:
            return (await r.json())["data"][0]["id"]


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", help="OpenAI compatible base URL with /v1 of a llama.cpp server")
    parser.add_argument("--config", type=Path, help="take the backends from this config file instead")
    parser.add_argument("--backend", help="with --config: use only this backend")
    parser.add_argument("--data", type=Path, required=True, help="scratch data folder, conversations are reset")
    parser.add_argument("--thinking", default="off")
    parser.add_argument("--skills", type=Path, help="local clone of CometWorks/skills")
    parser.add_argument("--books", type=Path, help="local clone of se-dev-books")
    parser.add_argument("--info-log", type=Path, help="an info.log to attach in the last turn")
    args = parser.parse_args()
    if not args.url and not args.config:
        parser.error("--url or --config is required")

    sources = [(args.skills, "knowledge/skills"), (args.books, "knowledge/books")]
    repos = [KnowledgeSource(url=str(path), link=link) for path, link in sources if path]
    if args.config:
        backends = [b for b in Config.load(args.config).backends if args.backend in (None, b.name)]
    else:
        backends = [
            BackendConfig(
                name="replay",
                url=args.url,
                model=await model_id(args.url),
                kind="llamacpp",
                thinking=args.thinking,
                compat={"thinkingFormat": "qwen-chat-template"},
            )
        ]
    config = Config(
        data_dir=args.data,
        backends=backends,
        channels=[ChannelConfig(id=FORUM, mode="support", initial_response=True)],
        sandbox=SandboxConfig(firejail=False, path=f"{Path(shutil.which('pi') or 'pi').parent}:/usr/bin:/bin"),
        knowledge={"se1": repos} if repos else {},
    )
    for folder in ("conversations", "store"):
        shutil.rmtree(args.data / folder, ignore_errors=True)
    if repos:
        sync(config)

    chat = PrintingChat()
    attachments = []
    if args.info_log:
        chat.files["file://info.log"] = args.info_log.read_bytes()
        attachments.append(Attachment(1, "info.log", args.info_log.stat().st_size, "file://info.log"))
    turns = [
        message(0, "My game crashes when I load my survival world. I play with Pulsar and a few plugins.", []),
        message(1, "@Clangy It's Space Engineers 1 on Windows 11, Pulsar Legacy, single player survival.", []),
        message(2, "@Clangy here is my info.log" if attachments else "@Clangy what should I send?", attachments),
    ]
    bot = Clangy(config, chat)
    for turn in turns:
        await bot.on_message(turn)
        await bot.wait_idle()
    await bot.close()

    print("\n" + summarize(load(args.data)))
    for line in (args.data / "conversations" / str(THREAD) / "metrics.jsonl").read_text().splitlines():
        record = json.loads(line)
        keys = ("run", "ok", "backend", "answered_by", "backend_switch", "total_seconds", "predicted_seconds", "steps")
        keys += ("prompt_tokens", "cached_tokens", "completion_tokens")
        print({k: record.get(k) for k in keys})


if __name__ == "__main__":
    asyncio.run(main())

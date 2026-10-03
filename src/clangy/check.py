"""Deployment check without Discord: backends, knowledge, the sandbox, and one real answer."""

import asyncio
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from clangy.config import ChannelConfig, Config
from clangy.core import Clangy, IncomingMessage
from clangy.runner import RunPaths, RunSpec, build_command

PROBE = """
import json, os, shutil, socket
result = {}
try:
    socket.create_connection(("1.1.1.1", 53), timeout=3).close()
    result["network blocked"] = False
except OSError:
    result["network blocked"] = True
result["config and other data hidden"] = not any(os.path.exists(p) for p in hidden)
try:
    open(os.path.join(work, "probe.txt"), "w").write("ok")
    result["work folder writable"] = True
except OSError:
    result["work folder writable"] = False
for tool in ("pi", "node", "python3"):
    result[f"{tool} available"] = shutil.which(tool) is not None
print(json.dumps(result))
"""


class ConsoleChat:
    bot_name = "Clangy"

    def __init__(self) -> None:
        self._next = 0

    async def send(self, channel_id: int, content: str, files: list[Path] | None = None) -> int:
        self._next += 1
        print(f"  post: {content}")
        return self._next

    async def edit(self, channel_id: int, message_id: int, content: str, files: list[Path] | None = None) -> None:
        print(f"  edit: {content}")

    async def create_thread(self, channel_id: int, message_id: int, name: str) -> int:
        return message_id

    async def download(self, attachment, path: Path) -> None:
        raise NotImplementedError


async def probe_sandbox(config: Config, config_path: Path, base: Path) -> dict[str, bool]:
    """Runs a probe script instead of pi through the same sandbox command line."""
    work = base / "probe" / "work"
    work.mkdir(parents=True)
    other = base / "probe" / "other-conversation"
    other.mkdir()
    code = f"work, hidden = {str(work)!r}, {[str(other), str(config_path.resolve())]!r}\n{PROBE}"
    sandbox = config.sandbox.model_copy(update={"pi_command": ["python3", "-c", code]})
    spec = RunSpec(paths=RunPaths(work), backend=config.backends[0], sandbox=sandbox, prompt="")
    command = build_command(spec, 0)
    process = await asyncio.create_subprocess_exec(*command, cwd=work, stdout=asyncio.subprocess.PIPE)
    stdout, _ = await process.communicate()
    lines = stdout.decode().strip().splitlines()
    return json.loads(lines[-1]) if lines else {"probe ran": False}


async def check(config: Config, config_path: Path) -> bool:
    ok = True
    base = config.data_dir / "check"
    shutil.rmtree(base, ignore_errors=True)
    channels = [ChannelConfig(id=1, mode="support", initial_response=True)]
    bot = Clangy(config.model_copy(update={"data_dir": base, "channels": channels}), ConsoleChat())

    print("Backends:")
    for backend in bot.backends.values():
        healthy = await backend.check_health(bot.http)
        ok &= healthy
        print(f"  {backend.name}: {'ok' if healthy else 'UNREACHABLE'} ({backend.config.url})")

    print("Knowledge:")
    for game in ("se1", "se2"):
        path = config.data_dir / "knowledge" / "current" / game
        print(f"  {game}: {path.resolve() if path.exists() else 'missing'}")

    print("Sandbox:")
    for name, passed in (await probe_sandbox(config, config_path, base)).items():
        ok &= passed
        print(f"  {name}: {'ok' if passed else 'FAILED'}")

    print("Answer:")
    question = "This is a deployment check. Reply with one short sentence saying you are ready."
    message = IncomingMessage(
        id=1, channel_id=1, parent_id=1, guild_id=0, author="Check", text=question,
        created=datetime.now(timezone.utc), mentions_bot=True, starts_thread=True,
    )  # fmt: skip
    await bot.on_message(message)
    await bot.wait_idle()
    await bot.close()
    metrics = base / "conversations" / "1" / "metrics.jsonl"
    record = json.loads(metrics.read_text().splitlines()[-1]) if metrics.exists() else {}
    ok &= bool(record.get("ok"))
    print(f"  {'ok' if record.get('ok') else 'FAILED'} in {record.get('total_seconds')} s")
    return ok


def main(config: Config, config_path: Path) -> None:
    sys.exit(0 if asyncio.run(check(config, config_path)) else 1)

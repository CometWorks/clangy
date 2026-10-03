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
result["config, keys and other data hidden"] = not any(os.path.exists(p) for p in hidden)
result["knowledge readable"] = all(os.access(p, os.R_OK) and os.listdir(p) for p in readable)
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
    secrets = [other, config_path.resolve(), Path.home() / ".ssh", Path.home() / ".pi" / "agent" / "auth.json"]
    hidden = [str(p) for p in secrets if p.exists()]
    sources = [s.path for sources in config.knowledge.values() for s in sources if s.path and s.path.exists()]
    readable = [p.resolve() for p in sources]
    code = f"work, hidden, readable = {str(work)!r}, {hidden!r}, {[str(p) for p in readable]!r}\n{PROBE}"
    sandbox = config.sandbox.model_copy(update={"pi_command": ["python3", "-c", code]})
    spec = RunSpec(paths=RunPaths(work), backend=config.backends[0], sandbox=sandbox, prompt="", read_only=readable)
    command = build_command(spec, 0)
    try:
        process = await asyncio.create_subprocess_exec(*command, cwd=work, stdout=asyncio.subprocess.PIPE)
    except FileNotFoundError:
        return {f"{command[0]} installed": False}
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
    for game, sources in config.knowledge.items():
        links = Clangy(config, ConsoleChat()).knowledge_links(game)
        for source in sources:
            ok &= links[source.link] is not None
            print(f"  {game} {source.link}: {links[source.link] or 'MISSING'}")

    print("Sandbox:")
    sandbox_ok = True
    for name, passed in (await probe_sandbox(config, config_path, base)).items():
        sandbox_ok &= passed
        print(f"  {name}: {'ok' if passed else 'FAILED'}")
    if config.sandbox.firejail and not sandbox_ok:
        print("Answer:\n  skipped, the sandbox doesn't work")
        await bot.close()
        return False
    ok &= sandbox_ok

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


async def sandbox_problems(config: Config, config_path: Path) -> list[str]:
    """Sandbox properties that don't hold. firejail runs programs unsandboxed when it detects a container."""
    base = config.data_dir / "check" / "startup"
    shutil.rmtree(base, ignore_errors=True)
    results = await probe_sandbox(config, config_path, base)
    shutil.rmtree(base, ignore_errors=True)
    return [name for name, passed in results.items() if not passed]


def main(config: Config, config_path: Path) -> None:
    sys.exit(0 if asyncio.run(check(config, config_path)) else 1)

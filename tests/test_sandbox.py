"""The real firejail sandbox, where firejail is installed: the model's tools can't reach the workspace history."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from clangy.config import BackendConfig, Config, SandboxConfig
from clangy.core import Clangy, Conversation
from clangy.runner import RunSpec, build_command
from tests.fakes import FakeChat

pytestmark = pytest.mark.skipif(shutil.which("firejail") is None, reason="firejail is not installed")

ATTEMPTS = """
exec 2>/dev/null
ls store/tickets/DIS-0001.md >/dev/null && echo SEES_TICKETS
ls -a {history} >/dev/null && echo SEES_HISTORY
git --git-dir={history} log >/dev/null && echo READS_HISTORY
rm -rf {history}/refs; mkdir -p {history}; echo x > {history}/sandbox-was-here
rm -f store/tickets/DIS-0001.md; test -e store/tickets/DIS-0001.md || echo DELETED_TICKET
echo x > store/tickets/DIS-0099.md && echo WROTE_TICKET
"""


async def test_tools_in_the_sandbox_cannot_reach_the_history():
    # firejail gives the sandbox a private /tmp, so the data folder sits in the home folder, like a deployment's
    base = Path(tempfile.mkdtemp(prefix="clangy-sandbox-test-", dir=Path.home()))
    try:
        config = Config(
            data_dir=base,
            backends=[BackendConfig(name="b", url="http://127.0.0.1:1/v1", model="m", kind="llamacpp")],
        )
        bot = Clangy(config, FakeChat())
        try:
            bot.store.create("ticket", "Crash", "Body", {})
            history = bot.store.history.git_dir
            head = (history / "refs" / "heads" / "main").read_text()
            conv = Conversation(thread_id=1, channel_id=2, guild_id=3)
            paths, read_only = bot._prepare_work_dir(conv)
            sandbox = SandboxConfig(pi_command=["sh", "-c", ATTEMPTS.format(history=history), "sh"])
            spec = RunSpec(paths=paths, backend=config.backends[0], sandbox=sandbox, prompt="", read_only=read_only)
            run = subprocess.run(build_command(spec, 1), cwd=paths.work, capture_output=True, text=True, timeout=60)
            assert run.stdout.split() == ["SEES_TICKETS"], run.stderr
            # The home folder is the sandbox's own, so writes there may work but never reach the host's
            assert (history / "refs" / "heads" / "main").read_text() == head
            assert not (history / "sandbox-was-here").exists()
            assert (base / "workspace" / "tickets" / "DIS-0001.md").exists()
            assert not (base / "workspace" / "tickets" / "DIS-0099.md").exists()
        finally:
            await bot.close()
    finally:
        shutil.rmtree(base)

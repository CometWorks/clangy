"""End to end through the real pi harness and Clangy's extension, against the fake backend.

Skipped where pi isn't installed, e.g. in CI.
"""

import json
import shutil
from pathlib import Path

import pytest

from clangy import text
from clangy.config import BackendConfig, ChannelConfig, Config, SandboxConfig
from clangy.core import SYSTEM_PROMPT, Clangy
from tests.fakes import Reply
from tests.test_core import SUPPORT, forum_post, message

PI = shutil.which("pi")
pytestmark = pytest.mark.skipif(PI is None, reason="pi is not installed")


async def test_pi_run_with_host_tool_and_stable_prefix(tmp_path, backend, chat):
    assert PI is not None
    config = Config(
        data_dir=tmp_path,
        backends=[BackendConfig(name="local", url=backend.url, model="m")],
        channels=[ChannelConfig(id=SUPPORT, mode="support", initial_response=True)],
        sandbox=SandboxConfig(firejail=False, path=f"{Path(PI).parent}:/usr/bin:/bin"),
        run_deadline=60,
    )
    bot = Clangy(config, chat)
    backend.replies = [
        Reply(tool="bash", arguments={"command": "mkdir -p out && echo hi > out/notes.txt"}),
        Reply(tool="attach_files", arguments={"paths": ["out/notes.txt"]}),
        Reply(tool="ticket_create", arguments={"title": "Crash on load", "body": "Details"}),
        Reply("Filed T-0001."),
        Reply("Second answer."),
    ]
    await bot.on_message(forum_post(SUPPORT, 2000, "My game crashes"))
    await bot.wait_idle()
    first_reply = chat.in_channel(2000)[0]
    assert first_reply.current == f"Filed T-0001.\n\n{text.footer('Clangy')}"
    assert first_reply.files == [b"hi\n"]
    assert (tmp_path / "store" / "tickets" / "T-0001.md").exists()

    await bot.on_message(message(2000, "@Clangy thanks", parent_id=SUPPORT, mentions_bot=True))
    await bot.wait_idle()
    await bot.close()
    assert chat.in_channel(2000)[1].current.startswith("Second answer.")

    first = backend.requests[0]
    system = first["messages"][0]
    assert system == {"role": "system", "content": SYSTEM_PROMPT.read_text()}
    names = {t["function"]["name"] for t in first["tools"]}
    assert {"read", "grep", "find", "ls", "bash", "set_game", "attach_files", "ticket_create"} <= names
    # Each request extends the previous one byte for byte, so the backend can reuse its KV cache
    for earlier, later in zip(backend.requests, backend.requests[1:]):
        prefix = earlier["messages"]
        assert json.dumps(later["messages"][: len(prefix)]) == json.dumps(prefix)
        assert json.dumps(later["tools"]) == json.dumps(earlier["tools"])

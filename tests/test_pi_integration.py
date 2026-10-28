"""End to end through the real pi harness and Clangy's extension, against the fake backend.

Skipped where pi isn't installed, e.g. in CI.
"""

import json
import os
import shutil

import pytest

from clangy import text
from clangy.config import BackendConfig, ChannelConfig, Config, KnowledgeSource, SandboxConfig
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
        sandbox=SandboxConfig(firejail=False, path=os.environ["PATH"]),
        run_deadline=60,
    )
    bot = Clangy(config, chat)
    backend.replies = [
        Reply(tool="bash", arguments={"command": "mkdir -p out && echo hi > out/notes.txt"}),
        Reply(tool="attach_files", arguments={"paths": ["out/notes.txt"]}),
        Reply(tool="ticket_create", arguments={"title": "Crash on load", "body": "Details"}),
        Reply("Filed DIS-0001."),
        Reply("Second answer."),
    ]
    await bot.on_message(forum_post(SUPPORT, 2000, "My game crashes"))
    await bot.wait_idle()
    first_reply = chat.in_channel(2000)[0]
    assert first_reply.current == f"Filed DIS-0001.\n\n{text.footer('Clangy')}"
    assert first_reply.files == [b"hi\n"]
    assert (tmp_path / "workspace" / "tickets" / "DIS-0001.md").exists()
    run = json.loads((bot.conversation_dir(2000) / "metrics.jsonl").read_text().splitlines()[0])
    assert run["tools"] == ["bash", "attach_files", "ticket_create"] and run["steps"] == 4
    assert len(run["requests"]) == 4 and run["prompt_tokens"] == 400

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


async def test_pi_commands_reach_the_proxy(tmp_path, backend, chat, squid):
    assert PI is not None
    config = Config(
        data_dir=tmp_path,
        backends=[BackendConfig(name="local", url=backend.url, model="m")],
        channels=[ChannelConfig(id=SUPPORT, mode="support", initial_response=True)],
        sandbox=SandboxConfig(firejail=False, path=os.environ["PATH"], proxy=f"127.0.0.1:{squid.port}"),
        run_deadline=60,
    )
    probe = (
        "python3 -c \"import os, socket; port = int(os.environ['https_proxy'].rsplit(':', 1)[1]); "
        "s = socket.create_connection(('127.0.0.1', port)); s.sendall(b'CONNECT pypi.org:443 HTTP/1.1\\r\\n\\r\\n'); "
        'print(s.recv(64).decode())"'
    )
    backend.replies = [Reply(tool="bash", arguments={"command": probe}), Reply("Done.")]
    bot = Clangy(config, chat)
    await bot.on_message(forum_post(SUPPORT, 2001, "Check the proxy"))
    await bot.wait_idle()
    await bot.close()
    assert squid.requests == ["pypi.org:443"]
    tool_result = json.dumps(backend.requests[-1]["messages"][-1])
    assert "200 Connection established" in tool_result


async def test_set_game_lists_the_skills_and_the_run_goes_on(tmp_path, backend, chat):
    assert PI is not None
    skills = tmp_path / "skills"
    skills.mkdir()
    config = Config(
        data_dir=tmp_path / "data",
        backends=[BackendConfig(name="local", url=backend.url, model="m")],
        channels=[ChannelConfig(id=SUPPORT, mode="support", initial_response=True)],
        sandbox=SandboxConfig(firejail=False, path=os.environ["PATH"]),
        knowledge={"se1": [KnowledgeSource(path=skills, link="knowledge/skills")]},
        run_deadline=60,
    )
    backend.replies = [Reply(tool="set_game", arguments={"game": "se1"}), Reply("SE1 answer.")]
    bot = Clangy(config, chat)
    await bot.on_message(forum_post(SUPPORT, 2002, "My SE1 game crashes"))
    await bot.wait_idle()
    await bot.close()
    assert len(backend.requests) == 2, "one run: the set_game call, then the answer"
    tool_result = backend.requests[1]["messages"][-1]
    assert tool_result["role"] == "tool" and "This conversation is about SE1" in json.dumps(tool_result)
    assert chat.in_channel(2002)[0].current.startswith("SE1 answer.")


@pytest.mark.skipif(shutil.which("rg") is None, reason="ripgrep is not installed")
async def test_pi_grep_searches_linked_knowledge(tmp_path, backend, chat):
    assert PI is not None
    skills = tmp_path / "skills"
    (skills / "se-dev-game-code" / "Data").mkdir(parents=True)
    (skills / "se-dev-game-code" / "SKILL.md").write_text("---\nname: se-dev-game-code\ndescription: Game code\n---\n")
    (skills / "se-dev-game-code" / "Data" / "MyWolfLogic.cs").write_text("class MyWolfLogic {}\n")
    config = Config(
        data_dir=tmp_path / "data",
        backends=[BackendConfig(name="local", url=backend.url, model="m")],
        channels=[ChannelConfig(id=SUPPORT, mode="support", initial_response=True, game="se1")],
        sandbox=SandboxConfig(firejail=False, path=os.environ["PATH"]),
        knowledge={"se1": [KnowledgeSource(path=skills, link="knowledge/skills")]},
        run_deadline=60,
    )
    backend.replies = [Reply(tool="grep", arguments={"pattern": "Wolf", "path": "knowledge"}), Reply("Wolves exist.")]
    bot = Clangy(config, chat)
    await bot.on_message(forum_post(SUPPORT, 2003, "Animals?"))
    await bot.wait_idle()
    await bot.close()
    first = json.dumps(backend.requests[0]["messages"][-1])
    assert "se-dev-game-code: Game code (knowledge/skills/se-dev-game-code/SKILL.md)" in first
    assert "MyWolfLogic.cs" in json.dumps(backend.requests[1]["messages"][-1]), "grep follows the knowledge link"


async def test_session_moves_to_another_backend(tmp_path, backend, backend2, chat):
    assert PI is not None
    config = Config(
        data_dir=tmp_path,
        backends=[
            BackendConfig(name="local", url=backend.url, model="m", kind="llamacpp"),
            BackendConfig(name="cloud", url=backend2.url, model="c"),
        ],
        channels=[ChannelConfig(id=SUPPORT, mode="support", initial_response=True)],
        sandbox=SandboxConfig(firejail=False, path=os.environ["PATH"]),
        run_deadline=60,
    )
    backend.replies = [
        Reply(reasoning="Let me look.", tool="bash", arguments={"command": "echo hi"}),
        Reply(reasoning="It printed hi.", text="First answer."),
    ]
    backend2.replies = [Reply("Second answer.")]
    bot = Clangy(config, chat)
    await bot.on_message(forum_post(SUPPORT, 2004, "My game crashes"))
    await bot.wait_idle()
    backend.status = 503
    await bot.on_message(message(2004, "@Clangy thanks", parent_id=SUPPORT, mentions_bot=True))
    await bot.wait_idle()
    await bot.close()
    assert chat.in_channel(2004)[1].current.startswith("Second answer.")
    moved = backend2.requests[0]["messages"]
    # The cloud gets the session as it was, reasoning still marked as such rather than turned into answer text
    assert json.dumps(moved[: len(backend.requests[1]["messages"])]) == json.dumps(backend.requests[1]["messages"])
    assistant = [m for m in moved if m["role"] == "assistant"]
    assert [m.get("reasoning_content") for m in assistant] == ["Let me look.", "It printed hi."]
    assert "Let me look." not in json.dumps([m.get("content") for m in assistant])


@pytest.mark.parametrize("failure", ["stall_mid", "stream_error", "rate_limit"])
async def test_pi_fails_over_when_the_stream_breaks(tmp_path, backend, backend2, chat, failure):
    assert PI is not None
    config = Config(
        data_dir=tmp_path,
        backends=[
            BackendConfig(name="cloud", url=backend.url, model="c", stall_timeout=0.5),
            BackendConfig(name="local", url=backend2.url, model="m", kind="llamacpp"),
        ],
        channels=[ChannelConfig(id=SUPPORT, mode="support", initial_response=True)],
        sandbox=SandboxConfig(firejail=False, path=os.environ["PATH"]),
        run_deadline=60,
    )
    if failure == "stall_mid":
        backend.stall_mid = 5.0
    elif failure == "stream_error":
        backend.stream_error = True
    else:
        backend.status = 429
    backend.replies = [Reply("Cut off answer that must not be posted.")]
    backend2.replies = [Reply("Answer from the other backend.")]
    bot = Clangy(config, chat)
    await bot.on_message(forum_post(SUPPORT, 2005, "My game crashes"))
    await bot.wait_idle()
    await bot.close()
    assert len(backend.requests) == 1
    assert chat.in_channel(2005)[0].current.startswith("Answer from the other backend.")
    assert bot.conversations[2005].backend == "local"


async def test_claude_run_with_tool_and_cached_prefix(tmp_path, backend, chat):
    assert PI is not None
    claude = BackendConfig(name="claude", url=backend.url, model="claude-sonnet-5", kind="anthropic", api_key="k")
    config = Config(
        data_dir=tmp_path,
        backends=[claude],
        channels=[ChannelConfig(id=SUPPORT, mode="support", initial_response=True)],
        sandbox=SandboxConfig(firejail=False, path=os.environ["PATH"]),
        run_deadline=60,
    )
    backend.replies = [
        Reply(reasoning="Let me look.", tool="bash", arguments={"command": "echo hi"}),
        Reply("Claude answer."),
    ]
    bot = Clangy(config, chat)
    await bot.on_message(forum_post(SUPPORT, 2006, "My game crashes"))
    await bot.wait_idle()
    await bot.close()
    assert chat.in_channel(2006)[0].current.startswith("Claude answer.")
    first, second = backend.requests
    assert first["model"] == "claude-sonnet-5" and first["system"][0]["text"] == SYSTEM_PROMPT.read_text()
    assert "temperature" not in first and first["thinking"] == {"type": "disabled"}
    assert all(h["x-api-key"] == "k" for h in backend.headers), "the key is added on the host"
    assert "cache_control" in json.dumps(first["messages"]), "pi marks the prefix for Anthropic's prompt cache"
    call, result = second["messages"][-2:]
    assert [b["type"] for b in call["content"]] == ["thinking", "tool_use"], "signed thinking goes back unchanged"
    assert result["content"][0]["tool_use_id"] == call["content"][1]["id"] == "toolu_1"
    run = json.loads((bot.conversation_dir(2006) / "metrics.jsonl").read_text().splitlines()[0])
    assert run["prompt_tokens"] == 200 and run["requests"][0]["model"] == "claude-sonnet-5"


async def test_session_moves_between_qwen_and_claude(tmp_path, backend, backend2, chat):
    assert PI is not None
    config = Config(
        data_dir=tmp_path,
        backends=[
            BackendConfig(name="local", url=backend.url, model="m", kind="llamacpp"),
            BackendConfig(name="claude", url=backend2.url, model="claude-sonnet-5", kind="anthropic", priority=2),
        ],
        channels=[ChannelConfig(id=SUPPORT, mode="support", initial_response=True)],
        sandbox=SandboxConfig(firejail=False, path=os.environ["PATH"]),
        run_deadline=60,
    )
    backend.replies = [
        Reply(reasoning="Qwen thinks.", tool="bash", arguments={"command": "echo hi"}),
        Reply("First answer."),
        Reply("Third answer."),
    ]
    backend2.replies = [Reply(reasoning="Claude thinks.", text="Second answer.")]
    bot = Clangy(config, chat)
    await bot.on_message(forum_post(SUPPORT, 2007, "My game crashes"))
    await bot.wait_idle()
    backend.status = 503
    await bot.on_message(message(2007, "@Clangy thanks", parent_id=SUPPORT, mentions_bot=True))
    await bot.wait_idle()
    backend.status = 200
    bot.backends["local"].healthy = True
    await bot.on_message(message(2007, "@Clangy one more", parent_id=SUPPORT, mentions_bot=True))
    await bot.wait_idle()
    await bot.close()
    assert [m.current.split("\n")[0] for m in chat.in_channel(2007)] == [
        "First answer.",
        "Second answer.",
        "Third answer.",
    ]
    moved = json.dumps(backend2.requests[0]["messages"])
    assert "Qwen thinks." not in moved, "another model's reasoning is dropped, not sent as answer text"
    assert '"type": "tool_use"' in moved and '"type": "tool_result"' in moved and "First answer." in moved
    back = json.dumps(backend.requests[-1]["messages"])
    assert "Second answer." in back and "Claude thinks." not in back


async def test_claude_overloaded_fails_over(tmp_path, backend, backend2, chat):
    assert PI is not None
    config = Config(
        data_dir=tmp_path,
        backends=[
            BackendConfig(name="claude", url=backend.url, model="claude-sonnet-5", kind="anthropic"),
            BackendConfig(name="local", url=backend2.url, model="m", kind="llamacpp", priority=2),
        ],
        channels=[ChannelConfig(id=SUPPORT, mode="support", initial_response=True)],
        sandbox=SandboxConfig(firejail=False, path=os.environ["PATH"]),
        run_deadline=60,
    )
    backend.stream_error = True
    backend2.replies = [Reply("Answer from the other backend.")]
    bot = Clangy(config, chat)
    await bot.on_message(forum_post(SUPPORT, 2008, "My game crashes"))
    await bot.wait_idle()
    await bot.close()
    assert len(backend.requests) == 1
    assert chat.in_channel(2008)[0].current.startswith("Answer from the other backend.")

import asyncio
import json
import os
import shutil
import subprocess
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import aiohttp
import pytest

from clangy import core, text
from clangy.config import BackendConfig, ChannelConfig, Config, KnowledgeSource
from clangy.core import Attachment, Clangy, Conversation, IncomingMessage
from clangy.proxy import short_path
from clangy.runner import RunResult, RunSpec
from clangy.store import frontmatter

GUILD = 1
SUPPORT, QUIET_FORUM, RECORD, MENTION, SE1_FORUM, SLOW_FORUM = 10, 20, 30, 40, 50, 60
DELAY = 0.3

Step = Callable[["FakeAgent", RunSpec], Awaitable[RunResult]]


async def host(spec: RunSpec, path: str, body: dict) -> tuple[int, dict | str]:
    with short_path(spec.paths.socket) as socket:
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=socket)) as http:
            async with http.post(f"http://host{path}", json=body) as r:
                return r.status, await r.json() if path.startswith("/tools") else await r.text()


def answer(reply: str) -> Step:
    async def step(agent: "FakeAgent", spec: RunSpec) -> RunResult:
        status, _ = await host(spec, "/v1/chat/completions", {"messages": [{"role": "user", "content": spec.prompt}]})
        if status != 200:
            return RunResult(error=f"HTTP {status}")
        with open(spec.paths.session, "a") as f:
            f.write(spec.prompt + "\n")
        return RunResult(answer=reply)

    return step


def tool(name: str, params: dict, then: Step) -> Step:
    async def step(agent: "FakeAgent", spec: RunSpec) -> RunResult:
        agent.tool_results.append(await host(spec, f"/tools/{name}", params))
        return await then(agent, spec)

    return step


def blocked(release: asyncio.Event, then: Step) -> Step:
    async def step(agent: "FakeAgent", spec: RunSpec) -> RunResult:
        await release.wait()
        return await then(agent, spec)

    return step


async def timeout(agent: "FakeAgent", spec: RunSpec) -> RunResult:
    spec.paths.session.write_text("partial\n")
    return RunResult(timed_out=True)


class FakeAgent:
    """Stands in for pi, talking to the host socket like Clangy's pi extension."""

    def __init__(self) -> None:
        self.script: list[Step] = []
        self.specs: list[RunSpec] = []
        self.tool_results: list = []
        self.linked: list[bool] = []

    @property
    def prompts(self) -> list[str]:
        return [s.prompt for s in self.specs]

    async def __call__(self, spec: RunSpec, port: int, deadline: float, on_turn) -> RunResult:
        self.specs.append(spec)
        self.on_turn = on_turn
        on_turn()
        step = self.script.pop(0) if self.script else answer("Default answer.")
        return await step(self, spec)


@pytest.fixture
def config(tmp_path, backend) -> Config:
    return Config(
        data_dir=tmp_path,
        backends=[BackendConfig(name="local", url=backend.url, model="m", kind="llamacpp")],
        channels=[
            ChannelConfig(id=SUPPORT, mode="support", initial_response=True),
            ChannelConfig(id=QUIET_FORUM, mode="support"),
            ChannelConfig(id=RECORD, mode="record"),
            ChannelConfig(id=MENTION, mode="mention"),
            ChannelConfig(id=SE1_FORUM, mode="support", initial_response=True, game="se1", prompt="Be extra nice."),
            ChannelConfig(id=SLOW_FORUM, mode="support", initial_response=True, initial_delay=DELAY),
        ],
        progress_interval=0.01,
        health_interval=0.01,
    )


@pytest.fixture
async def bot(config, chat):
    agent = FakeAgent()
    clangy = Clangy(config, chat, run_agent=agent)
    clangy.agent = agent  # type: ignore[attr-defined]
    yield clangy
    await clangy.close()


_ids = iter(range(100, 10**6))


def message(channel_id: int, text: str, parent_id: int | None = None, **kwargs) -> IncomingMessage:
    msg_id = kwargs.pop("id", next(_ids))
    return IncomingMessage(
        id=msg_id,
        channel_id=channel_id,
        parent_id=parent_id,
        guild_id=GUILD,
        author=kwargs.pop("author", "Alice"),
        text=text,
        created=datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc),
        **kwargs,
    )


def forum_post(forum: int, thread: int, text: str, **kwargs) -> IncomingMessage:
    kwargs.setdefault("thread_name", "Post")
    return message(thread, text, parent_id=forum, id=thread, starts_thread=True, **kwargs)


async def test_forum_post_gets_initial_answer(bot, chat):
    await bot.on_message(forum_post(SUPPORT, 1000, "My game crashes"))
    await bot.wait_idle()
    [reply] = chat.in_channel(1000)
    assert reply.content.startswith("⏳")
    assert reply.current == f"Default answer.\n\n{text.footer('Clangy')}"
    assert "Alice: My game crashes" in bot.agent.prompts[0]
    assert bot.conversations[1000].pending == []


async def test_quiet_forum_waits_for_mention_and_sends_everything(bot, chat):
    await bot.on_message(forum_post(QUIET_FORUM, 1001, "First post"))
    await bot.on_message(message(1001, "Me too", parent_id=QUIET_FORUM, author="Bob"))
    await bot.wait_idle()
    assert chat.messages == {}
    await bot.on_message(message(1001, "@Clangy help?", parent_id=QUIET_FORUM, mentions_bot=True))
    await bot.wait_idle()
    prompt = bot.agent.prompts[0]
    assert prompt.index("Alice: First post") < prompt.index("Bob: Me too") < prompt.index("Alice: @Clangy help?")


async def test_ignores_unconfigured_channels(bot, chat):
    await bot.on_message(message(1002, "@Clangy hi", parent_id=999, mentions_bot=True))
    await bot.on_message(message(999, "@Clangy hi", mentions_bot=True))
    await bot.on_message(message(MENTION, "no mention here"))
    await bot.wait_idle()
    assert chat.messages == {} and bot.conversations == {}


async def test_own_messages_are_skipped(bot, chat):
    await bot.on_message(forum_post(QUIET_FORUM, 1003, "Post"))
    await bot.on_message(message(1003, "bot text", parent_id=QUIET_FORUM, from_self=True))
    assert len(bot.conversations[1003].pending) == 1


async def test_mention_channel_starts_a_thread(bot, chat):
    await bot.on_message(message(MENTION, "@Clangy where is info.log?", id=777, mentions_bot=True))
    await bot.wait_idle()
    assert chat.threads == [(MENTION, 777, "@Clangy where is info.log?")]
    assert chat.in_channel(MENTION) == []
    assert chat.in_channel(777)[0].current.startswith("Default answer.")


async def test_messages_during_a_run_go_into_the_next_run(bot, chat):
    release = asyncio.Event()
    bot.agent.script = [blocked(release, answer("One")), answer("Two")]
    await bot.on_message(forum_post(SUPPORT, 1004, "Question"))
    await asyncio.sleep(0.05)
    await bot.on_message(message(1004, "More details", parent_id=SUPPORT))
    await bot.on_message(message(1004, "@Clangy and?", parent_id=SUPPORT, mentions_bot=True))
    release.set()
    await bot.wait_idle()
    first, second = bot.agent.prompts
    assert "Question" in first and "More details" not in first
    assert "More details" in second and "@Clangy and?" in second
    assert [m.current.split("\n")[0] for m in chat.in_channel(1004)] == ["One", "Two"]


async def test_progress_shows_the_queue_only_when_waiting(bot, chat, config):
    release = asyncio.Event()
    bot.agent.script = [blocked(release, answer("A")), answer("B")]
    await bot.on_message(forum_post(SUPPORT, 1005, "First"))
    await bot.on_message(forum_post(SUPPORT, 1006, "Second"))
    await asyncio.sleep(0.1)
    [running] = chat.in_channel(1005)
    assert running.current.startswith("⏳ Answer expected <t:") and running.current.endswith(":R>.")
    [waiting] = chat.in_channel(1006)
    assert waiting.content.startswith("⏳ Waiting for 1 other answer first. Answer expected <t:")
    release.set()
    await bot.wait_idle()


async def test_progress_says_when_it_takes_longer(bot, chat, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr("clangy.core.time.time", lambda: clock[0])
    release = asyncio.Event()

    async def slow(agent: FakeAgent, spec: RunSpec) -> RunResult:
        agent.on_turn()
        await release.wait()
        return await answer("Done.")(agent, spec)

    bot.agent.script = [slow]
    await bot.on_message(forum_post(SUPPORT, 1023, "Slow one"))
    await asyncio.sleep(0.05)
    [progress] = chat.in_channel(1023)
    assert progress.edits == [], "nothing to report while on time"
    clock[0] += 300
    await asyncio.sleep(0.05)
    assert progress.current.startswith("⏳ Taking longer than expected, deadline <t:")
    release.set()
    await bot.wait_idle()
    assert progress.current.startswith("Done.")
    record = json.loads((bot.conversation_dir(1023) / "metrics.jsonl").read_text())
    assert record["total_seconds"] == 300.0 and record["steps"] == 2
    assert (record["thread_id"], record["channel_id"], record["model"]) == (1023, SUPPORT, "m")
    assert record["answer_chars"] == len("Done.") and record["error"] is None
    assert record["requests"][0]["status"] == 200
    assert record["prompt_tokens"] == 100 and record["cached_tokens"] == 80
    assert {"queue_seconds", "run_seconds", "predicted_seconds"} <= record.keys()


async def test_long_answers_are_split(bot, chat):
    long_answer = "\n\n".join(f"Paragraph {i}. " + "x" * 900 for i in range(3))
    bot.agent.script = [answer(long_answer)]
    await bot.on_message(forum_post(SUPPORT, 1007, "Explain"))
    await bot.wait_idle()
    parts = [m.current for m in chat.in_channel(1007)]
    assert len(parts) == 2
    assert parts[0].startswith("Paragraph 0.") and parts[1].endswith(text.footer("Clangy"))


def pi_answer(reply: str) -> Step:
    """Like answer, but writes the session entry the way pi does."""

    async def step(agent: "FakeAgent", spec: RunSpec) -> RunResult:
        entry = {"type": "message", "message": {"role": "assistant", "content": [{"type": "text", "text": reply}]}}
        with open(spec.paths.session, "a") as f:
            f.write(json.dumps(entry) + "\n")
        return RunResult(answer=reply)

    return step


async def test_copied_turns_are_cut_from_the_answer_and_the_session(bot, chat):
    question = "Which executable do you start, Interim.exe or Legacy.exe?"
    bot.agent.script = [answer(question), pi_answer(f"{question}  \nAlice: Interim  \nSend me info.log, please.")]
    await bot.on_message(forum_post(SUPPORT, 1013, "SE1 crashes"))
    await bot.wait_idle()
    await bot.on_message(message(1013, "Interim", parent_id=SUPPORT, mentions_bot=True))
    await bot.wait_idle()
    assert chat.in_channel(1013)[-1].current.startswith("Send me info.log, please.")
    last = json.loads(bot.agent.specs[-1].paths.session.read_text().splitlines()[-1])
    assert last["message"]["content"] == [{"type": "text", "text": "Send me info.log, please."}]


async def test_timeout_keeps_messages_and_session(bot, chat):
    await bot.on_message(forum_post(SUPPORT, 1008, "Hello"))
    await bot.wait_idle()
    session = bot.agent.specs[0].paths.session
    before = session.read_text()
    bot.agent.script = [timeout]
    await bot.on_message(message(1008, "@Clangy again", parent_id=SUPPORT, mentions_bot=True))
    await bot.wait_idle()
    assert chat.in_channel(1008)[-1].current == text.timed_out("Clangy")
    assert session.read_text() == before
    assert [m.text for m in bot.conversations[1008].pending] == ["@Clangy again"]


async def test_backend_loss_retries_five_times_then_gives_up(bot, chat, backend):
    backend.status = 503
    await bot.on_message(forum_post(SUPPORT, 1009, "Crash"))
    await bot.wait_idle()
    conv = bot.conversations[1009]
    [progress] = chat.in_channel(1009)
    assert conv.status == "backend_error" and "retry 1/5" in progress.current
    assert len(conv.pending) == 1 and not bot.agent.specs[0].paths.session.exists()

    for attempt in range(1, 6):
        await bot.retry_failed()
        await bot.wait_idle()
        assert conv.retries == attempt
    assert conv.status == "failed"
    assert progress.current == text.backend_gave_up("Clangy")
    assert len(chat.in_channel(1009)) == 1

    await bot.retry_failed()
    await bot.wait_idle()
    assert len(bot.agent.specs) == 6, "no automatic retries after giving up"

    backend.status = 200
    await bot.on_message(message(1009, "@Clangy still there?", parent_id=SUPPORT, mentions_bot=True))
    await bot.wait_idle()
    assert conv.status == "idle" and conv.pending == []
    assert "Crash" in bot.agent.prompts[-1] and "still there?" in bot.agent.prompts[-1]


async def test_backend_recovery_answers_in_the_error_message(bot, chat, backend):
    backend.status = 503
    await bot.on_message(forum_post(SUPPORT, 1010, "Crash"))
    await bot.wait_idle()
    backend.status = 200
    await bot.retry_failed()
    await bot.wait_idle()
    [progress] = chat.in_channel(1010)
    assert any("retry 1/5" in edit for edit in progress.edits)
    assert progress.current.startswith("Default answer.")
    assert bot.conversations[1010].retries == 0


async def test_monitor_skips_unhealthy_backend(bot, backend):
    backend.status = 503
    await bot.on_message(forum_post(SUPPORT, 1011, "Crash"))
    await bot.wait_idle()
    await backend.stop()
    await bot.retry_failed()
    assert bot.conversations[1011].retries == 0


def linked(name: str, then: Step) -> Step:
    """Checks that the work folder link exists when the next step starts, like a read by the model would."""

    async def step(agent: "FakeAgent", spec: RunSpec) -> RunResult:
        agent.linked.append((spec.paths.work / name).is_dir())
        return await then(agent, spec)

    return step


async def test_set_game_links_knowledge_within_the_run(bot, chat, config):
    knowledge = config.data_dir / "knowledge" / "releases" / "r1" / "se2" / "knowledge" / "skills"
    knowledge.mkdir(parents=True)
    (config.data_dir / "knowledge" / "current").symlink_to("releases/r1")
    config.knowledge["se2"] = [KnowledgeSource(url="https://example.invalid/skills2.git", link="knowledge/skills")]
    bot.agent.script = [
        linked("knowledge/skills", tool("set_game", {"game": "se2"}, linked("knowledge/skills", answer("SE2 answer"))))
    ]
    await bot.on_message(forum_post(SUPPORT, 1012, "SE2 crashes on start"))
    await bot.wait_idle()
    (spec,) = bot.agent.specs
    assert knowledge.resolve() in spec.read_only, "readable before the game is known"
    assert bot.agent.linked == [False, True]
    status, reply = bot.agent.tool_results[0]
    assert status == 200 and reply["text"].startswith("Recorded. This conversation is about SE2. Its knowledge is")
    conv = bot.conversations[1012]
    assert conv.game == "se2" and conv.host_notes == [] and conv.pending == []
    assert chat.in_channel(1012)[0].current.startswith("SE2 answer")


async def test_failed_run_after_set_game_tells_the_next_run(bot, chat):
    bot.agent.script = [tool("set_game", {"game": "se1"}, timeout), answer("SE1 answer")]
    await bot.on_message(forum_post(SUPPORT, 1014, "Crash"))
    await bot.wait_idle()
    conv = bot.conversations[1014]
    assert conv.game == "se1" and conv.host_notes[-1].startswith("[host] This conversation is about SE1.")
    await bot.on_message(message(1014, "@Clangy again", parent_id=SUPPORT, mentions_bot=True))
    await bot.wait_idle()
    assert "[host] This conversation is about SE1." in bot.agent.prompts[-1]
    assert conv.host_notes == []


async def test_knowledge_folders_in_place_for_the_game(bot, config, tmp_path):
    folders = {name: tmp_path / "srv" / name for name in ("skills", "skills2", "se-dev", "books")}
    for folder in folders.values():
        folder.mkdir(parents=True)
    config.knowledge["se1"] = [
        KnowledgeSource(path=folders["skills"], link="knowledge/skills"),
        KnowledgeSource(path=folders["books"], link="knowledge/books"),
        KnowledgeSource(path=folders["se-dev"], link=".se-dev"),
    ]
    config.knowledge["se2"] = [KnowledgeSource(path=folders["skills2"], link="knowledge/skills")]
    await bot.on_message(forum_post(SE1_FORUM, 1024, "Help"))
    await bot.wait_idle()
    spec = bot.agent.specs[0]
    work = spec.paths.work
    assert (work / "knowledge" / "skills").resolve() == folders["skills"]
    assert (work / ".se-dev").resolve() == folders["se-dev"]
    assert set(folders.values()) <= set(spec.read_only), "both games, so set_game needs no new sandbox"
    assert not (work / "knowledge" / "skills2").exists()

    # Switching the game swaps the links and drops the other game's extras
    bot.agent.script = [tool("set_game", {"game": "se2"}, answer("SE2 then."))]
    await bot.on_message(message(1024, "@Clangy actually SE2", parent_id=SE1_FORUM, mentions_bot=True))
    await bot.wait_idle()
    assert (work / "knowledge" / "skills").resolve() == folders["skills2"]
    assert not (work / ".se-dev").exists() and not (work / "knowledge" / "books").exists()
    assert len(bot.agent.specs) == 2


async def test_channel_game_and_prompt(bot, config):
    await bot.on_message(forum_post(SE1_FORUM, 1013, "Help"))
    await bot.wait_idle()
    spec = bot.agent.specs[0]
    assert "This conversation is about SE1" in spec.prompt
    system = spec.paths.system_prompt.read_text()
    assert system.endswith("# Channel\nBe extra nice.\n")
    assert "Current date" not in system


async def test_system_prompt_is_stable_between_runs(bot):
    await bot.on_message(forum_post(SUPPORT, 1014, "One"))
    await bot.wait_idle()
    first = bot.agent.specs[0].paths.system_prompt.read_text()
    await bot.on_message(message(1014, "@Clangy two", parent_id=SUPPORT, mentions_bot=True))
    await bot.wait_idle()
    assert bot.agent.specs[1].paths.system_prompt.read_text() == first


async def test_ticket_tools_link_thread_and_keep_files(bot, chat, config):
    chat.files["https://cdn/info.log"] = b"log data"

    async def analyze(agent: FakeAgent, spec: RunSpec) -> RunResult:
        (spec.paths.work / "out").mkdir()
        (spec.paths.work / "out" / "plot.png").write_bytes(b"png")
        files = [
            {"path": "attachments/1015-info.log", "about": "Pulsar log"},
            {"path": "out/plot.png", "about": "Frame times"},
        ]
        return await tool(
            "ticket_create", {"title": "Crash on load", "body": "Details", "files": files}, answer("Filed.")
        )(agent, spec)

    bot.agent.script = [
        analyze,
        tool("ticket_add_report", {"id": "DIS-0001", "body": "Again"}, answer("Added.")),
        tool("ticket_create", {"title": "", "body": "x"}, answer("Oops.")),
        tool(
            "note_create",
            {"title": "Leak", "body": "x", "files": [{"path": ".pi/session.jsonl", "about": "s"}]},
            answer("No."),
        ),
    ]
    attachment = Attachment(1, "info.log", 8, "https://cdn/info.log")
    await bot.on_message(forum_post(SE1_FORUM, 1015, "Crash", attachments=[attachment]))
    await bot.wait_idle()
    work = bot.agent.specs[0].paths.work
    assert (work / "attachments" / "1015-info.log").read_bytes() == b"log data"
    assert "[host] Attachment from Alice: attachments/1015-info.log (1 KB)" in bot.agent.prompts[0]
    workspace = config.data_dir / "workspace"
    assert (work / "store" / "tickets").resolve() == (workspace / "tickets").resolve()
    ticket = (workspace / "tickets" / "DIS-0001.md").read_text()
    assert f"thread: https://discord.com/channels/{GUILD}/1015\n" in ticket
    assert "game: se1\n" in ticket and "notes: N-0001\n" in ticket and "files:" not in ticket
    note = workspace / "notes" / "N-0001"
    assert (note / "1015-info.log").read_bytes() == b"log data" and (note / "plot.png").read_bytes() == b"png"
    listing = (note / "NOTE.md").read_text()
    assert "ticket: DIS-0001\n" in listing
    assert "- `1015-info.log`: Pulsar log (posted by Alice, " in listing
    assert "- `plot.png`: Frame times (written by Clangy)" in listing
    for prompt in ("@Clangy same", "@Clangy bad", "@Clangy leak"):
        await bot.on_message(message(1015, prompt, parent_id=SE1_FORUM, mentions_bot=True))
        await bot.wait_idle()
    assert bot.agent.tool_results[1] == (200, {"text": "Added the report to DIS-0001."})
    assert bot.agent.tool_results[2] == (400, {"error": "title is empty"})
    assert bot.agent.tool_results[3] == (
        400,
        {"error": ".pi/session.jsonl is neither an attachment nor in your work folder"},
    )
    assert not (workspace / "notes" / "N-0002").exists()
    log = subprocess.run(
        ["git", f"--git-dir={bot.store.history.git_dir}", "log", "--format=%B"], capture_output=True, text=True
    ).stdout
    assert (
        f"Create DIS-0001: Crash on load\n\nDiscord user: Alice\nThread: https://discord.com/channels/{GUILD}/1015"
        in log
    )


async def test_files_are_sent_once_and_retained(bot, chat, config):
    async def produce(agent: FakeAgent, spec: RunSpec) -> RunResult:
        out = spec.paths.work / "out"
        out.mkdir(exist_ok=True)
        for name in ("a.txt", "b.txt", "c.txt", "d.txt", "summary.md"):
            (out / name).write_text(name)
        return await tool(
            "attach_files",
            {"paths": [f"out/{n}.txt" for n in "abcd"], "archive": "parts"},
            tool("attach_files", {"paths": ["out/summary.md"]}, answer("Here you go.")),
        )(agent, spec)

    chat.files["https://cdn/info.log"] = b"log data"
    bot.agent.script = [
        produce,
        tool("attach_files", {"paths": ["out/summary.md"]}, answer("Again.")),
        tool("attach_files", {"paths": ["attachments/1020-info.log"]}, answer("Echo.")),
    ]
    attachment = Attachment(1, "info.log", 8, "https://cdn/info.log")
    await bot.on_message(forum_post(SUPPORT, 1020, "Send me files", attachments=[attachment]))
    await bot.wait_idle()
    [reply] = chat.in_channel(1020)
    assert reply.filenames == ["parts.zip", "summary.md"]
    assert reply.current.startswith("Here you go.")
    files = config.data_dir / "conversations" / "1020" / "files"
    assert (files / "received" / "1020-info.log").read_bytes() == b"log data"
    assert sorted(p.name for p in (files / "sent" / "0001").iterdir()) == ["parts.zip", "summary.md"]
    assert bot.conversations[1020].sent_files == ["out/a.txt", "out/b.txt", "out/c.txt", "out/d.txt", "out/summary.md"]
    assert (bot.agent.specs[0].paths.work / "attachments").resolve() in bot.agent.specs[0].read_only

    for prompt in ("@Clangy again", "@Clangy echo"):
        await bot.on_message(message(1020, prompt, parent_id=SUPPORT, mentions_bot=True))
        await bot.wait_idle()
    assert bot.agent.tool_results[2][0] == 400 and "already sent" in bot.agent.tool_results[2][1]["error"]
    assert bot.agent.tool_results[3][0] == 400 and "outside your work folder" in bot.agent.tool_results[3][1]["error"]
    assert all(m.filenames == [] for m in chat.in_channel(1020)[1:])


async def test_files_of_a_failed_run_are_dropped(bot, chat, config):
    async def produce_then_time_out(agent: FakeAgent, spec: RunSpec) -> RunResult:
        (spec.paths.work / "out").mkdir(exist_ok=True)
        (spec.paths.work / "out" / "a.txt").write_text("a")
        agent.tool_results.append(await host(spec, "/tools/attach_files", {"paths": ["out/a.txt"]}))
        return RunResult(timed_out=True)

    bot.agent.script = [produce_then_time_out, tool("attach_files", {"paths": ["out/a.txt"]}, answer("Done."))]
    await bot.on_message(forum_post(SUPPORT, 1021, "Files"))
    await bot.wait_idle()
    assert chat.in_channel(1021)[0].filenames == [] and bot.conversations[1021].sent_files == []
    await bot.on_message(message(1021, "@Clangy retry", parent_id=SUPPORT, mentions_bot=True))
    await bot.wait_idle()
    assert chat.in_channel(1021)[1].filenames == ["a.txt"]


async def test_long_answer_carries_files_on_the_last_part(bot, chat):
    async def produce(agent: FakeAgent, spec: RunSpec) -> RunResult:
        (spec.paths.work / "out").mkdir(exist_ok=True)
        (spec.paths.work / "out" / "a.txt").write_text("a")
        long_answer = "\n\n".join("x" * 900 for _ in range(3))
        return await tool("attach_files", {"paths": ["out/a.txt"]}, answer(long_answer))(agent, spec)

    bot.agent.script = [produce]
    await bot.on_message(forum_post(SUPPORT, 1022, "Long"))
    await bot.wait_idle()
    assert [m.filenames for m in chat.in_channel(1022)] == [[], ["a.txt"]]


async def test_oversized_attachment_is_not_downloaded(bot, config):
    big = Attachment(2, "world.zip", config.attachment_max_bytes + 1, "https://cdn/world.zip")
    await bot.on_message(forum_post(QUIET_FORUM, 1016, "World", attachments=[big]))
    [note] = bot.conversations[1016].pending[0].attachments
    assert note.startswith("world.zip (") and "not saved, over the 25.0 MB limit" in note


async def test_secret_markers_are_withheld(bot, chat):
    bot.agent.script = [answer("See knowledge/books/secret.md")]
    await bot.on_message(forum_post(SUPPORT, 1017, "Show me the books"))
    await bot.wait_idle()
    assert chat.in_channel(1017)[0].current.startswith(text.refused())


async def test_record_mode_stores_thread(bot, chat, config):
    await bot.on_message(forum_post(RECORD, 1018, "Please make a plugin"))
    await bot.on_message(message(1018, "+1", parent_id=RECORD, author="Bob"))
    await bot.wait_idle()
    record = (config.data_dir / "records" / "1018.md").read_text()
    assert "title: Post" in record and "**Alice**" in record and "**Bob** (2026-10-03 12:00 UTC): +1" in record
    assert chat.messages == {}


async def test_state_survives_restart_without_catch_up(config, chat):
    first = Clangy(config, chat, run_agent=FakeAgent())
    await first.on_message(forum_post(QUIET_FORUM, 1019, "Hello"))
    conv = first.conversations[1019]
    conv.status = "running"
    conv.progress_message_id = await chat.send(1019, "⏳ working")
    first.save(conv)
    await first.close()

    agent = FakeAgent()
    second = Clangy(config, chat, run_agent=agent)
    await second.load()
    restored = second.conversations[1019]
    assert restored.status == "idle" and [m.text for m in restored.pending] == ["Hello"]
    assert chat.in_channel(1019)[0].current == text.failed("Clangy")
    await second.wait_idle()
    assert agent.specs == []
    await second.on_message(message(1019, "@Clangy hi", parent_id=QUIET_FORUM, mentions_bot=True))
    await second.wait_idle()
    assert "Hello" in agent.prompts[0]
    await second.close()


async def test_first_answer_waits_until_the_thread_is_quiet(bot, chat):
    await bot.on_message(forum_post(SLOW_FORUM, 1030, "My game crashes"))
    await asyncio.sleep(DELAY * 0.6)
    await bot.on_message(message(1030, "Forgot: it's SE2", parent_id=SLOW_FORUM))
    await asyncio.sleep(DELAY * 0.6)
    assert bot.agent.specs == [], "a new message restarts the wait"
    bot.on_edit(message(1030, "My game crashes on load", parent_id=SLOW_FORUM, id=1030))
    await asyncio.sleep(DELAY * 0.6)
    assert bot.agent.specs == [], "an edit restarts the wait too"
    assert chat.messages == {}, "nothing is posted while waiting"
    await bot.wait_idle()
    [prompt] = bot.agent.prompts
    assert "Alice: My game crashes on load" in prompt and "Forgot: it's SE2" in prompt


async def test_mention_skips_the_first_answer_wait(bot, chat):
    await bot.on_message(forum_post(SLOW_FORUM, 1031, "Question"))
    await bot.on_message(message(1031, "@Clangy now please", parent_id=SLOW_FORUM, mentions_bot=True))
    await bot.wait_idle()
    await asyncio.sleep(DELAY * 1.5)
    await bot.wait_idle()
    assert len(bot.agent.specs) == 1, "the mention's answer replaces the delayed first answer"


async def test_mentions_are_never_delayed(bot):
    await bot.on_message(forum_post(SLOW_FORUM, 1032, "Question"))
    await bot.wait_idle()
    await bot.on_message(message(1032, "@Clangy more", parent_id=SLOW_FORUM, mentions_bot=True))
    await asyncio.sleep(0.05)
    assert len(bot.agent.specs) == 2


async def test_edits_and_deletions_apply_to_the_next_run(bot, chat):
    await bot.on_message(forum_post(QUIET_FORUM, 1033, "Original question", author="Alice"))
    await bot.on_message(message(1033, "typo here", parent_id=QUIET_FORUM, id=7001, author="Bob"))
    await bot.on_message(message(1033, "spam", parent_id=QUIET_FORUM, id=7002, author="Bob"))
    # Still queued: edited and deleted in place
    bot.on_edit(message(1033, "fixed text", parent_id=QUIET_FORUM, id=7001, author="Bob"))
    bot.on_delete(7002)
    await bot.on_message(message(1033, "@Clangy go", parent_id=QUIET_FORUM, mentions_bot=True))
    await bot.wait_idle()
    first = bot.agent.prompts[0]
    assert (
        "Bob: fixed text" in first and "typo here" not in first and "spam" not in first and first.count("[host]") == 2
    )

    # Already seen by the model: the next run gets notes
    bot.on_edit(message(1033, "Original question, now with details", parent_id=QUIET_FORUM, id=1033))
    bot.on_delete(7001)
    bot.on_edit(message(1033, "unchanged", parent_id=QUIET_FORUM, id=9999))
    await bot.on_message(message(1033, "@Clangy again", parent_id=QUIET_FORUM, mentions_bot=True))
    await bot.wait_idle()
    second = bot.agent.prompts[1]
    assert (
        "Alice edited their message from 2026-10-03 12:00 UTC, it now reads: Original question, now with details"
        in second
    )
    assert 'Bob deleted their message from 2026-10-03 12:00 UTC ("fixed text"). Disregard only that message' in second
    third_notes = bot.conversations[1033].host_notes
    assert third_notes == [], "notes are delivered once"


async def test_changes_during_a_run_dont_stop_it(bot, chat):
    release = asyncio.Event()
    bot.agent.script = [blocked(release, answer("One")), answer("Two")]
    await bot.on_message(forum_post(SUPPORT, 1034, "Question v1"))
    await asyncio.sleep(0.05)
    bot.on_edit(message(1034, "Question v2", parent_id=SUPPORT, id=1034))
    release.set()
    await bot.wait_idle()
    assert "Question v1" in bot.agent.prompts[0]
    assert chat.in_channel(1034)[0].current.startswith("One")
    await bot.on_message(message(1034, "@Clangy and?", parent_id=SUPPORT, mentions_bot=True))
    await bot.wait_idle()
    assert "it now reads: Question v2" in bot.agent.prompts[1]


async def test_own_and_unknown_edits_are_ignored(bot):
    await bot.on_message(forum_post(QUIET_FORUM, 1035, "Question"))
    bot.on_edit(message(1035, "changed", parent_id=QUIET_FORUM, id=1035, from_self=True))
    bot.on_edit(message(1035, "changed", parent_id=QUIET_FORUM, id=4242))
    bot.on_delete(4243)
    conv = bot.conversations[1035]
    assert conv.pending[0].text == "Question" and conv.host_notes == ["[host] The title of the forum post: Post"]


async def test_record_mode_logs_edits_and_deletions(bot, config):
    await bot.on_message(forum_post(RECORD, 1036, "Make a plugin"))
    bot.on_edit(message(1036, "Make a plugin for X", parent_id=RECORD, id=1036))
    bot.on_delete(1036)
    record = (config.data_dir / "records" / "1036.md").read_text()
    assert "edited a message, it now reads: Make a plugin for X" in record and "deleted their message" in record


async def test_edits_still_work_after_restart(config, chat):
    first = Clangy(config, chat, run_agent=FakeAgent())
    await first.on_message(forum_post(QUIET_FORUM, 1037, "Question"))
    await first.close()
    second = Clangy(config, chat, run_agent=FakeAgent())
    await second.load()
    second.on_edit(message(1037, "Better question", parent_id=QUIET_FORUM, id=1037))
    assert second.conversations[1037].pending[0].text == "Better question"
    await second.close()


async def test_mention_brings_the_recent_channel_chat_along(bot, chat):
    chat.files["https://cdn/fox.png"] = b"png"
    chat.channel_history[MENTION] = [
        message(MENTION, "Little", id=601),
        message(MENTION, "an earlier answer", id=602, from_self=True),
        message(
            MENTION, "brown", id=603, author="Bob", attachments=[Attachment(9, "fox.png", 3, "https://cdn/fox.png")]
        ),
        message(MENTION, "fox", id=604),
    ]
    await bot.on_message(message(MENTION, "@Clangy Do we have this animal in SE1?", id=605, mentions_bot=True))
    await bot.wait_idle()
    [(channel_id, before_id, limit, after)] = chat.history_calls
    assert (channel_id, before_id, limit) == (MENTION, 605, 100)
    assert after == datetime(2026, 10, 3, 0, 0, tzinfo=timezone.utc), "since midnight"
    prompt = bot.agent.prompts[0]
    lines = [line for line in prompt.splitlines() if not line.startswith("[host] 20")]
    assert lines == [
        "[host] Earlier messages, for reference only. Don't answer them; "
        "use them only where the messages after them refer to them:",
        "Alice: Little",
        "Bob: brown",
        "[host] Attachment from Bob: attachments/603-fox.png (1 KB)",
        "Alice: fox",
        "[host] End of the earlier messages.",
        "Alice: @Clangy Do we have this animal in SE1?",
    ]
    bot.on_edit(message(MENTION, "Big", id=601))
    assert "it now reads: Big" in bot.conversations[605].host_notes[-1], "context messages are tracked for edits"


async def test_context_reaches_back_four_hours_around_midnight(bot, chat):
    late = message(MENTION, "@Clangy hi", id=606, mentions_bot=True)
    late.created = datetime(2026, 10, 4, 1, 30, tzinfo=timezone.utc)
    await bot.on_message(late)
    await bot.wait_idle()
    assert chat.history_calls[0][3] == datetime(2026, 10, 3, 21, 30, tzinfo=timezone.utc)


async def test_mention_in_an_unknown_thread_starts_with_its_history(bot, chat):
    chat.channel_history[1040] = [
        message(1040, "Old forum post", parent_id=QUIET_FORUM, id=1040, author="Carol"),
        message(1040, "v1 answer", parent_id=QUIET_FORUM, id=1041, from_self=True),
        message(1040, "Still broken", parent_id=QUIET_FORUM, id=1042, author="Carol"),
    ]
    await bot.on_message(message(1040, "@Clangy any idea?", parent_id=QUIET_FORUM, id=1043, mentions_bot=True))
    await bot.wait_idle()
    assert chat.history_calls == [(1040, 1043, None, None)], "all of the thread, however old and long"
    prompt = bot.agent.prompts[0]
    assert prompt.index("Carol: Old forum post") < prompt.index("Carol: Still broken") < prompt.index("any idea?")
    assert "v1 answer" not in prompt
    assert chat.in_channel(1040)[0].current.startswith("Default answer.")


async def test_thread_started_from_a_channel_message_brings_that_message(bot, chat):
    starter = message(MENTION, "Is there a plugin for this?", id=2000, author="Bob")
    chat.channel_history[2000] = [
        message(2000, "", parent_id=MENTION, id=2001, author="Bob", replied_to=starter),
        message(2000, "Me too", parent_id=MENTION, id=2002),
    ]
    await bot.on_message(message(2000, "@Clangy do you know?", parent_id=MENTION, id=2003, mentions_bot=True))
    await bot.wait_idle()
    lines = [line for line in bot.agent.prompts[0].splitlines() if not line.startswith("[host]")]
    assert lines == ["Bob: Is there a plugin for this?", "Alice: Me too", "Alice: @Clangy do you know?"]


async def test_replies_bring_older_messages_along(bot, chat):
    def old(text: str, message_id: int, author: str) -> IncomingMessage:
        m = message(MENTION, text, id=message_id, author=author)
        m.created -= timedelta(days=1)
        return m

    plugin, crash = old("Which plugin hides the HUD?", 500, "Bob"), old("My game crashes on load", 501, "Carol")
    own = old("an earlier answer", 502, "Clangy")
    own.from_self = True
    chat.channel_history[MENTION] = [
        old("Too old and nobody replies to it", 499, "Dave"),
        message(MENTION, "That one, Hide HUD", id=620, author="Dave", replied_to=plugin),
        message(MENTION, "thanks", id=621, author="Bob", replied_to=own),
    ]
    await bot.on_message(message(MENTION, "@Clangy still?", id=622, mentions_bot=True, replied_to=crash))
    await bot.wait_idle()
    lines = [line for line in bot.agent.prompts[0].splitlines() if not line.startswith("[host]")]
    assert lines == [
        "Bob: Which plugin hides the HUD?",
        "Carol: My game crashes on load",
        "Dave (reply to Bob): That one, Hide HUD",
        "Bob: thanks",  # Clangy's own messages aren't cached, nor brought along
        "Alice (reply to Carol): @Clangy still?",
    ]


async def test_known_thread_reads_what_was_posted_while_offline(config, chat):
    first = Clangy(config, chat, run_agent=FakeAgent())
    await first.on_message(forum_post(QUIET_FORUM, 1045, "Question"))
    await first.close()
    chat.channel_history[1045] = [
        forum_post(QUIET_FORUM, 1045, "Question"),
        message(1045, "Missed", parent_id=QUIET_FORUM, id=1046, author="Bob"),
        message(1045, "own answer", parent_id=QUIET_FORUM, id=1047, from_self=True),
        message(1045, "Missed too", parent_id=QUIET_FORUM, id=1048, author="Bob"),
    ]
    agent = FakeAgent()
    second = Clangy(config, chat, run_agent=agent)
    await second.load()
    await second.on_message(message(1045, "Any news?", parent_id=QUIET_FORUM, id=1049))
    await second.on_message(message(1045, "@Clangy hello?", parent_id=QUIET_FORUM, id=1050, mentions_bot=True))
    await second.wait_idle()
    assert chat.history_calls == [(1045, 1049, None, None)], "all of the thread"
    lines = [line for line in agent.prompts[0].splitlines() if not line.startswith("[host]")]
    assert lines == ["Alice: Question", "Bob: Missed", "Bob: Missed too", "Alice: Any news?", "Alice: @Clangy hello?"]
    await second.close()


async def test_known_thread_reads_the_messages_it_never_had(config, chat):
    first = Clangy(config, chat, run_agent=FakeAgent())
    # An older version read none of the thread before the mention
    await first.on_message(message(1170, "@Clangy Linux only?", parent_id=QUIET_FORUM, id=1172, mentions_bot=True))
    await first.wait_idle()
    [answer] = chat.in_channel(1170)
    await first.close()
    post = message(1170, "Laser antennas don't work", parent_id=QUIET_FORUM, id=1170, author="Carol")
    post.created -= timedelta(days=6)
    chat.channel_history[1170] = [
        post,
        message(1170, "@Clangy Linux only?", parent_id=QUIET_FORUM, id=1172),
        message(1170, answer.current, parent_id=QUIET_FORUM, id=answer.message_id, author="Clangy", from_self=True),
    ]
    agent = FakeAgent()
    second = Clangy(config, chat, run_agent=agent)
    await second.load()
    await second.on_message(message(1170, "@Clangy check the code", parent_id=QUIET_FORUM, id=1173, mentions_bot=True))
    await second.wait_idle()
    lines = [line for line in agent.prompts[0].splitlines() if not line.startswith("[host] 2026")]
    assert lines == [
        "[host] Earlier messages, for reference only. Don't answer them; "
        "use them only where the messages after them refer to them:",
        "Carol: Laser antennas don't work",
        "[host] End of the earlier messages.",
        "Alice: @Clangy check the code",
    ], "the session still has the mention and the answer"
    await second.close()


async def test_lost_session_brings_the_known_conversation_back(config, chat):
    first = Clangy(config, chat, run_agent=FakeAgent())
    await first.on_message(forum_post(SE1_FORUM, 1175, "My game crashes", author="Carol"))
    await first.wait_idle()
    [answer] = chat.in_channel(1175)
    await first.close()
    (config.data_dir / "conversations" / "1175" / "work" / ".pi" / "session.jsonl").unlink()
    chat.channel_history[1175] = [
        message(1175, "My game crashes", parent_id=SE1_FORUM, id=1175, author="Carol"),
        message(1175, answer.current, parent_id=SE1_FORUM, id=answer.message_id, author="Clangy", from_self=True),
    ]

    agent = FakeAgent()
    second = Clangy(config, chat, run_agent=agent)
    await second.load()
    assert [m.text for m in second.conversations[1175].pending] == ["My game crashes", "Default answer."]
    mention = message(1175, "@Clangy still?", parent_id=SE1_FORUM, author="Carol", mentions_bot=True)
    await second.on_message(replace(mention, thread_name="Post"))
    await second.wait_idle()
    prompt = agent.prompts[0]
    assert core.knowledge_note_start("se1") in prompt and "title of the forum post: Post" in prompt
    lines = [line for line in prompt.splitlines() if line.startswith(("Carol", "Clangy"))]
    assert lines == ["Carol: My game crashes", "Clangy (you): Default answer.", "Carol: @Clangy still?"]
    assert prompt.index("Carol: My game crashes") < prompt.index("End of the earlier messages")
    await second.close()


async def test_unknown_threads_without_mention_stay_ignored(bot, chat):
    await bot.on_message(message(1050, "just chatting", parent_id=QUIET_FORUM, id=1051))
    assert bot.conversations == {} and chat.history_calls == []


async def test_no_answer_without_context(bot, chat):
    chat.unreadable.add(MENTION)
    await bot.on_message(message(MENTION, "@Clangy what about this?", id=607, mentions_bot=True))
    await bot.wait_idle()
    assert bot.conversations == {} and chat.threads == [] and chat.messages == {}


async def test_context_comes_from_the_cache_after_catching_up(bot, chat):
    def recent(text: str, message_id: int, minutes_ago: int, **kwargs) -> IncomingMessage:
        # Timestamps at creation time, like real messages, which arrive after the catch-up
        m = message(MENTION, text, id=message_id, **kwargs)
        m.created = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return m

    chat.channel_history[MENTION] = [recent("before the restart", 701, 30), recent("bot", 702, 29, from_self=True)]
    await bot.catch_up()
    assert len(chat.history_calls) == 1 and chat.history_calls[0][1] is None
    await bot.on_message(recent("Little", 703, 3))
    await bot.on_message(recent("brown", 704, 2))
    await bot.on_message(recent("typo", 705, 1))
    bot.on_edit(recent("Little!", 703, 3))
    bot.on_delete(705)
    await bot.on_message(recent("@Clangy what is it?", 706, 0, mentions_bot=True))
    await bot.wait_idle()
    assert len(chat.history_calls) == 1, "no Discord request at mention time"
    lines = [line for line in bot.agent.prompts[0].splitlines() if not line.startswith("[host]")]
    assert lines == ["Alice: before the restart", "Alice: Little!", "Alice: brown", "Alice: @Clangy what is it?"]
    assert bot.agent.prompts[0].index("[host] End of the earlier messages.") < bot.agent.prompts[0].index("what is it?")


async def test_dead_attachment_links_become_a_note(bot, chat):
    dead = Attachment(3, "old.png", 10, "https://cdn/expired")
    await bot.on_message(forum_post(QUIET_FORUM, 1060, "Picture", attachments=[dead]))
    assert bot.conversations[1060].pending[0].attachments == ["old.png could not be downloaded"]


async def test_mounts_are_in_every_conversation(bot, config, tmp_path):
    repos = tmp_path / "repos"
    (repos / "CometWorks" / "magnetar").mkdir(parents=True)
    config.mounts.append(KnowledgeSource(path=repos, link="repos"))
    await bot.on_message(forum_post(QUIET_FORUM, 1070, "Question"))
    await bot.on_message(message(1070, "@Clangy hi", parent_id=QUIET_FORUM, mentions_bot=True))
    await bot.wait_idle()
    spec = bot.agent.specs[0]
    assert (spec.paths.work / "repos" / "CometWorks" / "magnetar").is_dir()
    assert repos in spec.read_only
    bot.agent.script = [tool("set_game", {"game": "se1"}, answer("SE1 then."))]
    await bot.on_message(message(1070, "@Clangy it's SE1", parent_id=QUIET_FORUM, mentions_bot=True))
    await bot.wait_idle()
    assert (spec.paths.work / "repos").is_dir(), "mounts stay when the game is set"


async def test_knowledge_note_lists_the_skills(bot, config, tmp_path):
    skills = tmp_path / "srv" / "skills"
    for name, description in (("se-dev", "Overview"), ("se-dev-game-code", "Decompiled game code")):
        (skills / name).mkdir(parents=True)
        (skills / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\nlicense: MIT\n---\n# Body\n"
        )
    books = tmp_path / "srv" / "books"
    (books / "se-dev-game-book").mkdir(parents=True)
    (books / "se-dev-game-book" / "SKILL.md").write_text('---\nname: se-dev-game-book\ndescription: "Handbook"\n---\n')
    (skills / "se-dev-plugin-review").mkdir()
    (skills / "se-dev-plugin-review" / "SKILL.md").write_text(
        "---\nname: se-dev-plugin-review\ndescription: Review\n---\n"
    )
    sdk = tmp_path / "srv" / "sdk"
    (sdk / "se-dev-plugin-sdk").mkdir(parents=True)
    (sdk / "se-dev-plugin-sdk" / "SKILL.md").write_text(
        "---\nname: se-dev-plugin-sdk\ndescription: |\n  Magnetar's PluginSdk.\n  For server plugins.\nlicense: MIT\n---\n"
    )
    config.knowledge["se1"] = [
        KnowledgeSource(path=skills, link="knowledge/skills"),
        KnowledgeSource(path=books, link="knowledge/books"),
    ]
    config.mounts = [KnowledgeSource(path=sdk, link="knowledge/sdk")]
    config.hidden_skills = ["se-dev-plugin-review"]
    bot.agent.script = [tool("set_game", {"game": "se1"}, answer("SE1 answer."))]
    await bot.on_message(forum_post(SUPPORT, 1080, "Do we have foxes in SE1?"))
    await bot.wait_idle()
    assert bot.agent.specs[0].read_only.count(sdk) == 1, "a mount is there before the game is known"
    second = bot.agent.tool_results[0][1]["text"]
    assert second.startswith("Recorded. This conversation is about SE1. Its knowledge is under knowledge/.")
    assert "- se-dev: Overview (knowledge/skills/se-dev/SKILL.md)" in second
    assert "- se-dev-game-code: Decompiled game code (knowledge/skills/se-dev-game-code/SKILL.md)" in second
    assert "- se-dev-game-book: Handbook (knowledge/books/se-dev-game-book/SKILL.md)" in second
    assert (
        "- se-dev-plugin-sdk: Magnetar's PluginSdk. For server plugins. (knowledge/sdk/se-dev-plugin-sdk/SKILL.md)"
        in second
    )
    assert "se-dev-plugin-review" not in second, "a hidden skill isn't listed"
    assert bot.conversations[1080].host_notes == []


async def test_reference_only_for_the_first_run(bot, chat):
    chat.channel_history[MENTION] = [message(MENTION, "Do we have foxes?", id=611)]
    await bot.on_message(message(MENTION, "@Clangy Help, my game crashed!", id=612, mentions_bot=True))
    await bot.wait_idle()
    first = bot.agent.prompts[0]
    assert first.index("Do we have foxes?") < first.index("End of the earlier messages") < first.index("crashed!")
    await bot.on_message(message(612, "@Clangy it crashes on load", parent_id=MENTION, mentions_bot=True))
    await bot.wait_idle()
    assert "for reference" not in bot.agent.prompts[1], "messages in Clangy's thread are the conversation"


@pytest.fixture
async def duo(config, chat, backend2):
    """A llama.cpp server (local) and a cloud API (cloud)."""
    config.backends.append(BackendConfig(name="cloud", url=backend2.url, model="c"))
    agent = FakeAgent()
    clangy = Clangy(config, chat, run_agent=agent)
    clangy.agent = agent  # type: ignore[attr-defined]
    yield clangy
    await clangy.close()


def metrics(bot: Clangy, thread: int) -> list[dict]:
    return [json.loads(line) for line in (bot.conversation_dir(thread) / "metrics.jsonl").read_text().splitlines()]


async def test_failover_answers_right_away_from_another_backend(duo, chat, backend, backend2):
    backend.status = 503
    backend2.answering_model = "c-fallback"
    await duo.on_message(forum_post(SUPPORT, 1100, "Crash"))
    await duo.wait_idle()
    conv = duo.conversations[1100]
    assert conv.backend == "cloud" and conv.status == "idle" and conv.pending == []
    [progress] = chat.in_channel(1100)
    assert progress.current.startswith("Default answer.")
    assert not any("retry" in edit or "Lost" in edit for edit in progress.edits), "no warning"
    assert conv.retries == conv.failovers == 0
    first, second = metrics(duo, 1100)
    assert first["backend"] == "local" and first["backend_switch"] == {"from": None, "reason": "new"}
    assert first["backend_failed"] and not first["ok"]
    assert second["backend"] == "cloud" and second["backend_switch"] == {"from": "local", "reason": "failure"}
    assert second["answered_by"] == ["c-fallback"]
    assert not duo.backends["local"].healthy


async def test_failed_backends_wait_for_health_or_cooldown(duo, chat, backend, backend2):
    backend.status = 503
    await duo.on_message(forum_post(SUPPORT, 1101, "Crash"))
    await duo.wait_idle()
    assert duo.conversations[1101].backend == "cloud"

    # The cloud is rate limited too and local hasn't passed a health check yet: wait
    backend.status, backend2.status = 200, 429
    await duo.on_message(message(1101, "@Clangy more", parent_id=SUPPORT, mentions_bot=True))
    await duo.wait_idle()
    conv = duo.conversations[1101]
    assert conv.status == "backend_error" and "retry 1/5" in chat.in_channel(1101)[-1].current
    assert len(backend.requests) == 1, "no request to local before its health check"

    await duo.retry_failed()
    await duo.wait_idle()
    assert conv.status == "idle" and conv.backend == "local"
    assert chat.in_channel(1101)[-1].current.startswith("Default answer.")
    assert not duo.backends["cloud"].available(time.monotonic()), "cooling down"
    assert duo.backends["local"].failures == 0, "reset by the answer"
    assert duo.backends["cloud"].failures == 1
    assert metrics(duo, 1101)[-1]["backend_switch"] == {"from": "cloud", "reason": "failure"}


async def test_failovers_stop_after_trying_every_other_backend(duo, chat, backend, backend2):
    # The cloud's cooldown ends right away, so only the limit stops it falling over to itself again
    duo.backends["cloud"].config.cooldown = 0
    backend.status = backend2.status = 503
    await duo.on_message(forum_post(SUPPORT, 1102, "Crash"))
    await duo.wait_idle()
    conv = duo.conversations[1102]
    assert len(duo.agent.specs) == 2
    assert conv.status == "backend_error" and conv.retries == 0 and conv.failovers == 1
    [progress] = chat.in_channel(1102)
    assert "retry 1/5" in progress.current

    backend2.status = 200
    await duo.retry_failed()
    await duo.wait_idle()
    assert conv.status == "idle" and progress.current.startswith("Default answer.")
    assert conv.retries == conv.failovers == 0


async def test_new_conversations_go_where_the_answer_comes_first(duo):
    release = asyncio.Event()
    duo.agent.script = [blocked(release, answer("First."))]
    await duo.on_message(forum_post(SUPPORT, 1110, "A"))
    await asyncio.sleep(0.05)
    await duo.on_message(forum_post(SUPPORT, 1111, "B"))
    await asyncio.sleep(0.05)
    release.set()
    await duo.wait_idle()
    assert duo.conversations[1110].backend == "local", "both idle: the first listed"
    assert duo.conversations[1111].backend == "cloud", "local is busy"


async def test_conversations_stay_unless_another_backend_is_clearly_faster(duo):
    local, cloud = duo.backends["local"], duo.backends["cloud"]
    new = Conversation(thread_id=1, channel_id=SUPPORT, guild_id=GUILD)
    assert await duo._choose_backend(new, 3000) == (local, "new")
    gone = Conversation(thread_id=2, channel_id=SUPPORT, guild_id=GUILD, backend="removed")
    assert await duo._choose_backend(gone, 100) == (local, "new")

    conv = Conversation(thread_id=3, channel_id=SUPPORT, guild_id=GUILD, backend="local", context_tokens=3000)
    cloud.run_seconds = 10.0
    assert await duo._choose_backend(conv, 100) == (local, None), "faster, but not by the margin"
    await local.acquire("other")
    assert await duo._choose_backend(conv, 100) == (cloud, "balance"), "local is busy"
    conv.context_tokens = 100_000
    assert await duo._choose_backend(conv, 100) == (local, None), "reading it all again takes longer"
    await local.release("other")


async def test_context_size_is_kept_for_the_switch_rule(duo, backend):
    backend.prompt_tokens = 5000
    await duo.on_message(forum_post(SUPPORT, 1120, "Hello"))
    await duo.wait_idle()
    assert duo.conversations[1120].context_tokens == 5007


async def test_forum_title_goes_to_the_model(bot, config):
    await bot.on_message(
        forum_post(RECORD, 1130, "Because then they'll all be pink", thread_name="Plugin to paint ships")
    )
    in_thread = {"parent_id": RECORD, "thread_name": "Plugin to paint ships"}
    await bot.on_message(message(1130, "@Clangy Is this a reasonable plugin idea?", mentions_bot=True, **in_thread))
    await bot.wait_idle()
    assert "[host] The title of the forum post: Plugin to paint ships\nAlice: Because" in bot.agent.prompts[0]

    await bot.on_message(message(1130, "@Clangy and?", mentions_bot=True, **in_thread))
    await bot.wait_idle()
    assert "title" not in bot.agent.prompts[1], "told once"

    in_thread["thread_name"] = "Plugin to paint ships pink"
    await bot.on_message(message(1130, "@Clangy renamed", mentions_bot=True, **in_thread))
    await bot.wait_idle()
    assert "[host] The title of the forum post is now: Plugin to paint ships pink" in bot.agent.prompts[2]


async def test_mention_threads_get_no_title_note(bot):
    await bot.on_message(message(MENTION, "@Clangy hello", mentions_bot=True))
    await bot.on_message(message(1, "@Clangy more", parent_id=MENTION, mentions_bot=True, thread_name="hello"))
    await bot.wait_idle()
    assert not any("title" in prompt for prompt in bot.agent.prompts)


async def test_forum_tags_go_to_the_model(bot):
    tags = ["Pulsar", "Magnetar"]
    await bot.on_message(forum_post(QUIET_FORUM, 1131, "Can't weld", thread_tags=tags))
    in_thread = {"parent_id": QUIET_FORUM, "thread_name": "Post"}
    await bot.on_message(message(1131, "@Clangy help", mentions_bot=True, thread_tags=tags, **in_thread))
    await bot.wait_idle()
    assert "[host] The title of the forum post: Post\n[host] The tags of the forum post: Pulsar, Magnetar\n" in (
        bot.agent.prompts[0]
    )

    await bot.on_message(message(1131, "@Clangy and?", mentions_bot=True, thread_tags=tags, **in_thread))
    await bot.on_message(message(1131, "@Clangy retagged", mentions_bot=True, thread_tags=["Magnetar"], **in_thread))
    await bot.on_message(message(1131, "@Clangy untagged", mentions_bot=True, **in_thread))
    await bot.wait_idle()
    later = "".join(bot.agent.prompts[1:])
    assert "[host] The tags of the forum post are now: Magnetar" in later
    assert "[host] The forum post has no tags now." in later
    assert later.count("tags") == 2, "told once per change"


async def test_reactions_go_to_the_model(bot):
    await bot.on_message(forum_post(QUIET_FORUM, 1132, "Crash <3", author="Alice", reactions={"👍": 2}))
    bot.on_reaction(1132, "🙏", 1, "Alice")
    bot.on_reaction(1132, "👍", -1, "")
    bot.on_reaction(1132, "👍", -1, "")
    await bot.on_message(message(1132, "@Clangy help", parent_id=QUIET_FORUM, mentions_bot=True))
    await bot.wait_idle()
    assert "Alice: Crash <3 [reactions: 🙏 1]\n" in bot.agent.prompts[0], "queued: counted in place"

    answer_id = next(i for i, m in bot.conversations[1132].known.items() if m.own)
    bot.on_reaction(answer_id, ":pepe_thanks:", 1, "Alice")
    bot.on_reaction(1132, "🙏", -1, "")
    bot.on_reaction(4242, "👍", 1, "Bob")
    await bot.on_message(message(1132, "@Clangy thanks", parent_id=QUIET_FORUM, mentions_bot=True))
    await bot.wait_idle()
    second = bot.agent.prompts[1]
    assert "[host] Alice reacted with :pepe_thanks: to your message from " in second
    assert second.count("react") == 1, "seen messages: only new reactions, unknown messages ignored"
    assert bot.conversations[1132].known[1132].reactions == {}


async def test_priority_decides_before_speed(config, chat, backend, backend2):
    config.backends = [
        BackendConfig(name="local", url=backend.url, model="m", kind="llamacpp", priority=2),
        BackendConfig(name="cloud", url=backend2.url, model="c", priority=1, parallel=2),
    ]
    bot = Clangy(config, chat, run_agent=FakeAgent())
    local, cloud = bot.backends["local"], bot.backends["cloud"]
    local.run_seconds = 5.0
    new = Conversation(thread_id=1, channel_id=SUPPORT, guild_id=GUILD)
    assert await bot._choose_backend(new, 3000) == (cloud, "new"), "preferred, though slower"

    on_cloud = Conversation(thread_id=2, channel_id=SUPPORT, guild_id=GUILD, backend="cloud", context_tokens=100)
    assert await bot._choose_backend(on_cloud, 100) == (cloud, None), "a less preferred one doesn't count while free"

    await cloud.acquire("a")
    await cloud.acquire("b")
    assert await bot._choose_backend(new, 3000) == (local, "new"), "the preferred one is fully busy"
    assert await bot._choose_backend(on_cloud, 100) == (local, "balance"), "busy, and local is faster by the margin"

    on_local = Conversation(thread_id=3, channel_id=SUPPORT, guild_id=GUILD, backend="local", context_tokens=100)
    assert await bot._choose_backend(on_local, 100) == (local, None)
    await cloud.release("a")
    assert await bot._choose_backend(on_local, 100) == (cloud, "priority"), "back once the preferred one is free"

    await local.acquire("c")
    await cloud.acquire("a")
    local.run_seconds = cloud.run_seconds = 60.0
    assert await bot._choose_backend(new, 3000) == (cloud, "new"), "all busy: the soonest, ties by priority"
    await bot.close()


async def test_sandboxes_share_one_read_only_python_environment(bot, config):
    await bot.on_message(forum_post(SUPPORT, 1140, "Plot this"))
    await bot.on_message(forum_post(SUPPORT, 1142, "Plot that"))
    await bot.wait_idle()
    first, second = bot.agent.specs
    assert first.venv is not None and first.venv == second.venv
    assert first.venv.parent.parent == config.data_dir / "python"
    calls = (first.venv.parent / ".uv-calls").read_text().splitlines()
    assert len(calls) == 1 and calls[0].startswith("sync --frozen --compile-bytecode"), "built once, from the lock"
    assert not (first.paths.work / "pyproject.toml").exists()


async def test_python_environment_is_rebuilt_for_a_new_lock_or_after_a_broken_build(bot, tmp_path, monkeypatch):
    current = await bot.python_env()
    (current.parent / ".complete").unlink()
    assert await bot.python_env() == current
    assert len((current.parent / ".uv-calls").read_text().splitlines()) == 1, "a broken build starts over"

    workspace = tmp_path / "python-project"
    shutil.copytree(core.WORKSPACE_DIR, workspace)
    with open(workspace / "uv.lock", "a") as f:
        f.write("\n")
    monkeypatch.setattr(core, "WORKSPACE_DIR", workspace)
    changed = await bot.python_env()
    assert changed != current and (current / "bin" / "python").exists(), "the old one stays for running answers"


async def test_answers_still_come_without_python_environment(bot, chat, config):
    config.sandbox.uv = "/nonexistent/uv"
    await bot.on_message(forum_post(SUPPORT, 1141, "Hi"))
    await bot.wait_idle()
    assert chat.in_channel(1141)[0].current.startswith("Default answer.")
    assert bot.agent.specs[0].venv is None


async def test_old_conversations_and_records_are_deleted_at_start(bot, chat, config):
    await bot.on_message(forum_post(SUPPORT, 1143, "Old question"))
    await bot.on_message(forum_post(SUPPORT, 1144, "New question"))
    await bot.on_message(forum_post(RECORD, 1145, "Old request"))
    await bot.wait_idle()
    conversations = config.data_dir / "conversations"
    (conversations / "1146" / "files" / "received").mkdir(parents=True)  # Left without a state.json
    old = time.time() - 31 * 86400
    for path in ("conversations/1143/state.json", "conversations/1145/state.json", "conversations/1146"):
        os.utime(config.data_dir / path, (old, old))
    os.utime(config.data_dir / "records" / "1145.md", (old, old))

    second = Clangy(config, chat, run_agent=FakeAgent())
    await second.load()
    assert set(second.conversations) == {1144}
    assert sorted(p.name for p in conversations.iterdir()) == ["1143", "1144"]
    assert [p.name for p in (conversations / "1143").iterdir()] == ["metrics.jsonl"]
    assert (conversations / "1144" / "work").is_dir()
    assert not (config.data_dir / "records" / "1145.md").exists()
    await second.close()


async def test_cache_never_outlives_the_retention(bot, config, monkeypatch):
    pruned = []
    monkeypatch.setattr(bot.cache, "prune", pruned.append)
    bot.expire()
    config.retention_days = 0.5
    bot.expire()
    now = datetime.now(timezone.utc)
    assert [round((now - when).total_seconds() / 3600) for when in pruned] == [48, 12]


async def test_expired_thread_starts_over_from_discord_on_a_mention(bot, chat, config):
    await bot.on_message(forum_post(SUPPORT, 1147, "Old question", author="Carol"))
    await bot.wait_idle()
    old = time.time() - 41 * 86400
    os.utime(config.data_dir / "conversations" / "1147" / "state.json", (old, old))
    chat.channel_history[1147] = [
        message(1147, "Old question", parent_id=SUPPORT, id=1147, author="Carol"),
        message(1147, chat.in_channel(1147)[0].current, parent_id=SUPPORT, id=1148, author="Clangy", from_self=True),
        message(1147, "Back again", parent_id=SUPPORT, id=1149, author="Carol"),
    ]

    agent = FakeAgent()
    second = Clangy(config, chat, run_agent=agent)
    await second.load()
    await second.on_message(chat.channel_history[1147][-1])
    await second.wait_idle()
    assert 1147 not in second.conversations and agent.specs == [], "only a mention brings it back"
    await second.on_message(message(1147, "@Clangy still broken", parent_id=SUPPORT, id=1150, mentions_bot=True))
    await second.wait_idle()
    prompt = agent.prompts[0]
    assert prompt.index("Carol: Old question") < prompt.index("Carol: Back again") < prompt.index("still broken")
    assert prompt.index("Carol: Old question") < prompt.index("Clangy (you): Default answer.") < prompt.index("Back")
    assert text.footer("Clangy") not in prompt
    assert chat.in_channel(1147)[-1].current.startswith("Default answer.")
    await second.close()


async def test_unknown_thread_keeps_only_clangys_answers(bot, chat):
    footer = text.footer("Clangy")
    own = dict(parent_id=QUIET_FORUM, author="Clangy", from_self=True)
    long_answer = text.answer_parts("A" * 1500 + "\n\n" + "B" * 1500, footer)
    chat.channel_history[1151] = [
        message(1151, "v1 answer", **own),
        message(1151, "Question", parent_id=QUIET_FORUM, author="Carol"),
        message(1151, text.failed("Clangy"), **own),
        message(1151, long_answer[0], **own),
        message(1151, long_answer[1], **own),
        message(1151, "Thanks", parent_id=QUIET_FORUM, author="Carol"),
        message(1151, text.progress(0, None, 0, 1), **own),
        message(1151, "Short answer.", **own),
        message(1151, footer, **own),
    ]
    await bot.on_message(message(1151, "@Clangy again", parent_id=QUIET_FORUM, mentions_bot=True))
    await bot.wait_idle()
    prompt = bot.agent.prompts[0]
    assert "v1 answer" not in prompt and "Sorry" not in prompt and "⏳" not in prompt
    assert prompt.count(footer) == 0
    lines = [line for line in prompt.splitlines() if line.startswith(("Carol", "Clangy"))]
    assert lines == [
        "Carol: Question",
        "Clangy (you): " + "A" * 1500,
        "Clangy (you): " + "B" * 1500,
        "Carol: Thanks",
        "Clangy (you): Short answer.",
    ]


def queue(bot: Clangy, name: str, to: str, body: str) -> None:
    bot.outbox.mkdir(exist_ok=True)
    (bot.outbox / f"{name}.md").write_text(f"---\nto: {to}\n---\n\n{body}\n")


def thread_url(thread_id: int) -> str:
    return f"https://discord.com/channels/{GUILD}/{thread_id}"


async def test_queued_post_goes_to_the_ticket_thread_once(bot, chat, config):
    await bot.on_message(forum_post(QUIET_FORUM, 1160, "Crash on load", author="Carol"))
    bot.store.create("ticket", "Crash on load", "Details", {"thread": thread_url(1160)}, by="Carol")
    chat.parents[1160] = QUIET_FORUM
    queue(bot, "DIS-0001-info", "DIS-0001", "Which Pulsar version do you run?")
    await bot.send_posts()
    [post] = chat.in_channel(1160)
    assert post.content == f"Which Pulsar version do you run?\n\n{text.footer('Clangy')}"
    link = f"{thread_url(1160)}/{post.message_id}"
    sent = (bot.outbox / "sent" / "DIS-0001-info.md").read_text()
    assert f"message: {link}\n" in sent and "posted: " in sent and "to: DIS-0001\n" in sent
    assert not (bot.outbox / "DIS-0001-info.md").exists()
    ticket = (config.data_dir / "workspace" / "tickets" / "DIS-0001.md").read_text()
    assert ticket.endswith(f": Clangy posted DIS-0001-info from the outbox in the thread: {link}\n")
    log = subprocess.run(
        ["git", f"--git-dir={bot.store.history.git_dir}", "log", "-1", "--format=%B"], capture_output=True, text=True
    ).stdout
    assert log.startswith("Post DIS-0001-info in the thread of DIS-0001")

    # A retry under the same name is refused
    queue(bot, "DIS-0001-info", "DIS-0001", "Which Pulsar version do you run?")
    await bot.send_posts()
    assert len(chat.in_channel(1160)) == 1
    failed = (bot.outbox / "failed" / "DIS-0001-info.md").read_text()
    assert "error: sent/DIS-0001-info.md exists, it was posted before\n" in failed

    # The reporter's answer comes with the post, so the model can add it to the ticket
    await bot.on_message(message(1160, "@Clangy 2.4.3", parent_id=QUIET_FORUM, author="Carol", mentions_bot=True))
    await bot.wait_idle()
    prompt = bot.agent.prompts[0]
    note = "[host] A developer had you post this in the thread about DIS-0001: Which Pulsar version do you run?"
    assert prompt.index(note) < prompt.index("Carol: @Clangy 2.4.3")
    assert bot.conversations[1160].known[post.message_id].own


async def test_queued_post_by_thread_id_without_conversation(bot, chat, config):
    tickets = config.data_dir / "workspace" / "tickets"
    bot.store.create("ticket", "Old crash", "Details", {"thread": thread_url(1161)})
    (tickets / "DIS-0001.md").rename(tickets / "completed" / "DIS-0001.md")
    bot.store.append_record(1162, "Request", thread_url(1162), "**Dan**: please add X")
    chat.parents.update({1161: QUIET_FORUM, 1162: RECORD})
    queue(bot, "fixed", "1161", "Fixed in Bugfixes 1.7.0.")
    queue(bot, "request", "1162", "Added in 1.8.0.")
    await bot.send_posts()
    assert chat.in_channel(1161)[0].content.startswith("Fixed in Bugfixes 1.7.0.")
    assert chat.in_channel(1162)[0].content.startswith("Added in 1.8.0.")
    assert "Clangy posted fixed from the outbox" in (tickets / "completed" / "DIS-0001.md").read_text()
    assert sorted(p.name for p in (bot.outbox / "sent").iterdir()) == ["fixed.md", "request.md"]


async def test_queued_posts_are_refused_outside_known_threads(bot, chat, config):
    bot.store.create("ticket", "Elsewhere", "Details", {"thread": thread_url(1163)})
    bot.store.create("ticket", "Here", "Details", {"thread": thread_url(1164)})
    chat.parents.update({1163: 999, 1164: QUIET_FORUM})
    queue(bot, "a", "DIS-0001", "Hi")
    queue(bot, "b", "DIS-0099", "Hi")
    queue(bot, "c", "1165", "Hi")
    queue(bot, "d", "general", "Hi")
    queue(bot, "e", "DIS-0002", "")
    queue(bot, "f", "DIS-0002", "x" * 2000)
    await bot.send_posts()
    assert chat.messages == {} and list(bot.outbox.glob("*.md")) == []
    errors = {p.stem: frontmatter(p.read_text())[0]["error"] for p in (bot.outbox / "failed").iterdir()}
    assert errors == {
        "a": "thread 1163 is not in a channel Clangy monitors",
        "b": "no ticket DIS-0099 with a thread",
        "c": "Clangy has no conversation, record or ticket of thread 1165",
        "d": "`to:` is neither a DIS ticket nor a thread id",
        "e": "the message is empty",
        "f": f"the message is over {text.LIMIT - 2 - len(text.footer('Clangy'))} characters",
    }


async def test_failed_post_can_be_queued_again(bot, chat, monkeypatch):
    bot.store.create("ticket", "Crash", "Details", {"thread": thread_url(1166)})
    chat.parents[1166] = QUIET_FORUM
    send = chat.send

    async def forbidden(*args, **kwargs):
        raise RuntimeError("403 Forbidden")

    monkeypatch.setattr(chat, "send", forbidden)
    queue(bot, "reply", "DIS-0001", "Fixed.")
    await bot.send_posts()
    assert "error: 403 Forbidden\n" in (bot.outbox / "failed" / "reply.md").read_text()
    assert not (bot.outbox / "sent" / "reply.md").exists()
    monkeypatch.setattr(chat, "send", send)
    queue(bot, "reply", "DIS-0001", "Fixed.")
    await bot.send_posts()
    assert len(chat.in_channel(1166)) == 1

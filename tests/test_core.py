import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

import aiohttp
import pytest

from clangy import text
from clangy.config import BackendConfig, ChannelConfig, Config, KnowledgeSource
from clangy.core import Attachment, Clangy, IncomingMessage
from clangy.proxy import short_path
from clangy.runner import RunResult, RunSpec

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

    @property
    def prompts(self) -> list[str]:
        return [s.prompt for s in self.specs]

    async def __call__(self, spec: RunSpec, port: int, deadline: float, on_state) -> RunResult:
        self.specs.append(spec)
        self.on_state = on_state
        on_state("reading")
        step = self.script.pop(0) if self.script else answer("Default answer.")
        return await step(self, spec)


@pytest.fixture
def config(tmp_path, backend) -> Config:
    return Config(
        data_dir=tmp_path,
        backends=[BackendConfig(name="local", url=backend.url, model="m")],
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
    return message(thread, text, parent_id=forum, id=thread, starts_thread=True, thread_name="Post", **kwargs)


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


async def test_ignores_unknown_threads_and_unconfigured_channels(bot, chat):
    await bot.on_message(message(1002, "@Clangy hi", parent_id=QUIET_FORUM, mentions_bot=True))
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


async def test_progress_shows_queue_position(bot, chat, config):
    release = asyncio.Event()
    bot.agent.script = [blocked(release, answer("A")), answer("B")]
    await bot.on_message(forum_post(SUPPORT, 1005, "First"))
    await bot.on_message(forum_post(SUPPORT, 1006, "Second"))
    await asyncio.sleep(0.1)
    [waiting] = chat.in_channel(1006)
    assert waiting.current.startswith("⏳") and "waiting in queue" in waiting.current and "<t:" in waiting.current
    [running] = chat.in_channel(1005)
    assert "reading" in running.current and "deadline <t:" in running.current
    release.set()
    await bot.wait_idle()


async def test_progress_is_edited_every_minute(bot, chat, monkeypatch):
    clock = [1_000_000.0]
    monkeypatch.setattr("clangy.core.time.time", lambda: clock[0])
    release = asyncio.Event()

    async def two_turns(agent: FakeAgent, spec: RunSpec) -> RunResult:
        agent.on_state("looking things up")
        agent.on_state("reading")
        await release.wait()
        return await answer("Done.")(agent, spec)

    bot.agent.script = [two_turns]
    await bot.on_message(forum_post(SUPPORT, 1023, "Slow one"))
    await asyncio.sleep(0.05)
    [progress] = chat.in_channel(1023)
    for minute in (1, 2):
        clock[0] += 60
        await asyncio.sleep(0.05)
        assert f"(step 2, {minute} min so far)" in progress.current
    release.set()
    await bot.wait_idle()
    assert progress.current.startswith("Done.")
    record = json.loads((bot.conversation_dir(1023) / "metrics.jsonl").read_text())
    assert record["total_seconds"] == 120.0 and record["steps"] == 2
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


async def test_backend_loss_retries_three_times_then_gives_up(bot, chat, backend):
    backend.status = 503
    await bot.on_message(forum_post(SUPPORT, 1009, "Crash"))
    await bot.wait_idle()
    conv = bot.conversations[1009]
    [progress] = chat.in_channel(1009)
    assert conv.status == "backend_error" and "retry 0/3" in progress.current
    assert len(conv.pending) == 1 and not bot.agent.specs[0].paths.session.exists()

    for attempt in (1, 2, 3):
        await bot.retry_failed()
        await bot.wait_idle()
        assert conv.retries == attempt
    assert conv.status == "failed"
    assert progress.current == text.backend_gave_up("Clangy")
    assert len(chat.in_channel(1009)) == 1

    await bot.retry_failed()
    await bot.wait_idle()
    assert len(bot.agent.specs) == 4, "no automatic retries after giving up"

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
    assert any("retry 1/3" in edit for edit in progress.edits)
    assert progress.current.startswith("Default answer.")
    assert bot.conversations[1010].retries == 0


async def test_monitor_skips_unhealthy_backend(bot, backend):
    backend.status = 503
    await bot.on_message(forum_post(SUPPORT, 1011, "Crash"))
    await bot.wait_idle()
    await backend.stop()
    await bot.retry_failed()
    assert bot.conversations[1011].retries == 0


async def test_set_game_mounts_knowledge_and_continues(bot, chat, config):
    knowledge = config.data_dir / "knowledge" / "releases" / "r1" / "se2" / "knowledge" / "skills"
    knowledge.mkdir(parents=True)
    (config.data_dir / "knowledge" / "current").symlink_to("releases/r1")
    config.knowledge["se2"] = [KnowledgeSource(url="https://example.invalid/skills2.git", link="knowledge/skills")]
    bot.agent.script = [tool("set_game", {"game": "se2"}, answer("")), answer("SE2 answer")]
    await bot.on_message(forum_post(SUPPORT, 1012, "SE2 crashes on start"))
    await bot.wait_idle()
    first, second = bot.agent.specs
    assert knowledge.resolve() not in first.read_only
    assert second.prompt.startswith("[host] This conversation is about SE2. Its knowledge is under knowledge/.")
    assert (second.paths.work / "knowledge" / "skills").is_dir()
    assert knowledge.resolve() in second.read_only
    conv = bot.conversations[1012]
    assert conv.game == "se2" and conv.host_notes == [] and conv.pending == []
    assert chat.in_channel(1012)[0].current.startswith("SE2 answer")


async def test_knowledge_folders_in_place_only_for_the_game(bot, config, tmp_path):
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
    assert {folders["skills"], folders["books"], folders["se-dev"]} <= set(spec.read_only)
    assert folders["skills2"] not in spec.read_only

    # Switching the game swaps the links and drops the other game's extras
    bot.agent.script = [tool("set_game", {"game": "se2"}, answer("")), answer("SE2 then.")]
    await bot.on_message(message(1024, "@Clangy actually SE2", parent_id=SE1_FORUM, mentions_bot=True))
    await bot.wait_idle()
    assert (work / "knowledge" / "skills").resolve() == folders["skills2"]
    assert not (work / ".se-dev").exists() and not (work / "knowledge" / "books").exists()
    assert folders["se-dev"] not in bot.agent.specs[-1].read_only


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


async def test_ticket_tools_link_thread_and_attachments(bot, chat, config):
    chat.files["https://cdn/info.log"] = b"log data"
    bot.agent.script = [
        tool("ticket_create", {"title": "Crash on load", "body": "Details"}, answer("Filed T-0001.")),
        tool("ticket_add_report", {"id": "T-0001", "body": "Again"}, answer("Added.")),
        tool("ticket_create", {"title": "", "body": "x"}, answer("Oops.")),
    ]
    attachment = Attachment(1, "info.log", 8, "https://cdn/info.log")
    await bot.on_message(forum_post(SE1_FORUM, 1015, "Crash", attachments=[attachment]))
    await bot.wait_idle()
    work = bot.agent.specs[0].paths.work
    assert (work / "attachments" / "1015-info.log").read_bytes() == b"log data"
    files = config.data_dir / "conversations" / "1015" / "files"
    assert "[host] Attachment from Alice: attachments/1015-info.log (1 KB)" in bot.agent.prompts[0]
    ticket = (config.data_dir / "store" / "tickets" / "T-0001.md").read_text()
    assert f"thread: https://discord.com/channels/{GUILD}/1015\n" in ticket
    assert "game: se1\n" in ticket and f"files: {files}\n" in ticket
    for prompt in ("@Clangy same", "@Clangy bad"):
        await bot.on_message(message(1015, prompt, parent_id=SE1_FORUM, mentions_bot=True))
        await bot.wait_idle()
    assert bot.agent.tool_results[1] == (200, {"text": "Added the report to T-0001."})
    assert bot.agent.tool_results[2] == (400, {"error": "title is empty"})


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
    record = (config.data_dir / "store" / "records" / "1018.md").read_text()
    assert "title: Post" in record and "**Alice**" in record and "**Bob** (2026-10-03 12:00 UTC): +1" in record
    assert chat.messages == {}


async def test_backends_are_assigned_round_robin_and_pinned(config, chat, backend):
    config.backends.append(BackendConfig(name="second", url=backend.url, model="m"))
    bot = Clangy(config, chat, run_agent=FakeAgent())
    for thread in (1, 2, 3):
        await bot.on_message(forum_post(QUIET_FORUM, thread, "x"))
    assert [bot.conversations[t].backend for t in (1, 2, 3)] == ["local", "second", "local"]
    await bot.close()


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
        "Bob: fixed text" in first and "typo here" not in first and "spam" not in first and "[host]" not in first[25:]
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
    assert 'Bob deleted their message from 2026-10-03 12:00 UTC ("fixed text"). Disregard it.' in second
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
    assert conv.pending[0].text == "Question" and conv.host_notes == []


async def test_record_mode_logs_edits_and_deletions(bot, config):
    await bot.on_message(forum_post(RECORD, 1036, "Make a plugin"))
    bot.on_edit(message(1036, "Make a plugin for X", parent_id=RECORD, id=1036))
    bot.on_delete(1036)
    record = (config.data_dir / "store" / "records" / "1036.md").read_text()
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

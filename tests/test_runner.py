import json
import sys
import textwrap
from pathlib import Path

from clangy.config import BackendConfig, SandboxConfig
from clangy.runner import RunPaths, RunSpec, build_command, event_state, final_result, run_pi, write_pi_config

BACKEND = BackendConfig(name="b", url="http://x/v1", model="qwen", thinking="low", compat={"thinkingFormat": "qwen"})


def spec(tmp_path: Path, sandbox: SandboxConfig, prompt: str = "[host] hi") -> RunSpec:
    return RunSpec(paths=RunPaths(tmp_path), backend=BACKEND, sandbox=sandbox, prompt=prompt, read_only=[Path("/k")])


def test_firejail_command(tmp_path):
    command = build_command(spec(tmp_path, SandboxConfig(read_only=["/opt/node"], env={"UV_OFFLINE": "1"})), 4321)
    separator = command.index("--")
    jail, inner = command[:separator], command[separator + 1 :]
    assert jail[0] == "firejail" and "--net=none" in jail and "--noroot" in jail
    assert f"--whitelist={tmp_path}" in jail and f"--read-write={tmp_path}" in jail
    for path in ("/k", "/opt/node"):
        assert f"--read-only={path}" in jail
    assert inner[:2] == ["env", "-i"]
    assert f"HOME={tmp_path}" in inner and "CLANGY_PORT=4321" in inner and "UV_OFFLINE=1" in inner
    # Secrets from the host environment never reach the sandbox
    assert not any(arg.startswith(("DISCORD", "CLANGY_TEST_KEY")) for arg in inner)
    assert inner[-1] == "[host] hi"
    assert inner[inner.index("--model") + 1] == "clangy/qwen"


def test_unsandboxed_command(tmp_path):
    command = build_command(spec(tmp_path, SandboxConfig(firejail=False)), 1)
    assert command[0] == "env"


def test_pi_config_points_at_the_bridge(tmp_path):
    paths = RunPaths(tmp_path)
    write_pi_config(paths, BACKEND, 4321)
    provider = json.loads((paths.agent_dir / "models.json").read_text())["providers"]["clangy"]
    assert provider["baseUrl"] == "http://127.0.0.1:4321/v1"
    assert provider["compat"] == {"supportsDeveloperRole": False, "thinkingFormat": "qwen"}
    assert provider["models"][0]["reasoning"] is True
    settings = json.loads((paths.agent_dir / "settings.json").read_text())
    assert settings["retry"]["enabled"] is False


def test_event_states():
    assert event_state({"type": "turn_start"}) == "reading"
    assert event_state({"type": "tool_execution_start", "toolName": "grep"}) == "looking things up"
    assert event_state({"type": "tool_execution_start", "toolName": "ticket_create"}) == "filing a ticket"
    update = {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta"}}
    assert event_state(update) == "thinking"
    assert event_state({"type": "agent_start"}) is None


def test_final_result():
    def end(*messages):
        return {"type": "agent_end", "messages": list(messages)}

    tool_turn = {"role": "assistant", "content": [{"type": "text", "text": "Let me check."}], "stopReason": "toolUse"}
    answer = {
        "role": "assistant",
        "content": [{"type": "thinking", "thinking": "hm"}, {"type": "text", "text": " Done "}],
    }
    assert final_result(end(tool_turn, answer)).answer == "Done"
    error = {"role": "assistant", "content": [], "stopReason": "error", "errorMessage": "502"}
    assert final_result(end(error)).error == "502"
    assert final_result(end({"role": "assistant", "content": []})).error == "empty answer"


def fake_pi(tmp_path: Path, body: str) -> SandboxConfig:
    script = tmp_path / "fake_pi.py"
    script.write_text(textwrap.dedent(body))
    return SandboxConfig(firejail=False, pi_command=[sys.executable, str(script)], path="/usr/bin:/bin")


async def test_run_pi_parses_events(tmp_path):
    sandbox = fake_pi(
        tmp_path,
        """
        import json
        def emit(e): print(json.dumps(e), flush=True)
        emit({"type": "turn_start"})
        emit({"type": "tool_execution_start", "toolName": "grep"})
        emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta"}})
        emit({"type": "agent_end", "messages": [{"role": "assistant", "content": [{"type": "text", "text": "Hi"}]}]})
        """,
    )
    states = []
    result = await run_pi(spec(tmp_path, sandbox), 1, 10.0, states.append)
    assert result.answer == "Hi"
    assert result.tools == ["grep"]
    assert states == ["reading", "looking things up", "writing"]


async def test_run_pi_deadline(tmp_path):
    sandbox = fake_pi(tmp_path, "import time; time.sleep(30)")
    result = await run_pi(spec(tmp_path, sandbox), 1, 0.3, lambda s: None)
    assert result.timed_out and result.answer is None


def test_host_tools_match_the_extension():
    import re

    from clangy.runner import EXTENSION, HOST_TOOLS

    assert re.findall(r'name: "(\w+)"', EXTENSION.read_text()) == HOST_TOOLS

"""Runs one pi turn for a conversation inside its sandbox."""

import asyncio
import json
import logging
import os
import signal
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from clangy.config import BackendConfig, SandboxConfig

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).parent
EXTENSION = PACKAGE_DIR / "pi" / "clangy.ts"
SANDBOX_BIN = PACKAGE_DIR / "sandbox-bin"
"""rg and fd wrappers that follow symlinks, first on the sandbox's PATH."""
HOST_TOOLS = ["set_game", "attach_files", "ticket_create", "ticket_add_report", "note_create", "plan_create"]
"""Registered by pi/clangy.ts. pi 1.x applies --tools to extension tools too, so they must be listed."""
TOOLS = ",".join(["read", "grep", "find", "ls", "bash", *HOST_TOOLS])
PI_MODEL = "clangy"
"""The model id pi sees on every backend; the host sets the real one. pi turns the reasoning of another model's
turns into plain text, which would happen whenever a conversation moves between backends."""

TOOL_STATES = {
    "bash": "analyzing",
    "set_game": "loading the game knowledge",
    "attach_files": "preparing files",
    "ticket_create": "filing a ticket",
    "ticket_add_report": "updating a ticket",
    "note_create": "writing a note",
    "plan_create": "writing a plan",
}


@dataclass
class RunPaths:
    work: Path

    @property
    def pi_dir(self) -> Path:
        return self.work / ".pi"

    @property
    def agent_dir(self) -> Path:
        return self.pi_dir / "agent"

    @property
    def session(self) -> Path:
        return self.pi_dir / "session.jsonl"

    @property
    def system_prompt(self) -> Path:
        return self.pi_dir / "system.md"

    @property
    def socket(self) -> Path:
        return self.work / ".clangy" / "host.sock"

    @property
    def proxy_socket(self) -> Path:
        return self.work / ".clangy" / "proxy.sock"


@dataclass
class RunSpec:
    paths: RunPaths
    backend: BackendConfig
    sandbox: SandboxConfig
    prompt: str
    read_only: list[Path] = field(default_factory=list)
    """Host paths the sandbox may read: knowledge, store, guide."""
    proxy_port: int | None = None
    """Sandbox port bridged to the filtering proxy, if one is configured."""


@dataclass
class RunResult:
    answer: str | None = None
    error: str | None = None
    timed_out: bool = False
    tools: list[str] = field(default_factory=list)
    """Tools called during the run, in order."""


def write_pi_config(paths: RunPaths, backend: BackendConfig, port: int) -> None:
    """Writes pi's model and settings files, pointing inference at the host bridge."""
    paths.agent_dir.mkdir(parents=True, exist_ok=True)
    model = {
        "id": PI_MODEL,
        # pi sends a thinkingFormat's "thinking off" only to reasoning models; without it Qwen thinks by default
        "reasoning": backend.thinking != "off" or "thinkingFormat" in backend.compat,
        "contextWindow": backend.context_window,
        "maxTokens": backend.max_tokens,
    }
    provider = {
        "baseUrl": f"http://127.0.0.1:{port}/v1",
        "api": "openai-completions",
        "apiKey": "unused",
        "compat": {"supportsDeveloperRole": False, **backend.compat},
        "models": [model],
    }
    models = {"providers": {"clangy": provider}}
    # Retries are the host's job, so a lost backend never leaves half a turn in the session
    settings = {"retry": {"enabled": False}, "quietStartup": True}
    (paths.agent_dir / "models.json").write_text(json.dumps(models, indent=2))
    (paths.agent_dir / "settings.json").write_text(json.dumps(settings, indent=2))


def pi_arguments(spec: RunSpec) -> list[str]:
    return [
        *spec.sandbox.pi_command,
        "--mode", "json", "--print", "--offline",
        "--session", str(spec.paths.session),
        "--no-skills", "--no-prompt-templates", "--no-themes", "--no-extensions",
        "--extension", str(EXTENSION),
        "--tools", TOOLS,
        "--model", f"clangy/{PI_MODEL}",
        "--thinking", spec.backend.thinking,
        "--system-prompt", str(spec.paths.system_prompt),
        spec.prompt,
    ]  # fmt: skip


def build_command(spec: RunSpec, port: int) -> list[str]:
    """Command line running pi with a clean environment, inside firejail unless disabled."""
    env = {
        "PATH": f"{SANDBOX_BIN}:{spec.sandbox.path}",
        "HOME": str(spec.paths.work),
        "LANG": "C.UTF-8",
        "PI_CODING_AGENT_DIR": str(spec.paths.agent_dir),
        "PI_OFFLINE": "1",
        # Relative to pi's working directory, which keeps it under the unix socket path limit
        "CLANGY_SOCKET": str(spec.paths.socket.relative_to(spec.paths.work)),
        "CLANGY_PORT": str(port),
        "CLANGY_SYSTEM_PROMPT": str(spec.paths.system_prompt),
        **spec.sandbox.env,
    }
    if spec.proxy_port is not None:
        proxy = f"http://127.0.0.1:{spec.proxy_port}"
        env["CLANGY_PROXY_PORT"] = str(spec.proxy_port)
        env["CLANGY_PROXY_SOCKET"] = str(spec.paths.proxy_socket.relative_to(spec.paths.work))
        # Both spellings: curl only reads the lowercase http_proxy, other tools the uppercase ones
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env[name] = proxy
        env["NO_PROXY"] = env["no_proxy"] = "127.0.0.1,localhost"
    command = ["env", "-i", *(f"{k}={v}" for k, v in env.items()), *pi_arguments(spec)]
    if not spec.sandbox.firejail:
        return command
    jail = [
        "firejail", "--quiet", "--noprofile",
        "--net=none", "--nosound", "--no3d", "--nodvd", "--notv", "--novideo", "--nou2f",
        "--caps.drop=all", "--nonewprivs", "--noroot", "--seccomp",
        "--private-dev", "--private-tmp",
        f"--whitelist={spec.paths.work}", f"--read-write={spec.paths.work}",
    ]  # fmt: skip
    read_only = [*spec.read_only, PACKAGE_DIR, *map(Path, spec.sandbox.read_only)]
    for path in read_only:
        jail += [f"--whitelist={path}", f"--read-only={path}"]
    return [*jail, "--", *command]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def event_state(event: dict) -> str | None:
    """Plain-language progress state for a pi JSON event, if it changes the state."""
    match event.get("type"):
        case "turn_start":
            return "reading"
        case "tool_execution_start":
            return TOOL_STATES.get(event.get("toolName", ""), "looking things up")
        case "message_update":
            kind = event.get("assistantMessageEvent", {}).get("type", "")
            if kind.startswith("thinking"):
                return "thinking"
            if kind.startswith("text"):
                return "writing"
    return None


def final_result(event: dict) -> RunResult:
    """Extracts the answer from pi's agent_end event."""
    assistant = [m for m in event.get("messages", []) if m.get("role") == "assistant"]
    if not assistant:
        return RunResult(error="no answer")
    last = assistant[-1]
    if last.get("stopReason") in ("error", "aborted"):
        return RunResult(error=last.get("errorMessage") or last["stopReason"])
    text = "\n".join(c["text"] for c in last.get("content", []) if c.get("type") == "text")
    # With reasoning off, a model may still write its reasoning into the answer, ended by a stray tag
    text = text.rpartition("</think>")[2].strip()
    return RunResult(answer=text) if text else RunResult(error="empty answer")


async def run_pi(spec: RunSpec, port: int, deadline: float, on_state: Callable[[str], None]) -> RunResult:
    """Runs pi until it exits or the deadline (seconds from now) passes."""
    command = build_command(spec, port)
    process = await asyncio.create_subprocess_exec(
        *command,
        cwd=spec.paths.work,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
        limit=64 * 1024**2,
    )
    result = RunResult(error="pi exited without an answer")
    tools: list[str] = []

    async def consume() -> None:
        nonlocal result
        assert process.stdout is not None
        async for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if state := event_state(event):
                on_state(state)
            if event.get("type") == "tool_execution_start":
                tools.append(event.get("toolName", ""))
            if event.get("type") == "agent_end":
                result = final_result(event)
        await process.wait()

    try:
        await asyncio.wait_for(consume(), deadline)
    except TimeoutError:
        os.killpg(process.pid, signal.SIGKILL)
        await process.wait()
        return RunResult(timed_out=True, tools=tools)
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
    if process.returncode != 0 and result.answer is None:
        assert process.stderr is not None
        stderr = (await process.stderr.read()).decode(errors="replace")[-2000:]
        log.warning("pi exited with %s: %s", process.returncode, stderr)
        result.error = f"pi exited with {process.returncode}: {stderr[-500:]}"
    result.tools = tools
    return result

import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Game = Literal["se1", "se2"]
PI_ATTRIBUTION = {"HTTP-Referer": "https://pi.dev", "X-OpenRouter-Title": "pi", "X-OpenRouter-Categories": "cli-agent"}
"""pi's own OpenRouter attribution. pi sends it only when it talks to OpenRouter itself, never through the proxy."""
ChannelMode = Literal["support", "record", "mention"]


class BackendConfig(BaseModel):
    name: str
    url: str
    """OpenAI compatible base URL, including the `/v1` suffix."""
    model: str
    api_key: str | None = None
    """Used when set, otherwise the api_key_env variable. Keep the file readable only by the bot."""
    api_key_env: str | None = None
    parallel: int = 1
    priority: int = 1
    """Lower is preferred. A backend gets runs only while every more preferred one has failed or is fully busy."""
    kind: Literal["openai", "llamacpp"] = "openai"
    slot_save: bool = False
    """llama.cpp only: save/restore slot KV caches, needs `--slot-save-path` on the server."""
    context_window: int = 131072
    max_tokens: int = 4096
    thinking: str = "off"
    """pi thinking level. On OpenRouter it becomes the `reasoning` effort, `off` turns reasoning off."""
    compat: dict[str, Any] = Field(default_factory=dict)
    prompt_tps: float = 300.0
    gen_tps: float = 40.0
    extra_body: dict[str, Any] = Field(default_factory=dict)
    """Merged into every chat completion request, e.g. OpenRouter's `models` and `provider`."""
    headers: dict[str, str] = Field(default_factory=dict)
    """Sent with every chat completion request, over the top-level headers."""
    first_byte_timeout: float | None = None
    """A request fails if no data arrives for this long. Not for llama.cpp, it's silent while reading a prompt."""
    stall_timeout: float | None = None
    """A request fails if its stream stops for this long after the first data."""
    cooldown: float = 60.0
    """openai kind: a failed backend gets new runs again after this long, or after the Retry-After it sent."""

    @model_validator(mode="after")
    def _check(self) -> "BackendConfig":
        if "reasoning" in self.extra_body:
            raise ValueError(f"backend {self.name}: set thinking instead of extra_body.reasoning")
        return self

    @property
    def root_url(self) -> str:
        return self.url.removesuffix("/").removesuffix("/v1")


class ChannelConfig(BaseModel):
    id: int
    mode: ChannelMode
    initial_response: bool = False
    initial_delay: float = 0.0
    """Forums: the first answer to a new thread waits until nobody wrote in it for this long."""
    history_messages: int = 100
    """At most this many earlier channel messages go along with a mention as context. Threads go along in full."""
    history_hours: float = 4.0
    """Context reaches back to today's midnight (UTC), but at least this many hours."""
    game: Game | None = None
    prompt: str = ""


class SandboxConfig(BaseModel):
    firejail: bool = True
    pi_command: list[str] = ["pi"]
    path: str = "/usr/local/bin:/usr/bin:/bin"
    """PATH inside the sandbox, must reach node, pi and python3."""
    read_only: list[str] = Field(default_factory=list)
    """Extra host paths the sandbox may read, e.g. the node installation."""
    env: dict[str, str] = Field(default_factory=dict)
    """Extra environment variables inside the sandbox, e.g. to keep uv offline."""
    proxy: str | None = None
    """host:port of an HTTP proxy the sandbox may use, e.g. a local squid with a domain allowlist."""
    uv: str = "uv"
    """uv on the host, builds the sandboxes' shared Python environment."""
    python: str = "python3"
    """Interpreter of that environment, must be readable in the sandbox."""


class KnowledgeSource(BaseModel):
    link: str
    """Where it appears in the work folder: knowledge/skills, knowledge/books, or e.g. .se-dev for data the
    skills expect in the home folder."""
    url: str | None = None
    """Git repository, mirrored by `clangy sync-knowledge`."""
    path: Path | None = None
    """Folder used in place."""

    @model_validator(mode="after")
    def _check(self) -> "KnowledgeSource":
        if (self.url is None) == (self.path is None):
            raise ValueError("a knowledge source needs either url or path")
        link = PurePosixPath(self.link)
        if link.is_absolute() or ".." in link.parts or not link.parts:
            raise ValueError(f"link must be a relative path inside the work folder: {self.link}")
        return self


class Config(BaseModel):
    data_dir: Path
    workspace_dir: Path | None = None
    """Tickets, notes and plans, kept past retention_days. Defaults to `<data_dir>/workspace`."""
    discord_token: str = ""
    """Bot token; falls back to the DISCORD_TOKEN environment variable. Keep the file readable only by the bot."""
    backends: list[BackendConfig]
    context_window: int | None = Field(None, gt=0)
    """Caps the context on every backend. Defaults to the smallest backend context_window, so a conversation still
    fits after it moves to another backend."""
    headers: dict[str, str] = Field(default_factory=lambda: dict(PI_ATTRIBUTION))
    """Sent with every backend's chat completion requests; a backend's own headers override single keys. OpenRouter
    serves some free models only to the agent harnesses it lists, so this names pi."""
    channels: list[ChannelConfig] = Field(default_factory=list)
    run_deadline: float = 600.0
    attachment_max_bytes: int = 25 * 1024**2
    answer_max_bytes: int = 10 * 1024**2
    """Total size of the files attached to one answer."""
    progress_interval: float = 60.0
    health_interval: float = 30.0
    max_retries: int = 3
    switch_margin: float = 60.0
    """A conversation moves to another backend only if that's faster by this much, after reading the whole prompt."""
    retention_days: float = Field(30.0, gt=0)
    """Conversations and records not touched for this long are deleted at startup, the message cache sooner."""
    secret_markers: list[str] = Field(default_factory=lambda: ["se-dev-books", "se2-dev-books", "knowledge/books"])
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    knowledge: dict[Game, list[KnowledgeSource]] = Field(default_factory=dict)
    mounts: list[KnowledgeSource] = Field(default_factory=list)
    """Like knowledge, but linked into every conversation whatever the game, e.g. the ecosystem's repos."""

    @model_validator(mode="after")
    def _merge_headers(self) -> "Config":
        for backend in self.backends:
            backend.headers = self.headers | backend.headers
        return self

    def context_limit(self) -> int:
        return self.context_window or min(b.context_window for b in self.backends)

    def channel(self, channel_id: int) -> ChannelConfig | None:
        return next((c for c in self.channels if c.id == channel_id), None)

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            return cls.model_validate(tomllib.load(f))

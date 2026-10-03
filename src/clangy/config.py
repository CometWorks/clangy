import tomllib
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

Game = Literal["se1", "se2"]
ChannelMode = Literal["support", "record", "mention"]


class BackendConfig(BaseModel):
    name: str
    url: str
    """OpenAI compatible base URL, including the `/v1` suffix."""
    model: str
    api_key_env: str | None = None
    parallel: int = 1
    kind: Literal["openai", "llamacpp"] = "openai"
    slot_save: bool = False
    """llama.cpp only: save/restore slot KV caches, needs `--slot-save-path` on the server."""
    context_window: int = 131072
    max_tokens: int = 4096
    thinking: str = "off"
    compat: dict[str, Any] = Field(default_factory=dict)
    prompt_tps: float = 300.0
    gen_tps: float = 40.0

    @property
    def root_url(self) -> str:
        return self.url.removesuffix("/").removesuffix("/v1")


class ChannelConfig(BaseModel):
    id: int
    mode: ChannelMode
    initial_response: bool = False
    initial_delay: float = 0.0
    """Forums: the first answer to a new thread waits until nobody wrote in it for this long."""
    history_messages: int = 20
    """Mention channels: at most this many earlier channel messages go along as context."""
    history_age: float = 3600.0
    """Mention channels: only earlier messages this recent go along as context."""
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
    discord_token: str = ""
    """Bot token; falls back to the DISCORD_TOKEN environment variable. Keep the file readable only by the bot."""
    backends: list[BackendConfig]
    channels: list[ChannelConfig] = Field(default_factory=list)
    run_deadline: float = 600.0
    attachment_max_bytes: int = 25 * 1024**2
    answer_max_bytes: int = 10 * 1024**2
    """Total size of the files attached to one answer."""
    progress_interval: float = 60.0
    health_interval: float = 30.0
    max_retries: int = 3
    secret_markers: list[str] = Field(default_factory=lambda: ["se-dev-books", "se2-dev-books", "knowledge/books"])
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    knowledge: dict[Game, list[KnowledgeSource]] = Field(default_factory=dict)

    def channel(self, channel_id: int) -> ChannelConfig | None:
        return next((c for c in self.channels if c.id == channel_id), None)

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            return cls.model_validate(tomllib.load(f))

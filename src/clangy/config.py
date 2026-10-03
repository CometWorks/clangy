import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

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
    game: Game | None = None
    prompt: str = ""


class SandboxConfig(BaseModel):
    firejail: bool = True
    pi_command: list[str] = ["pi"]
    path: str = "/usr/local/bin:/usr/bin:/bin"
    """PATH inside the sandbox, must reach node, pi and python3."""
    read_only: list[str] = Field(default_factory=list)
    """Extra host paths the sandbox may read, e.g. the node installation."""
    memory_limit: int = 4 * 1024**3


class KnowledgeRepo(BaseModel):
    url: str
    mount: Literal["skills", "books"]


class Config(BaseModel):
    data_dir: Path
    token_env: str = "DISCORD_TOKEN"
    backends: list[BackendConfig]
    channels: list[ChannelConfig] = Field(default_factory=list)
    run_deadline: float = 600.0
    attachment_max_bytes: int = 25 * 1024**2
    answer_max_bytes: int = 10 * 1024**2
    """Total size of the files attached to one answer."""
    progress_interval: float = 10.0
    health_interval: float = 30.0
    max_retries: int = 3
    secret_markers: list[str] = Field(default_factory=lambda: ["se-dev-books", "se2-dev-books", "knowledge/books"])
    sandbox: SandboxConfig = Field(default_factory=SandboxConfig)
    knowledge: dict[Game, list[KnowledgeRepo]] = Field(default_factory=dict)

    def channel(self, channel_id: int) -> ChannelConfig | None:
        return next((c for c in self.channels if c.id == channel_id), None)

    @classmethod
    def load(cls, path: Path) -> "Config":
        with open(path, "rb") as f:
            return cls.model_validate(tomllib.load(f))

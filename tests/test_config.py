from pathlib import Path

import pytest
from pydantic import ValidationError

from clangy.config import BackendConfig, Config, KnowledgeSource

ROOT = Path(__file__).parent.parent


def test_example_config_loads():
    config = Config.load(ROOT / "config.example.toml")
    assert config.backends[0].root_url == "http://192.168.1.20:8080"
    assert config.channel(100000000000000003).mode == "mention"
    assert config.channel(1) is None
    assert [s.link for s in config.knowledge["se2"]] == ["knowledge/skills", "knowledge/books", ".se2-dev"]


@pytest.mark.parametrize(
    "source",
    [
        {"link": "knowledge/x"},
        {"link": "x", "url": "u", "path": "/p"},
        {"link": "../x", "path": "/p"},
        {"link": "/x", "path": "/p"},
    ],
)
def test_invalid_knowledge_sources(source):
    with pytest.raises(ValidationError):
        KnowledgeSource.model_validate(source)


def test_context_limit_defaults_to_the_smallest_backend(tmp_path):
    backends = [
        BackendConfig(name=n, url="http://x/v1", model="m", context_window=w) for n, w in [("a", 400000), ("b", 184320)]
    ]
    assert Config(data_dir=tmp_path, backends=backends).context_limit() == 184320
    assert Config(data_dir=tmp_path, backends=backends, context_window=100000).context_limit() == 100000


def test_reasoning_in_extra_body_is_refused():
    with pytest.raises(ValidationError, match="thinking"):
        BackendConfig(name="b", url="http://x/v1", model="m", extra_body={"reasoning": {"effort": "minimal"}})

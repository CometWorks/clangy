from pathlib import Path

import pytest
from pydantic import ValidationError

from clangy.config import PI_ATTRIBUTION, BackendConfig, Config, KnowledgeSource

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


def test_backends_send_pi_attribution_unless_overridden(tmp_path):
    backends = [
        BackendConfig(name="a", url="http://x/v1", model="m"),
        BackendConfig(name="b", url="http://x/v1", model="m", headers={"X-OpenRouter-Title": "b", "X-Extra": "1"}),
    ]
    a, b = Config(data_dir=tmp_path, backends=backends).backends
    assert a.headers == PI_ATTRIBUTION
    assert b.headers == PI_ATTRIBUTION | {"X-OpenRouter-Title": "b", "X-Extra": "1"}


def test_reasoning_in_extra_body_is_refused():
    with pytest.raises(ValidationError, match="thinking"):
        BackendConfig(name="b", url="http://x/v1", model="m", extra_body={"reasoning": {"effort": "minimal"}})


def test_unknown_keys_are_refused(tmp_path):
    with pytest.raises(ValidationError, match="initial_dealy"):
        Config.model_validate(
            {"data_dir": tmp_path, "backends": [], "channels": [{"id": 1, "mode": "support", "initial_dealy": 5}]}
        )

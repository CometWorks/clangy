from pathlib import Path

from clangy.config import Config

ROOT = Path(__file__).parent.parent


def test_example_config_loads():
    config = Config.load(ROOT / "config.example.toml")
    assert config.backends[0].root_url == "http://192.168.1.20:8080"
    assert config.channel(100000000000000003).mode == "mention"
    assert config.channel(1) is None
    assert [r.mount for r in config.knowledge["se2"]] == ["skills", "books"]

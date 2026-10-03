from clangy.check import sandbox_problems
from clangy.config import BackendConfig, Config


async def test_missing_firejail_is_a_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    config = Config(data_dir=tmp_path, backends=[BackendConfig(name="b", url="http://x/v1", model="m")])
    assert await sandbox_problems(config, tmp_path / "config.toml") == ["firejail installed"]

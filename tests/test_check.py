from clangy.check import sandbox_problems
from clangy.config import BackendConfig, Config


async def test_missing_firejail_is_a_problem(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    config = Config(data_dir=tmp_path, backends=[BackendConfig(name="b", url="http://x/v1", model="m")])
    assert await sandbox_problems(config, tmp_path / "config.toml") == ["firejail installed"]


async def test_probe_checks_the_proxy(tmp_path, squid):
    from clangy.check import probe_sandbox
    from clangy.config import SandboxConfig

    sandbox = SandboxConfig(firejail=False, path="/usr/bin:/bin", proxy=f"127.0.0.1:{squid.port}")
    config = Config(
        data_dir=tmp_path, backends=[BackendConfig(name="b", url="http://x/v1", model="m")], sandbox=sandbox
    )
    # Unsandboxed, so only the proxy results are meaningful here; the network test would reach out, skip it
    results = await probe_sandbox(config, tmp_path / "config.toml", tmp_path / "check", network=False)
    assert results["proxy reachable"] and results["proxy blocks other sites"]
    squid.allowed.add("example.com")
    results = await probe_sandbox(config, tmp_path / "config.toml", tmp_path / "check2", network=False)
    assert not results["proxy blocks other sites"]

import json

import aiohttp

from clangy.backend import Backend
from clangy.config import BackendConfig
from clangy.proxy import HostServer, ProxyRun, ToolError, short_path


async def tools(name: str, params: dict) -> str:
    if name == "fail":
        raise ToolError("nope")
    return f"{name}: {params['x']}"


def make_backend(url: str) -> Backend:
    return Backend(BackendConfig(name="b", url=url, model="real-model", api_key_env="CLANGY_TEST_KEY"))


async def post(socket, path, body):
    with short_path(socket) as short:
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(path=short)) as http:
            async with http.post(f"http://host{path}", json=body) as r:
                return r.status, await r.text()


async def test_forwards_chat_with_key_slot_and_records_usage(backend, tmp_path, monkeypatch):
    monkeypatch.setenv("CLANGY_TEST_KEY", "secret")
    async with aiohttp.ClientSession() as http:
        run = ProxyRun(backend=make_backend(backend.url), session=http, tools=tools, slot=3)
        async with HostServer(run, tmp_path / "host.sock"):
            status, body = await post(tmp_path / "host.sock", "/v1/chat/completions", {"model": "x", "messages": []})
    assert status == 200 and "Hello from the fake backend." in body
    assert backend.requests[0]["model"] == "real-model"
    assert backend.requests[0]["id_slot"] == 3
    assert backend.headers[0]["Authorization"] == "Bearer secret"
    assert (run.stats.prompt_tokens, run.stats.cached_tokens, run.stats.completion_tokens) == (100, 80, 7)
    assert not run.stats.backend_failed
    assert run.backend.prompt_tps > 300


async def test_short_prompts_are_no_speed_sample(backend, tmp_path):
    backend.processed_tokens = 20
    async with aiohttp.ClientSession() as http:
        run = ProxyRun(backend=make_backend(backend.url), session=http, tools=tools)
        async with HostServer(run, tmp_path / "host.sock"):
            await post(tmp_path / "host.sock", "/v1/chat/completions", {"messages": []})
    assert run.backend.prompt_tps == 300.0
    assert run.backend.gen_tps > 40.0


async def test_backend_down_is_flagged(backend, tmp_path):
    url = backend.url
    await backend.stop()
    async with aiohttp.ClientSession() as http:
        run = ProxyRun(backend=make_backend(url), session=http, tools=tools)
        async with HostServer(run, tmp_path / "host.sock"):
            status, _ = await post(tmp_path / "host.sock", "/v1/chat/completions", {"messages": []})
    assert status == 502
    assert run.stats.backend_failed


async def test_backend_server_error_is_flagged(backend, tmp_path):
    backend.status = 503
    async with aiohttp.ClientSession() as http:
        run = ProxyRun(backend=make_backend(backend.url), session=http, tools=tools)
        async with HostServer(run, tmp_path / "host.sock"):
            status, _ = await post(tmp_path / "host.sock", "/v1/chat/completions", {"messages": []})
    assert status == 503
    assert run.stats.backend_failed


async def test_deep_socket_path(tmp_path):
    deep = tmp_path.joinpath(*["d" * 50] * 4) / "host.sock"
    assert len(str(deep)) > 200
    async with aiohttp.ClientSession() as http:
        run = ProxyRun(backend=make_backend("http://127.0.0.1:1/v1"), session=http, tools=tools)
        async with HostServer(run, deep):
            assert await post(deep, "/tools/echo", {"x": 2}) == (200, json.dumps({"text": "echo: 2"}))


async def test_tools(tmp_path):
    async with aiohttp.ClientSession() as http:
        run = ProxyRun(backend=make_backend("http://127.0.0.1:1/v1"), session=http, tools=tools)
        async with HostServer(run, tmp_path / "host.sock"):
            assert await post(tmp_path / "host.sock", "/tools/echo", {"x": 1}) == (200, json.dumps({"text": "echo: 1"}))
            status, body = await post(tmp_path / "host.sock", "/tools/fail", {})
    assert status == 400 and json.loads(body) == {"error": "nope"}
    assert not (tmp_path / "host.sock").exists()

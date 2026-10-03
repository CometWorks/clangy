import asyncio
import json

import aiohttp

from clangy.backend import Backend
from clangy.config import BackendConfig
from clangy.proxy import HostServer, ProxyRun, TcpRelay, ToolError, short_path


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
    [record] = run.stats.per_request
    assert record["status"] == 200 and record["slot"] == 3 and record["time"].endswith("+00:00")
    assert record["timings"]["prompt_n"] == 1000 and "first_token_seconds" in record
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
    assert run.stats.per_request[0]["timings"]["prompt_per_second"] == 500.0, "raw numbers are still recorded"


async def test_backend_down_is_flagged(backend, tmp_path):
    url = backend.url
    await backend.stop()
    async with aiohttp.ClientSession() as http:
        run = ProxyRun(backend=make_backend(url), session=http, tools=tools)
        async with HostServer(run, tmp_path / "host.sock"):
            status, _ = await post(tmp_path / "host.sock", "/v1/chat/completions", {"messages": []})
    assert status == 502
    assert run.stats.backend_failed
    [record] = run.stats.per_request
    assert "ClientConnectorError" in record["error"]


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


async def connect_via(socket_path, host: str) -> bytes:
    with short_path(socket_path) as short:
        reader, writer = await asyncio.open_unix_connection(short)
    writer.write(f"CONNECT {host}:443 HTTP/1.1\r\n\r\n".encode())
    await writer.drain()
    answer = await reader.read(100)
    writer.close()
    return answer


async def test_relay_reaches_the_proxy(tmp_path, squid):
    deep = tmp_path.joinpath(*["r" * 50] * 4) / "proxy.sock"
    async with TcpRelay(deep, f"127.0.0.1:{squid.port}"):
        assert (await connect_via(deep, "pypi.org")).startswith(b"HTTP/1.1 200")
        assert (await connect_via(deep, "example.com")).startswith(b"HTTP/1.1 403")
    assert squid.requests == ["pypi.org:443", "example.com:443"]
    assert not deep.exists()


async def test_relay_closes_when_the_proxy_is_down(tmp_path, squid):
    await squid.stop()
    async with TcpRelay(tmp_path / "proxy.sock", f"127.0.0.1:{squid.port}"):
        assert await connect_via(tmp_path / "proxy.sock", "pypi.org") == b""

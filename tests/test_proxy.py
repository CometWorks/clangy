import asyncio
import json

import aiohttp
import pytest

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
            status, body = await post(deep, "/tools/echo", {"x": 2})
            assert (status, json.loads(body)) == (200, {"text": "echo: 2", "stop": False})


async def test_tools(tmp_path):
    async with aiohttp.ClientSession() as http:
        run = ProxyRun(backend=make_backend("http://127.0.0.1:1/v1"), session=http, tools=tools)
        async with HostServer(run, tmp_path / "host.sock"):
            status, body = await post(tmp_path / "host.sock", "/tools/echo", {"x": 1})
            assert (status, json.loads(body)) == (200, {"text": "echo: 1", "stop": False})
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


async def chat_once(backend_config: BackendConfig, tmp_path, requests: int = 1) -> tuple[ProxyRun, list]:
    replies = []
    async with aiohttp.ClientSession() as http:
        run = ProxyRun(backend=Backend(backend_config), session=http, tools=tools)
        async with HostServer(run, tmp_path / "host.sock"):
            for _ in range(requests):
                replies.append(await post(tmp_path / "host.sock", "/v1/chat/completions", {"messages": []}))
    return run, replies


def cloud(url: str, **kwargs) -> BackendConfig:
    return BackendConfig(name="cloud", url=url, model="a/model:free", **kwargs)


async def test_extra_body_and_key_from_config_reach_the_backend(backend, tmp_path):
    extra = {"models": ["a/model:free", "b/model:free"], "provider": {"data_collection": "allow"}}
    backend.answering_model = "b/model:free"
    run, _ = await chat_once(cloud(backend.url, api_key="sk-test", extra_body=extra), tmp_path)
    request = backend.requests[0]
    assert request["model"] == "a/model:free" and request["models"] == extra["models"]
    assert request["provider"] == {"data_collection": "allow"}
    assert backend.headers[0]["Authorization"] == "Bearer sk-test"
    assert run.stats.per_request[0]["model"] == "b/model:free", "the model that answered"


async def test_headers_from_config_reach_the_backend(backend, tmp_path):
    await chat_once(cloud(backend.url, headers={"HTTP-Referer": "https://pi.dev"}), tmp_path)
    assert backend.headers[0]["HTTP-Referer"] == "https://pi.dev"


@pytest.mark.parametrize("code", [403, 429])
async def test_rate_and_key_limits_are_backend_failures(backend, tmp_path, code):
    backend.status = code
    run, [(status, _)] = await chat_once(cloud(backend.url), tmp_path)
    assert status == code and run.stats.backend_failed and run.stats.retry_after is None
    assert run.stats.per_request[0]["error"] == f"HTTP {code}"


async def test_rejected_request_is_no_backend_failure(backend, tmp_path):
    backend.status = 400
    run, _ = await chat_once(cloud(backend.url), tmp_path)
    assert not run.stats.backend_failed


async def test_retry_after_is_kept(backend, tmp_path):
    backend.status, backend.retry_after = 429, "120"
    run, _ = await chat_once(cloud(backend.url), tmp_path)
    assert run.stats.retry_after == 120.0


async def test_no_first_data_in_time_fails(backend, tmp_path):
    backend.stall_first = 1.0
    run, [(status, _)] = await chat_once(cloud(backend.url, first_byte_timeout=0.2), tmp_path)
    assert status == 502 and run.stats.backend_failed
    assert "TimeoutError" in run.stats.per_request[0]["error"]


async def test_keep_alive_comments_are_no_first_data(backend, tmp_path):
    backend.stall_first, backend.keepalive = 1.0, True
    run, [(status, body)] = await chat_once(cloud(backend.url, first_byte_timeout=0.3), tmp_path)
    assert run.stats.backend_failed
    assert '"error"' in body, "pi gets an error event, not a cut-off stream"


async def test_slow_first_data_is_fine_without_timeout(backend, tmp_path):
    backend.stall_first = 0.3
    run, [(status, body)] = await chat_once(cloud(backend.url, stall_timeout=0.1), tmp_path)
    assert status == 200 and "Hello from the fake backend." in body and not run.stats.backend_failed


async def test_stalled_stream_fails(backend, tmp_path):
    backend.stall_mid = 1.0
    run, [(status, body)] = await chat_once(cloud(backend.url, stall_timeout=0.2), tmp_path)
    assert status == 200 and run.stats.backend_failed
    assert body.rstrip().endswith('"}}'), "ends with the error event"


async def test_error_inside_the_stream_fails(backend, tmp_path):
    backend.stream_error = True
    run, [(status, _)] = await chat_once(cloud(backend.url), tmp_path)
    assert status == 200 and run.stats.backend_failed
    assert "Provider returned error" in run.stats.per_request[0]["error"]


async def test_no_more_requests_after_a_failure(backend, tmp_path):
    backend.status = 429
    run, replies = await chat_once(cloud(backend.url), tmp_path, requests=2)
    assert [status for status, _ in replies] == [429, 502]
    assert len(backend.requests) == 1

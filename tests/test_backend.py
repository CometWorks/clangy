import asyncio

import aiohttp

from clangy.backend import Backend
from clangy.config import BackendConfig


def config(**kwargs) -> BackendConfig:
    return BackendConfig(name="b", url="http://127.0.0.1:1/v1", model="m", **kwargs)


async def test_runs_ahead_and_parallel_limit():
    backend = Backend(config(parallel=1))
    await backend.acquire("a")
    second = asyncio.create_task(backend.acquire("b"))
    third = asyncio.create_task(backend.acquire("c"))
    await asyncio.sleep(0.01)
    assert [backend.ahead_of(c) for c in "abcd"] == [0, 1, 2, 3]
    await backend.release("a")
    await second
    assert backend.ahead_of("c") == 1
    assert not third.done()
    await backend.release("b")
    await third


async def test_estimate_uses_measured_speeds_and_run_times():
    backend = Backend(config(prompt_tps=100, gen_tps=10))
    backend.run_seconds = 5.0
    assert backend.estimate("x", prompt_tokens=1000, answer_tokens=100) == 20.0
    backend.record_speed(200, None)
    assert backend.prompt_tps == 130
    backend.record_run(105.0)
    assert backend.run_seconds == 35.0
    assert backend.estimate("x", prompt_tokens=1000, answer_tokens=100) == 35.0


async def test_slots_are_pinned_saved_and_restored(backend):
    b = Backend(config(kind="llamacpp", parallel=2, slot_save=True).model_copy(update={"url": backend.url}))
    async with aiohttp.ClientSession() as http:
        for conversation in ("a", "b"):
            await b.acquire(conversation)
            assert await b.take_slot(http, conversation, now=1.0 if conversation == "a" else 2.0) in (0, 1)
            await b.release(conversation)
        slot_a = await b.take_slot(http, "a", now=3.0)
        assert backend.slot_actions == []
        # A third conversation evicts the least recently used slot, which is b's
        slot_c = await b.take_slot(http, "c", now=4.0)
        assert slot_c != slot_a
        assert backend.slot_actions == [(slot_c, "save", "clangy-b.bin")]
        await b.take_slot(http, "b", now=5.0)
        assert backend.slot_actions[-2:] == [(slot_a, "save", "clangy-a.bin"), (slot_a, "restore", "clangy-b.bin")]


async def test_health_check(backend):
    b = Backend(config().model_copy(update={"url": backend.url}))
    async with aiohttp.ClientSession() as http:
        assert await b.check_health(http)
        await backend.stop()
        assert not await b.check_health(http)
        assert not b.healthy


async def test_cloud_backend_returns_after_its_cooldown():
    backend = Backend(config(cooldown=60))
    backend.mark_failed(now=100.0)
    assert not backend.available(159.0)
    assert backend.available(160.0)
    backend.mark_failed(now=200.0, retry_after=300.0)
    assert not backend.available(450.0) and backend.available(500.0)


async def test_cooldown_doubles_with_each_failure_in_a_row():
    backend = Backend(config(cooldown=60, max_cooldown=200))
    backend.mark_failed(now=0.0)
    backend.mark_failed(now=30.0)
    assert backend.failures == 1, "a run that was going when it failed doesn't count again"
    assert not backend.available(89.0) and backend.available(90.0)
    backend.mark_failed(now=100.0)
    assert not backend.available(219.0) and backend.available(220.0)
    backend.mark_failed(now=300.0)
    assert not backend.available(499.0) and backend.available(500.0), "capped at max_cooldown"
    backend.mark_ok()
    backend.mark_failed(now=600.0)
    assert backend.available(660.0)


async def test_llamacpp_returns_only_after_a_health_check(backend):
    b = Backend(config(kind="llamacpp").model_copy(update={"url": backend.url}))
    b.mark_failed(now=0.0)
    assert not b.available(10**6)
    async with aiohttp.ClientSession() as http:
        await b.check_health(http)
    assert b.available(0.0)


async def test_api_key_from_config_or_environment(monkeypatch):
    monkeypatch.setenv("CLANGY_TEST_KEY", "from-env")
    assert Backend(config(api_key_env="CLANGY_TEST_KEY")).api_key == "from-env"
    assert Backend(config(api_key="from-config", api_key_env="CLANGY_TEST_KEY")).api_key == "from-config"

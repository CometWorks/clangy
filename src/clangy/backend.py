"""Inference backends: run queue, measured speeds, health and llama.cpp slot handling."""

import asyncio
import logging
import os

import aiohttp

from clangy.config import BackendConfig

log = logging.getLogger(__name__)

EMA_WEIGHT = 0.3
DEFAULT_RUN_SECONDS = 60.0


class Backend:
    def __init__(self, config: BackendConfig):
        self.config = config
        self.prompt_tps = config.prompt_tps
        self.gen_tps = config.gen_tps
        self.run_seconds = DEFAULT_RUN_SECONDS
        self.healthy = True
        self._cooldown_until = 0.0
        self._waiting: list[str] = []
        self._running: set[str] = set()
        self._changed = asyncio.Condition()
        self._slot_owner: list[str | None] = [None] * config.parallel
        self._slot_last_use = [0.0] * config.parallel
        self._saved: set[str] = set()

    @property
    def name(self) -> str:
        return self.config.name

    @property
    def api_key(self) -> str | None:
        if self.config.api_key:
            return self.config.api_key
        return os.environ.get(self.config.api_key_env) if self.config.api_key_env else None

    def available(self, now: float) -> bool:
        """Whether runs may use it. llama.cpp comes back with a passed health check, other kinds after a cooldown:
        a cloud API's model list answers even while its models are rate limited."""
        if not self.healthy and self.config.kind != "llamacpp" and now >= self._cooldown_until:
            self.healthy = True
        return self.healthy

    def mark_failed(self, now: float, retry_after: float | None = None) -> None:
        self.healthy = False
        self._cooldown_until = now + max(self.config.cooldown, retry_after or 0.0)

    def ahead_of(self, conversation: str) -> int:
        """Runs that go before this conversation's: the running ones and those queued earlier."""
        if conversation in self._running:
            return 0
        queued = self._waiting.index(conversation) if conversation in self._waiting else len(self._waiting)
        return len(self._running) + queued if len(self._running) + queued >= self.config.parallel else 0

    async def acquire(self, conversation: str) -> None:
        async with self._changed:
            self._waiting.append(conversation)
            self._changed.notify_all()
            try:
                await self._changed.wait_for(
                    lambda: len(self._running) < self.config.parallel and self._waiting[0] == conversation
                )
            finally:
                self._waiting.remove(conversation)
            self._running.add(conversation)
            self._changed.notify_all()

    def record_run(self, seconds: float) -> None:
        """Records the duration of a successful run, the floor of later estimates."""
        self.run_seconds += EMA_WEIGHT * (seconds - self.run_seconds)

    async def release(self, conversation: str) -> None:
        async with self._changed:
            self._running.discard(conversation)
            self._changed.notify_all()

    def record_speed(self, prompt_tps: float | None, gen_tps: float | None) -> None:
        if prompt_tps:
            self.prompt_tps += EMA_WEIGHT * (prompt_tps - self.prompt_tps)
        if gen_tps:
            self.gen_tps += EMA_WEIGHT * (gen_tps - self.gen_tps)

    def estimate(self, conversation: str, prompt_tokens: int, answer_tokens: int) -> float:
        """Seconds until the answer is expected.

        Runs with tool calls take several model turns, so the typical run time is the floor.
        """
        queued = self.ahead_of(conversation) * self.run_seconds / self.config.parallel
        tokens = prompt_tokens / self.prompt_tps + answer_tokens / self.gen_tps
        return queued + max(tokens, self.run_seconds)

    async def check_health(self, session: aiohttp.ClientSession) -> bool:
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            async with session.get(
                f"{self.config.url}/models", headers=headers, timeout=aiohttp.ClientTimeout(10)
            ) as r:
                self.healthy = r.status < 500
        except (aiohttp.ClientError, TimeoutError):
            self.healthy = False
        return self.healthy

    async def take_slot(self, session: aiohttp.ClientSession, conversation: str, now: float) -> int | None:
        """Pins the conversation to a llama.cpp slot, so its KV cache survives other conversations' turns."""
        if self.config.kind != "llamacpp":
            return None
        busy = {i for i, owner in enumerate(self._slot_owner) if owner in self._running and owner != conversation}
        if conversation in self._slot_owner:
            slot = self._slot_owner.index(conversation)
        else:
            free = [i for i in range(self.config.parallel) if i not in busy]
            slot = min(free, key=lambda i: self._slot_last_use[i])
            await self._swap_slot(session, slot, conversation)
        self._slot_last_use[slot] = now
        return slot

    async def _swap_slot(self, session: aiohttp.ClientSession, slot: int, conversation: str) -> None:
        previous = self._slot_owner[slot]
        self._slot_owner[slot] = conversation
        if not self.config.slot_save:
            return
        if previous is not None and await self._slot_action(session, slot, "save", previous):
            self._saved.add(previous)
        if conversation in self._saved:
            await self._slot_action(session, slot, "restore", conversation)

    async def _slot_action(self, session: aiohttp.ClientSession, slot: int, action: str, conversation: str) -> bool:
        url = f"{self.config.root_url}/slots/{slot}?action={action}"
        try:
            async with session.post(url, json={"filename": f"clangy-{conversation}.bin"}) as r:
                ok = r.status == 200
        except aiohttp.ClientError:
            ok = False
        if not ok:
            log.warning("Slot %s of slot %d for conversation %s failed on %s", action, slot, conversation, self.name)
        return ok

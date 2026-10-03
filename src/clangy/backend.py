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
        return os.environ.get(self.config.api_key_env) if self.config.api_key_env else None

    def queue_position(self, conversation: str) -> int:
        """Number of runs ahead of this conversation in the queue, 0 once it is running."""
        if conversation in self._running or conversation not in self._waiting:
            return 0
        return self._waiting.index(conversation) + 1

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

    async def release(self, conversation: str, seconds: float | None = None) -> None:
        if seconds is not None:
            self.run_seconds += EMA_WEIGHT * (seconds - self.run_seconds)
        async with self._changed:
            self._running.discard(conversation)
            self._changed.notify_all()

    async def wait_for_change(self) -> None:
        async with self._changed:
            await self._changed.wait()

    def record_speed(self, prompt_tps: float | None, gen_tps: float | None) -> None:
        if prompt_tps:
            self.prompt_tps += EMA_WEIGHT * (prompt_tps - self.prompt_tps)
        if gen_tps:
            self.gen_tps += EMA_WEIGHT * (gen_tps - self.gen_tps)

    def estimate(self, conversation: str, prompt_tokens: int, answer_tokens: int) -> float:
        """Seconds until the answer is expected."""
        queued = self.queue_position(conversation) * self.run_seconds / self.config.parallel
        return queued + prompt_tokens / self.prompt_tps + answer_tokens / self.gen_tps

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

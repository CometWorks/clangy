import socket

import pytest

from tests.fakes import FakeBackend, FakeChat, FakeSquid

_connect = socket.socket.connect


def _offline_connect(self: socket.socket, address) -> None:
    """The suite must run offline: only unix sockets and loopback are allowed."""
    if self.family in (socket.AF_INET, socket.AF_INET6) and address[0] not in ("127.0.0.1", "::1", "localhost"):
        raise RuntimeError(f"network access in tests: {address}")
    return _connect(self, address)


socket.socket.connect = _offline_connect  # type: ignore[method-assign]


@pytest.fixture
async def backend():
    fake = FakeBackend()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def backend2():
    fake = FakeBackend()
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
async def squid():
    fake = FakeSquid({"pypi.org"})
    await fake.start()
    yield fake
    await fake.stop()


@pytest.fixture
def chat() -> FakeChat:
    return FakeChat()

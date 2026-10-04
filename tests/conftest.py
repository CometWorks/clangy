import os
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


FAKE_UV = """#!/bin/sh
# Offline stand-in for uv: records the call and creates the environment's python
echo "$@" >> .uv-calls
mkdir -p .venv/bin && ln -sf "$(command -v python3)" .venv/bin/python
"""


@pytest.fixture(scope="session")
def fake_uv_dir(tmp_path_factory) -> str:
    folder = tmp_path_factory.mktemp("fake-uv")
    (folder / "uv").write_text(FAKE_UV)
    (folder / "uv").chmod(0o755)
    return str(folder)


@pytest.fixture(autouse=True)
def offline_uv(monkeypatch, fake_uv_dir) -> None:
    """Work folders get their Python environment from uv, which would download packages."""
    monkeypatch.setenv("PATH", f"{fake_uv_dir}:{os.environ['PATH']}")


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

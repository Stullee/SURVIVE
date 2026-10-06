"""The agent's code runs with the network and other programs blocked."""

from __future__ import annotations

import _thread
import ctypes.util
import os
import socket
import subprocess
import sys
import threading

import pytest

from app.agent import netguard


@pytest.mark.parametrize(
    "action",
    [
        lambda: socket.socket(socket.AF_INET, socket.SOCK_STREAM),
        lambda: socket.getaddrinfo("api.anthropic.com", 443),
        lambda: socket.create_connection(("127.0.0.1", 9), timeout=0.1),
        lambda: subprocess.run(["true"], check=False),  # noqa: S603, S607
        lambda: os.system("true"),  # noqa: S605, S607
        lambda: threading.Thread(target=lambda: None).start(),
        lambda: _thread.start_new_thread(lambda: None, ()),
        lambda: ctypes.CDLL(ctypes.util.find_library("c")),
        lambda: __import__("urllib.request").request.urlopen("http://127.0.0.1:9", timeout=0.1),  # noqa: S310
    ],
)
def test_sealed_code_can_not_reach_out(action) -> None:  # noqa: ANN001
    # PermissionError, or a library's OSError wrapping it (urllib's URLError).
    with netguard.sealed(), pytest.raises(OSError, match="not allowed"):
        action()


def test_only_the_sealed_thread_is_affected() -> None:
    results: list[str] = []

    def other() -> None:
        sock = socket.socket()
        sock.close()
        results.append("ok")

    with netguard.sealed():
        assert netguard.is_sealed()
    thread = threading.Thread(target=other)
    thread.start()
    thread.join()
    assert results == ["ok"]
    assert not netguard.is_sealed()
    socket.socket().close()  # unsealed again


def test_sealing_nests() -> None:
    with netguard.sealed():
        with netguard.sealed():
            pass
        assert netguard.is_sealed()
    assert not netguard.is_sealed()


def test_the_hook_survives_attempts_to_clear_it() -> None:
    netguard.install()
    with netguard.sealed():
        # There is no API to remove audit hooks; clearing the module state doesn't help either.
        assert not hasattr(sys, "removeaudithook")
        with pytest.raises(PermissionError):
            socket.socket()

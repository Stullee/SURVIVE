"""No network, no other programs: enforced while the agent's code runs.

A Python audit hook (``sys.addaudithook``, which can't be removed once added)
refuses sockets, name lookups, subprocesses, ``os.system``/``exec``/``fork``/
``spawn``, loading native libraries and starting threads, but only in threads
that are *sealed*. The wake-cycle thread is sealed while it works in dry run,
so a bug in a tool handler (or anything it calls) can't reach the network or
start another program. The web server, the database and the rest of the app
are not affected.

In live mode the model transport needs the network, so only the tool
handlers are sealed there (tools.run seals them in both modes).
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

BLOCKED_EVENTS = frozenset(
    {
        "socket.__new__",
        "socket.connect",
        "socket.bind",
        "socket.getaddrinfo",
        "socket.gethostbyname",
        "socket.gethostbyaddr",
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.spawn",
        "os.startfile",
        "ctypes.dlopen",
        "_thread.start_new_thread",
        "webbrowser.open",
    }
)

_state = threading.local()
_install_lock = threading.Lock()
_installed = False


class Blocked(PermissionError):
    """Something the agent's code isn't allowed to do."""


def _hook(event: str, args: tuple[Any, ...]) -> None:
    if event in BLOCKED_EVENTS and getattr(_state, "sealed", False):
        raise Blocked(f"{event} is not allowed: the agent has no network access and can't start programs")


def install() -> None:
    """Add the audit hook (once per process)."""
    global _installed
    with _install_lock:
        if not _installed:
            sys.addaudithook(_hook)
            _installed = True


def is_sealed() -> bool:
    return bool(getattr(_state, "sealed", False))


@contextmanager
def sealed() -> Iterator[None]:
    """Run the enclosed code in this thread with the network and processes blocked."""
    install()
    previous = is_sealed()
    _state.sealed = True
    try:
        yield
    finally:
        _state.sealed = previous

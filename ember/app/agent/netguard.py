"""No network, no other programs: enforced while the agent's code runs.

A Python audit hook (``sys.addaudithook``, which can't be removed once added)
refuses sockets, sends to an address, name lookups, subprocesses,
``os.system``/``exec``/``fork``/``spawn``, loading native libraries with
ctypes and starting threads, but only in threads that are *sealed*. The
wake-cycle thread is sealed while it works in dry run, so a bug in a tool
handler (or anything it calls) can't reach the network or start another
program. The web server, the database and the rest of the app are not
affected.

In live mode the model transport needs the network, so only the tool
handlers are sealed there (tools.run seals them in both modes), but not those
of the tools that call a model (tools.CALLING_TOOLS).

0.40.3 (analysis-0.40.1 codebase, T6): what raises no audit event stays
possible in a sealed thread: ``send``/``sendall`` on a socket connected
before the seal, ``_posixsubprocess.fork_exec`` called directly, work handed
to a thread already running, and importing a native module not yet loaded.
No tool handler holds a socket or a thread pool (analysis-0.40.1, 3.3).
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
        # 0.40.3 (analysis-0.40.1 codebase, T6): a send on a socket opened before the seal (a datagram, or any
        # sendmsg); send and sendall on a connected socket raise no audit event, so they stay possible there
        "socket.sendto",
        "socket.sendmsg",
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.spawn",
        "os.startfile",
        "ctypes.dlopen",
        "_thread.start_new_thread",  # and threading.Thread.start up to Python 3.12
        "_thread.start_joinable_thread",  # threading.Thread.start since Python 3.13
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

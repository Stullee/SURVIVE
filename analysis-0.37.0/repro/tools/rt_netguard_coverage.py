"""Review probe: which audit events a few process/network/native-code entry points raise, against netguard's list.

Benign by construction: a second audit hook raises on every event of interest, so nothing that raises an event goes
on to act (no lookup is made, nothing is loaded); an entry point that raises no event at all only runs /bin/true or
imports a standard-library module.
"""

from __future__ import annotations

import sys

from app.agent import netguard

seen: list[str] = []
WATCH = ("socket.", "subprocess.", "os.", "ctypes.", "_thread.", "import", "webbrowser")


class Stop(Exception):
    pass


armed = False


def hook(event: str, args: tuple) -> None:
    if armed and event.startswith(WATCH) and event not in ("os.listdir", "os.scandir", "open"):
        seen.append(event)
        if not event.startswith("import"):
            raise Stop(event)


netguard.install()
sys.addaudithook(hook)


def probe(label: str, action) -> None:
    global armed
    seen.clear()
    armed = True
    try:
        with netguard.sealed():
            result = action()
        outcome = f"ran ({result!r})"
    except netguard.Blocked as exc:
        outcome = f"blocked by netguard ({exc})"
    except Stop as exc:
        outcome = f"stopped by the probe at event {exc}"
    except Exception as exc:  # noqa: BLE001
        outcome = f"{type(exc).__name__}: {exc}"
    finally:
        armed = False
    blocked = [e for e in seen if e in netguard.BLOCKED_EVENTS]
    print(f"{label}: {outcome}; events {seen}; in BLOCKED_EVENTS: {blocked}")


def reverse_lookup():
    import socket

    return socket.getnameinfo(("127.0.0.1", 0), 0)


def spawn_passfds():
    import multiprocessing.util as util

    pid = util.spawnv_passfds(b"/bin/true", [b"/bin/true"], [])
    import os
    return ("child pid", pid, "exit", os.waitpid(pid, 0)[1])


def import_extension():
    import importlib

    for name in ("_lzma", "_bz2", "_curses", "_dbm", "_gdbm", "_tkinter", "_decimal", "_ctypes_test"):
        if name not in sys.modules:
            try:
                importlib.import_module(name)
                return f"imported {name}"
            except ImportError:
                continue
    return "no unimported extension found"


probe("socket.getnameinfo", reverse_lookup)
probe("multiprocessing.util.spawnv_passfds(/bin/true)", spawn_passfds)
probe("import of a C extension module", import_extension)

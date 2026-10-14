"""Does multiprocessing really start a process while a handler is sealed? Write a marker from the child."""

import multiprocessing
import os
import sys
import tempfile
import time

from app.agent import netguard

netguard.install()

MARKER = os.path.join(tempfile.mkdtemp(prefix="mp-"), "child-ran.txt")


def child(path):
    with open(path, "w") as f:
        f.write(f"child pid {os.getpid()} ran\n")


if __name__ == "__main__":
    for method in ("spawn", "fork", "forkserver"):
        try:
            ctx = multiprocessing.get_context(method)
        except ValueError:
            print(method, "-> not available")
            continue
        marker = MARKER + "." + method
        with netguard.sealed():
            try:
                p = ctx.Process(target=child, args=(marker,))
                p.start()
                p.join(5)
                ran = os.path.exists(marker)
                print(f"{method}: start() did not raise; child actually ran = {ran}")
            except netguard.Blocked:
                print(f"{method}: blocked by netguard")
            except Exception as exc:  # noqa: BLE001
                print(f"{method}: {type(exc).__name__}: {str(exc)[:80]}")

    # Also: os.fork and os.forkpty directly.
    for name in ("fork", "forkpty"):
        with netguard.sealed():
            try:
                fn = getattr(os, name)
                pid = fn()
                if isinstance(pid, tuple):
                    pid = pid[0]
                if pid == 0:
                    os._exit(0)  # child
                os.waitpid(pid, 0)
                print(f"os.{name}: NOT BLOCKED (forked pid {pid})")
            except netguard.Blocked:
                print(f"os.{name}: blocked")
            except Exception as exc:  # noqa: BLE001
                print(f"os.{name}: {type(exc).__name__}: {str(exc)[:60]}")

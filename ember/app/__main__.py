"""Entry point: ``python3 -m app`` (started by the s6 service in the container)."""

from __future__ import annotations

import os

import uvicorn

from .main import app_factory

# Home Assistant Ingress connects to this port (ingress_port in config.yaml).
PORT = int(os.environ.get("EMBER_PORT", "8099"))


def main() -> None:
    uvicorn.run(
        app_factory(),
        host="0.0.0.0",  # noqa: S104 - only reachable on the internal app network; see security.py
        port=PORT,
        log_config=None,  # keep our redacting log setup
        access_log=False,
        server_header=False,
        proxy_headers=False,  # the client address must be the real peer (Ingress proxy), never a header
    )


if __name__ == "__main__":
    main()

"""Entry point: ``python3 -m app`` (started by the s6 service in the container)."""

from __future__ import annotations

import os

import uvicorn

from .main import app_factory, dev_mode_enabled

# Home Assistant Ingress connects to this port (ingress_port in config.yaml).
PORT = int(os.environ.get("EMBER_PORT", "8099"))

# Supervisor API tokens. The s6 run script already removes them; dropping them
# here as well keeps them away from anything this process might start.
SUPERVISOR_TOKEN_VARS = ("SUPERVISOR_TOKEN", "HASSIO_TOKEN")


def drop_supervisor_tokens() -> None:
    for name in SUPERVISOR_TOKEN_VARS:
        os.environ.pop(name, None)


def bind_host() -> str:
    """All interfaces inside the app container (the Supervisor connects over the
    internal network); only this machine in local development, unless EMBER_HOST
    says otherwise (docker-compose sets 0.0.0.0 and publishes the port on localhost)."""
    return os.environ.get("EMBER_HOST") or ("127.0.0.1" if dev_mode_enabled() else "0.0.0.0")  # noqa: S104


def main() -> None:
    drop_supervisor_tokens()
    uvicorn.run(
        app_factory(),
        host=bind_host(),
        port=PORT,
        log_config=None,  # keep our redacting log setup
        access_log=False,
        server_header=False,
        proxy_headers=False,  # the client address must be the real peer (Ingress proxy), never a header
        # Home Assistant kills an app 10 s after asking it to stop (also for cold backups); an open
        # browser connection must not keep the shutdown, and closing the database, waiting.
        timeout_graceful_shutdown=2,
    )


if __name__ == "__main__":
    main()

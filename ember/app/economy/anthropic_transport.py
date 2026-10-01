"""The real model transport: one streamed POST to api.anthropic.com per metered call.

The budget guard (metering.py) decides whether a call may be sent and books its
cost; this module only sends it and reports what happened, as one of the four
outcomes the guard understands:

* ``NotSent``: nothing left this process (no connection, a refused host).
* ``Rejected``: the API refused before generating anything (4xx, 429, 5xx
  before the stream opened).
* ``Interrupted``: the request was sent but the answer broke off, or another
  server error. The cost is unknown, so the guard books the worst case.
* ``Completed``: a whole message with its usage.

Rules: the SDK never retries (``max_retries=0``), so one call is one request;
only ``https://api.anthropic.com`` can be reached (``AllowlistTransport``, which
also ignores proxy variables); the API key is only handed to the SDK and never
logged. The SDK is imported here only, and only when live mode needs it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import socket
import threading
import time
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from .metering import Completed, FilesError, Interrupted, NotSent, Outcome, Rejected, rough_token_count

log = logging.getLogger(__name__)

API_HOST = "api.anthropic.com"
API_URL = f"https://{API_HOST}"
CONNECT_SECONDS = 10.0
WRITE_SECONDS = 60.0
READ_SECONDS = 300.0  # the longest silence between two stream events
TOTAL_SECONDS = 1_800.0  # the longest a whole call may take
SERVER_TOOL_ALLOWANCE = 3_000  # tokens per server tool definition, which count_tokens can't size (0.12.0: 1,000)
# 0.15.0: tokens per file handed to code execution (a container_upload block). count_tokens refused them without the
# code execution tool, which it doesn't size either; the file goes into the container, not the prompt.
UPLOAD_ALLOWANCE = 200
COUNTS_KEPT = 16  # 0.15.0: counts remembered, by what was counted (a work step's guard counted one request 8 times)
FILE_SECONDS = 120.0  # one Files API request (the workshop's inputs and outputs)
UPLOAD_EXPIRY_SECONDS = 3_600  # the workshop's inputs are gone within the hour, even if deleting them fails
_COUNT_FIELDS = ("model", "messages", "system", "tools", "tool_choice", "thinking", "output_config", "cache_control")
_SERVER_TOOL_PREFIXES = ("web_search_", "web_fetch_", "code_execution_")
# Refusals before anything is generated. 0.15.0: a server error before the stream opened is one too, as 529 is (live:
# a 503 was booked at the worst case, $0.15, and warned the owner for good). After the stream opened, the cost is
# unknown: the guard books the worst case.
_REJECTED_STATUSES = frozenset({400, 401, 402, 403, 404, 409, 413, 422, 429, 500, 502, 503, 504, 529})


class _Stopped(Exception):
    pass


def _socket_options() -> list[tuple[int, int, int]]:
    options = [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)]
    for name, value in (("TCP_KEEPIDLE", 60), ("TCP_KEEPINTVL", 60), ("TCP_KEEPCNT", 5)):
        if hasattr(socket, name):
            options.append((socket.IPPROTO_TCP, getattr(socket, name), value))
    return options


def _allowlist_transport(httpx2: Any) -> Any:
    class AllowlistTransport(httpx2.HTTPTransport):
        """Refuses every request that isn't HTTPS to api.anthropic.com (raised as a connect error)."""

        def handle_request(self, request: Any) -> Any:
            url = request.url
            if url.scheme != "https" or url.host != API_HOST or url.port not in (None, 443):
                raise httpx2.ConnectError(f"Ember only talks to {API_URL}, not {url.scheme}://{url.host}")
            return super().handle_request(request)

    # No pooled keep-alive connections: a stale one fails in ways that can't be
    # told apart from a sent request. One TLS handshake per call is cheap here.
    return AllowlistTransport(
        retries=0,
        socket_options=_socket_options(),
        limits=httpx2.Limits(max_connections=4, max_keepalive_connections=0),
    )


class AnthropicTransport:
    simulated = False

    def __init__(self, api_key: str, stop: threading.Event | None = None, *, http_transport: Any = None) -> None:
        """``http_transport`` replaces the allowlist, for tests only."""
        # The SDK reads ANTHROPIC_* variables (extra headers, logging, another base URL); none may apply.
        for name in [n for n in os.environ if n.startswith("ANTHROPIC_")]:
            os.environ.pop(name, None)
        import anthropic  # noqa: PLC0415 - about a second to import; only live mode needs it
        import httpx2  # noqa: PLC0415

        self._anthropic = anthropic
        self._httpx = httpx2
        self._stop = stop or threading.Event()
        # Set when the API refuses the key or the account (bad key, no credit, a spend limit):
        # no further calls until the owner fixes it and restarts the app.
        self.blocked: str | None = None
        self._counts: OrderedDict[str, int] = OrderedDict()
        self._counts_lock = threading.Lock()
        self._not_sent = (
            httpx2.ConnectError,
            httpx2.ConnectTimeout,
            httpx2.PoolTimeout,
            httpx2.WriteError,
            httpx2.WriteTimeout,
            httpx2.LocalProtocolError,
            httpx2.UnsupportedProtocol,
        )
        http_client = anthropic.DefaultHttpxClient(
            transport=http_transport or _allowlist_transport(httpx2), trust_env=False, follow_redirects=False
        )
        self._client = anthropic.Anthropic(
            api_key=api_key,
            base_url=API_URL,
            max_retries=0,
            http_client=http_client,
            timeout=httpx2.Timeout(
                connect=CONNECT_SECONDS, read=READ_SECONDS, write=WRITE_SECONDS, pool=CONNECT_SECONDS
            ),
        )

    def _check_blocking(self, rejected: Rejected) -> None:
        text = rejected.error
        if rejected.status in (401, 403):
            reason = "The Anthropic API refused the API key"
        elif rejected.status == 402 or "billing_error" in text:
            reason = "The Anthropic account has a billing problem (credit balance?)"
        elif (
            "enforced_spend_limit_reached" in text
            or "specified API usage limits" in text
            or ("workspace API usage limits" in text)
        ):
            reason = "The Anthropic spend limit is reached"
        else:
            return
        self.blocked = f"{reason}: {text[:200]}. Fix it in the Anthropic Console or the app options, then restart."
        log.error("%s", self.blocked)

    # --- sizing: a free endpoint with its own rate limit ---

    def count_tokens(self, request: Mapping[str, Any]) -> int:
        """The prompt's size in tokens, with a margin; a rough upper bound if counting fails. 0.15.0: what was counted
        once is answered from memory, a rough bound too: the budget guard sizes one request several times (a work step
        sent 8 count requests for its 2 requests), and each could wait for retries or fail on its own."""
        body = {k: request[k] for k in _COUNT_FIELDS if k in request}
        key = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()
        with self._counts_lock:
            if key in self._counts:
                self._counts.move_to_end(key)
                return self._counts[key]
        counted = self._count(request, body)
        with self._counts_lock:
            self._counts[key] = counted
            while len(self._counts) > COUNTS_KEPT:
                self._counts.popitem(last=False)
        return counted

    def _count(self, request: Mapping[str, Any], body: dict[str, Any]) -> int:
        """One count_tokens request for ``body`` (``request``'s counted fields)."""
        # 0.15.0: a file for code execution (container_upload) is refused without the code execution tool, which isn't
        # counted (below): every workshop request with files fell back to the rough count. Each counts as an allowance.
        uploads = 0
        messages = []
        for message in body.get("messages") or []:
            content = message.get("content") if isinstance(message, Mapping) else None
            if isinstance(content, list) and any(_is_upload(b) for b in content):
                uploads += sum(1 for b in content if _is_upload(b))
                kept = [b for b in content if not _is_upload(b)] or [{"type": "text", "text": "-"}]
                message = {**message, "content": kept}
            messages.append(message)
        if uploads:
            body = {**body, "messages": messages}
        tools = list(body.get("tools") or [])
        local = [t for t in tools if not str(t.get("type") or "").startswith(_SERVER_TOOL_PREFIXES)]
        server = len(tools) - len(local)
        if server:  # count_tokens refuses web search and web fetch
            if local:
                body["tools"] = local
            else:
                body.pop("tools", None)
            choice = body.get("tool_choice")
            if not local or (isinstance(choice, dict) and choice.get("name") not in {t.get("name") for t in local}):
                body.pop("tool_choice", None)
        try:
            counted = self._client.with_options(max_retries=2, timeout=30.0).messages.count_tokens(**body)
        except Exception as exc:  # noqa: BLE001 - a failed count must not stop the call; guess high instead
            log.warning("Token counting failed (%s); using a rough upper bound", type(exc).__name__)
            return rough_token_count(request) + server * SERVER_TOOL_ALLOWANCE
        return (
            math.ceil(counted.input_tokens * 1.05) + 200 + server * SERVER_TOOL_ALLOWANCE + uploads * UPLOAD_ALLOWANCE
        )

    # --- the Files API: the workshop's inputs and outputs (free, and not metered) ---

    def _files(self) -> Any:
        return self._client.with_options(max_retries=2, timeout=FILE_SECONDS).files

    def upload_file(self, name: str, data: bytes, mime: str) -> str:
        """Upload one input for a workshop run; it expires within the hour. Returns the file's id."""
        try:
            return str(self._files().upload(file=(name, data, mime), expires_in_seconds=UPLOAD_EXPIRY_SECONDS).id)
        except Exception as exc:  # noqa: BLE001 - reported to the workshop, which gives up on this run
            raise FilesError(f"uploading {name} failed ({type(exc).__name__})") from None

    def file_info(self, file_id: str) -> dict[str, Any]:
        try:
            meta = self._files().retrieve_metadata(file_id)
        except Exception as exc:  # noqa: BLE001
            raise FilesError(f"reading a file's details failed ({type(exc).__name__})") from None
        return {
            "filename": str(meta.filename),
            "size_bytes": int(meta.size_bytes),
            "mime_type": str(getattr(meta, "mime_type", "") or ""),
        }

    def download_file(self, file_id: str, limit: int) -> bytes:
        """A file the sandbox made, refused if it is larger than ``limit`` bytes."""
        try:
            with self._files().with_streaming_response.download(file_id) as response:
                data = bytearray()
                for chunk in response.iter_bytes():
                    data.extend(chunk)
                    if len(data) > limit:
                        raise FilesError(f"the file is larger than {limit // (1024 * 1024)} MB")
                return bytes(data)
        except FilesError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise FilesError(f"downloading a file failed ({type(exc).__name__})") from None

    def delete_file(self, file_id: str) -> None:
        try:
            self._files().delete(file_id)
        except Exception as exc:  # noqa: BLE001
            raise FilesError(f"deleting a file failed ({type(exc).__name__})") from None

    # --- one metered call: one POST /v1/messages, streamed ---

    def send(self, request: Mapping[str, Any]) -> Outcome:
        a = self._anthropic
        kwargs = {k: v for k, v in request.items() if k != "stream"}
        start: dict[str, Any] | None = None
        delta: dict[str, Any] | None = None
        request_id: str | None = None
        opened = stopped = False
        deadline = time.monotonic() + TOTAL_SECONDS
        try:
            with self._client.messages.stream(**kwargs) as stream:  # the POST happens here
                opened, request_id = True, stream.request_id
                for event in stream:
                    if event.type == "message_start":
                        start = _usage(getattr(event.message, "usage", None))
                    elif event.type == "message_delta":
                        delta = _usage(getattr(event, "usage", None))  # running totals
                    elif event.type == "message_stop":
                        stopped = True
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"the answer took longer than {TOTAL_SECONDS:.0f} s")
                    if self._stop.is_set():
                        raise _Stopped("the app is stopping")
                if not stopped:
                    raise EOFError("the answer ended without message_stop")
                message = stream.get_final_message().to_dict(mode="json")
                # The SDK's final message drops usage fields it doesn't know; take them from the events.
                message["usage"] = _merge(start, delta)
                return Completed(message, request_id)
        except a.APIStatusError as exc:
            if not opened:
                if exc.status_code in _REJECTED_STATUSES:
                    rejected = Rejected(exc.status_code, _describe(exc), exc.request_id)
                    self._check_blocking(rejected)
                    return rejected
                return Interrupted(_describe(exc), None, exc.request_id)
            # An SSE error event after the answer began (e.g. overloaded_error).
            return Interrupted(_describe(exc), _merge(start, delta) if start else None, request_id or exc.request_id)
        except a.APIConnectionError as exc:  # also APITimeoutError; only raised while opening
            cause = exc.__cause__
            if not opened and isinstance(cause, self._not_sent):
                return NotSent(f"{type(cause).__name__}: {_short(cause)}")
            return Interrupted(
                f"{type(exc).__name__} ({type(cause).__name__ if cause else 'no cause'})", None, request_id
            )
        except Exception as exc:  # noqa: BLE001 - an API failure never crashes the app
            if not opened and not isinstance(exc, self._httpx.HTTPError):
                return NotSent(f"{type(exc).__name__}: {_short(exc)}")  # the SDK refused the request itself
            # Read timeouts and dropped connections mid-stream arrive unwrapped from httpx2.
            return Interrupted(
                f"{type(exc).__name__}: {_short(exc)}", _merge(start, delta) if start else None, request_id
            )


def _is_upload(block: Any) -> bool:
    return isinstance(block, Mapping) and block.get("type") == "container_upload"


def _usage(usage: Any) -> dict[str, Any] | None:
    if usage is None:
        return None
    if hasattr(usage, "to_dict"):
        return usage.to_dict(mode="json")
    return dict(usage) if isinstance(usage, Mapping) else None


def _merge(start: dict[str, Any] | None, delta: dict[str, Any] | None) -> dict[str, Any]:
    usage = dict(start or {})
    usage.update({k: v for k, v in (delta or {}).items() if v is not None})
    return usage


def _short(exc: BaseException) -> str:
    return str(exc)[:300]


def _describe(exc: Any) -> str:
    """Error type, message, and the details the runner reacts to (spend limits, retry-after)."""
    body = exc.body if isinstance(getattr(exc, "body", None), dict) else {}
    error = body.get("error") if isinstance(body.get("error"), dict) else {}
    details = error.get("details") if isinstance(error.get("details"), dict) else {}
    parts = [str(getattr(exc, "type", None) or error.get("type") or exc.status_code)]
    parts.append(str(error.get("message") or getattr(exc, "message", "") or "")[:300])
    if details.get("error_code"):
        parts.append(f"error_code={details['error_code']}")
    response = getattr(exc, "response", None)
    retry_after = response.headers.get("retry-after") if response is not None else None
    if retry_after:
        parts.append(f"retry-after={retry_after}")
    return " | ".join(p for p in parts if p)

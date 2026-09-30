"""The live transport against a mocked HTTP layer: every failure maps to the right outcome, nothing is retried."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest

httpx2 = pytest.importorskip("httpx2")
pytest.importorskip("anthropic")

from app.economy.anthropic_transport import AnthropicTransport, _allowlist_transport  # noqa: E402
from app.economy.metering import Completed, FilesError, Interrupted, NotSent, Rejected  # noqa: E402

REQUEST = {"model": "claude-sonnet-5", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
START = {
    "type": "message_start",
    "message": {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "content": [],
        "model": "claude-sonnet-5",
        "stop_reason": None,
        "stop_sequence": None,
        "usage": {"input_tokens": 25, "output_tokens": 1, "cache_read_input_tokens": 0},
    },
}
BLOCK = [
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
    {"type": "content_block_stop", "index": 0},
]
DELTA = {
    "type": "message_delta",
    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
    "usage": {"output_tokens": 15, "output_tokens_details": {"thinking_tokens": 0}},
}
STOP = {"type": "message_stop"}


def sse(*events: dict[str, Any]) -> bytes:
    return b"".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n".encode() for e in events)


class Server:
    """Answers each POST with the next scripted response and records the requests."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item(request) if callable(item) else item


def stream(body: bytes | Iterator[bytes]) -> Any:
    return httpx2.Response(200, headers={"content-type": "text/event-stream", "request-id": "req_s"}, content=body)


def error(status: int, kind: str, message: str = "nope", **headers: str) -> Any:
    body = {"type": "error", "error": {"type": kind, "message": message}, "request_id": "req_e"}
    return httpx2.Response(status, json=body, headers={"request-id": "req_e", **headers})


def transport(server: Server, stop: threading.Event | None = None) -> AnthropicTransport:
    return AnthropicTransport("sk-ant-test", stop, http_transport=httpx2.MockTransport(server))


def test_a_complete_answer_keeps_every_usage_field() -> None:
    server = Server(stream(sse(START, *BLOCK, DELTA, STOP)))
    outcome = transport(server).send({**REQUEST, "stream": False})
    assert isinstance(outcome, Completed) and outcome.request_id == "req_s"
    assert outcome.response["content"][0]["text"] == "Hello"
    assert outcome.response["stop_reason"] == "end_turn"
    assert outcome.response["usage"]["input_tokens"] == 25 and outcome.response["usage"]["output_tokens"] == 15
    assert outcome.response["usage"]["output_tokens_details"] == {"thinking_tokens": 0}
    assert len(server.requests) == 1 and server.requests[0].url.host == "api.anthropic.com"
    sent = json.loads(server.requests[0].content)
    assert sent["stream"] is True and sent["model"] == "claude-sonnet-5"
    assert server.requests[0].headers["x-api-key"] == "sk-ant-test"


@pytest.mark.parametrize("status", [400, 401, 402, 403, 413, 429, 529])
def test_refusals_before_generation_are_rejected_and_never_retried(status: int) -> None:
    server = Server(error(status, "rate_limit_error", "slow down", **{"retry-after": "7"}))
    outcome = transport(server).send(REQUEST)
    assert isinstance(outcome, Rejected) and outcome.status == status
    assert "retry-after=7" in outcome.error and len(server.requests) == 1


def test_a_spend_limit_is_described() -> None:
    body = {
        "type": "error",
        "error": {
            "type": "rate_limit_error",
            "message": "limit",
            "details": {"error_code": "enforced_spend_limit_reached"},
        },
    }
    outcome = transport(Server(httpx2.Response(429, json=body))).send(REQUEST)
    assert isinstance(outcome, Rejected) and "enforced_spend_limit_reached" in outcome.error


@pytest.mark.parametrize("status", [500, 503, 504])
def test_server_errors_count_as_unknown_cost(status: int) -> None:
    outcome = transport(Server(error(status, "api_error"))).send(REQUEST)
    assert isinstance(outcome, Interrupted) and outcome.partial_usage is None


def test_no_connection_is_not_sent() -> None:
    outcome = transport(Server(httpx2.ConnectError("no route"))).send(REQUEST)
    assert isinstance(outcome, NotSent) and "ConnectError" in outcome.error


def test_a_read_timeout_before_the_answer_is_interrupted() -> None:
    outcome = transport(Server(httpx2.ReadTimeout("slow"))).send(REQUEST)
    assert isinstance(outcome, Interrupted)


def test_an_error_event_mid_stream_keeps_the_usage_so_far() -> None:
    overloaded = {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}
    outcome = transport(Server(stream(sse(START, *BLOCK[:2], overloaded)))).send(REQUEST)
    assert isinstance(outcome, Interrupted) and outcome.partial_usage == START["message"]["usage"]
    assert "overloaded" in outcome.error


def test_a_cut_connection_is_interrupted() -> None:
    def body() -> Iterator[bytes]:
        yield sse(START, BLOCK[0])
        raise httpx2.RemoteProtocolError("peer closed connection")

    outcome = transport(Server(stream(body()))).send(REQUEST)
    assert isinstance(outcome, Interrupted) and outcome.partial_usage is not None


def test_an_answer_without_message_stop_is_interrupted() -> None:
    outcome = transport(Server(stream(sse(START, *BLOCK, DELTA)))).send(REQUEST)
    assert isinstance(outcome, Interrupted) and "message_stop" in outcome.error


def test_stopping_the_app_interrupts_the_stream() -> None:
    stop = threading.Event()
    stop.set()
    outcome = transport(Server(stream(sse(START, *BLOCK, DELTA, STOP))), stop).send(REQUEST)
    assert isinstance(outcome, Interrupted) and "stopping" in outcome.error


def test_an_invalid_request_is_not_sent() -> None:
    server = Server()
    outcome = transport(server).send({"model": "claude-sonnet-5", "messages": []})  # no max_tokens
    assert isinstance(outcome, NotSent) and server.requests == []


def test_counting_drops_server_tools_and_falls_back_when_it_fails() -> None:
    tools = [
        {"name": "note", "description": "d", "input_schema": {"type": "object"}},
        {"type": "web_search_20250305", "name": "web_search", "max_uses": 2},
    ]
    server = Server(httpx2.Response(200, json={"input_tokens": 1000}))
    counted = transport(server).count_tokens({**REQUEST, "tools": tools, "tool_choice": {"type": "auto"}})
    sent = json.loads(server.requests[0].content)
    assert [t["name"] for t in sent["tools"]] == ["note"] and "max_tokens" not in sent
    assert counted == 1050 + 200 + 3000  # 3,000 tokens for the server tool (0.12.0: 1,000 was too few)
    failing = Server(error(429, "rate_limit_error"), error(429, "rate_limit_error"), error(429, "rate_limit_error"))
    assert transport(failing).count_tokens(REQUEST) > 600  # a rough upper bound instead of an error


def test_only_the_anthropic_api_can_be_reached() -> None:
    allow = _allowlist_transport(httpx2)
    for url in (
        "https://example.com/v1/messages",
        "http://api.anthropic.com/v1/messages",
        "https://api.anthropic.com:8443/",
    ):
        with pytest.raises(httpx2.ConnectError, match="only talks to"):
            allow.handle_request(httpx2.Request("POST", url))


def test_only_the_transport_imports_the_sdk() -> None:
    import ast
    from pathlib import Path

    app_dir = Path(__file__).resolve().parent.parent / "app"
    for source in app_dir.rglob("*.py"):
        if source.name == "anthropic_transport.py":
            continue
        # Etsy's client (0.8.0) is Ember's own code, allowlisted to api.etsy.com: it may use httpx2, never the SDK.
        # So are Pinterest's and Printify's (0.13.0), allowlisted to api.pinterest.com and api.printify.com.
        clients = ("etsy_live.py", "pinterest_live.py", "printify_live.py")
        forbidden = {"anthropic"} if source.name in clients else {"anthropic", "httpx2"}
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            names = [a.name for a in node.names] if isinstance(node, ast.Import) else []
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                names = [node.module or ""]
            assert not any(n.split(".")[0] in forbidden for n in names), source


def test_live_mode_uses_the_api_only_with_a_key(data_dir: Any) -> None:
    from app.agent.service import Agent
    from app.config import LoadedSettings, Settings
    from app.economy.anthropic_transport import AnthropicTransport as Real
    from tests.economy_helpers import make_economy

    no_key = Settings(dry_run=False)
    economy = make_economy(data_dir, no_key)
    agent = Agent(economy.db, LoadedSettings(no_key), economy, cycles_enabled=True)
    assert "API key" in (agent.blocked_reason() or "")
    economy.stop()

    keyed = Settings(dry_run=False, anthropic_api_key="sk-ant-test")
    economy = make_economy(data_dir, keyed)
    agent = Agent(economy.db, LoadedSettings(keyed), economy, cycles_enabled=True)
    assert isinstance(agent.transport, Real)
    agent.transport.blocked = "The Anthropic API refused the API key"
    assert agent.blocked_reason() == "The Anthropic API refused the API key"


def test_a_refused_key_blocks_further_calls() -> None:
    server = Server(error(401, "authentication_error", "invalid x-api-key"))
    live = transport(server)
    assert isinstance(live.send(REQUEST), Rejected)
    assert live.blocked is not None and "refused the API key" in live.blocked
    assert "sk-ant-test" not in live.blocked
    limit = transport(Server(error(400, "invalid_request_error", "You have reached your specified API usage limits")))
    limit.send(REQUEST)
    assert limit.blocked is not None and "spend limit" in limit.blocked
    busy = transport(Server(error(529, "overloaded_error")))
    busy.send(REQUEST)
    assert busy.blocked is None


# --- the Files API: the workshop's inputs and outputs ---

FILE = {
    "id": "file_1",
    "type": "file",
    "filename": "chart.png",
    "mime_type": "image/png",
    "size_bytes": 9,
    "created_at": "2026-09-28T10:00:00Z",
    "downloadable": True,
}


def test_an_upload_expires_within_the_hour() -> None:
    server = Server(httpx2.Response(200, json={**FILE, "filename": "prices.csv", "downloadable": False}))
    assert transport(server).upload_file("prices.csv", b"a,b\n", "text/csv") == "file_1"
    sent = server.requests[0]
    assert sent.method == "POST" and sent.url.path == "/v1/files" and sent.url.host == "api.anthropic.com"
    body = sent.read()
    assert b'name="expires_in_seconds"\r\n\r\n3600' in body and b'filename="prices.csv"' in body and b"a,b\n" in body


def test_a_made_file_is_read_and_downloaded_within_its_limit() -> None:
    server = Server(
        httpx2.Response(200, json=FILE),
        httpx2.Response(200, content=b"PNG bytes", headers={"content-type": "application/octet-stream"}),
        httpx2.Response(200, content=b"PNG bytes", headers={"content-type": "application/octet-stream"}),
        httpx2.Response(200, json={"id": "file_1", "type": "file_deleted"}),
    )
    files = transport(server)
    assert files.file_info("file_1") == {"filename": "chart.png", "size_bytes": 9, "mime_type": "image/png"}
    assert files.download_file("file_1", 100) == b"PNG bytes"
    with pytest.raises(FilesError, match="larger than"):
        files.download_file("file_1", 5)
    files.delete_file("file_1")
    assert [(r.method, r.url.path) for r in server.requests] == [
        ("GET", "/v1/files/file_1"),
        ("GET", "/v1/files/file_1/content"),
        ("GET", "/v1/files/file_1/content"),
        ("DELETE", "/v1/files/file_1"),
    ]


def test_a_files_api_error_is_a_files_error() -> None:
    server = Server(error(404, "not_found_error"), error(404, "not_found_error"), error(404, "not_found_error"))
    files = transport(server)
    for call in (lambda: files.file_info("file_x"), lambda: files.delete_file("file_x")):
        with pytest.raises(FilesError, match="NotFoundError"):
            call()
    with pytest.raises(FilesError, match="uploading notes.md failed"):
        files.upload_file("notes.md", b"x", "text/markdown")

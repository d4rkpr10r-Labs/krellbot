"""A failed market-data fetch carries a closed failure code, never its text.

The tick derives the code from the exception class (walking the chained
exceptions) and the HTTP status only. The journal keeps the code beside the
exception type name; `/api/v1/paper/health` serves the code and nothing else
about the failure. Messages, URLs and reason phrases never decide the code and
never reach the wire.
"""

from __future__ import annotations

import json
import socket
import ssl
import urllib.error
from decimal import Decimal
from email.message import Message

import pytest

from krellbot import cli, config, journal
from tests.test_multi_venue_paper_status import (
    TEST_PORT,
    _arm,
    _authenticated_headers,
    _bootstrap_session,
    _get,
    _write_pack,
)
from tests.test_ns06_api import _build_app, asgi_call

SECRET = "sk-live-7f3a9 at https://api.private.example.net/0/private?token=7f3a9"
CODES = ("timeout", "dns_or_network", "tls", "region_or_permission", "rate_limited", "venue_error")
ARMED_AT = 100_000


class PublicTransport:
    def get(self, url, headers=None):
        return {"error": [], "result": {}}

    def post(self, *args, **kwargs):
        raise AssertionError("paper ticks must not call private endpoints")


def _http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(f"https://api.private.example.net/?{SECRET}", status, SECRET, Message(), None)


def _raise(exc: BaseException):
    def raiser():
        raise exc

    return raiser


def _raise_from(outer: BaseException, inner: BaseException):
    def raiser():
        raise outer from inner

    return raiser


def _raise_parse_error_while_handling_timeout():
    # Implicit chaining: the ValueError's __context__ is the TimeoutError.
    try:
        raise TimeoutError(SECRET)
    except TimeoutError:
        raise ValueError(SECRET)


CLASSIFIED = [
    pytest.param(_raise(TimeoutError(SECRET)), "timeout", id="timeout"),
    pytest.param(_raise(urllib.error.URLError(TimeoutError(SECRET))), "timeout", id="urlerror-timeout"),
    pytest.param(_raise(socket.gaierror(-2, SECRET)), "dns_or_network", id="dns"),
    pytest.param(_raise(urllib.error.URLError(socket.gaierror(-2, SECRET))), "dns_or_network", id="urlerror-dns"),
    pytest.param(_raise(ConnectionRefusedError(SECRET)), "dns_or_network", id="refused"),
    pytest.param(_raise(ConnectionResetError(SECRET)), "dns_or_network", id="reset"),
    pytest.param(_raise(urllib.error.URLError(SECRET)), "dns_or_network", id="urlerror-text-reason"),
    pytest.param(_raise(ssl.SSLCertVerificationError(SECRET)), "tls", id="cert"),
    pytest.param(_raise(ssl.SSLError(SECRET)), "tls", id="ssl"),
    pytest.param(_raise(urllib.error.URLError(ssl.SSLCertVerificationError(SECRET))), "tls", id="urlerror-cert"),
    pytest.param(_raise(_http_error(403)), "region_or_permission", id="http-403"),
    pytest.param(_raise(_http_error(451)), "region_or_permission", id="http-451"),
    pytest.param(_raise(_http_error(429)), "rate_limited", id="http-429"),
    pytest.param(_raise(_http_error(500)), "venue_error", id="http-500"),
    pytest.param(_raise(_http_error(503)), "venue_error", id="http-503"),
    pytest.param(_raise(_http_error(404)), "venue_error", id="http-404"),
    pytest.param(_raise(ValueError(SECRET)), "venue_error", id="parse-value-error"),
    pytest.param(_raise(TypeError(SECRET)), "venue_error", id="parse-type-error"),
    pytest.param(_raise(RuntimeError(SECRET)), "venue_error", id="runtime-error"),
    pytest.param(_raise_from(RuntimeError(SECRET), _http_error(429)), "rate_limited", id="chained-cause-429"),
    pytest.param(_raise_from(ValueError(SECRET), _http_error(451)), "region_or_permission", id="chained-cause-451"),
    pytest.param(_raise_parse_error_while_handling_timeout, "timeout", id="implicit-context-timeout"),
    pytest.param(
        _raise_from(RuntimeError(SECRET), urllib.error.URLError(ssl.SSLError(SECRET))), "tls", id="chained-tls"
    ),
]


@pytest.fixture
def armed(home, fresh_keyring, monkeypatch):
    """One paper arm on kraken SUIUSD, with tick records dated after the arm."""
    path = _write_pack(home, pack_id="wave", venue="kraken", pair="SUIUSD")
    _arm(home, path, venue="kraken", paper_balance=Decimal(1000))
    saved = config.load_config(home)
    saved.armed[0].armed_at_ts = ARMED_AT
    config.save_config(home, saved)
    monkeypatch.setattr(cli, "_now_seconds_tick", lambda: float(ARMED_AT + 1))
    return home


def _tick(raiser, *, fail_pair: str | None = None) -> int:
    def fetch(venue, pair, timeframe, transport, include_forming=False):
        if fail_pair is None or pair == fail_pair:
            raiser()
        return []

    return cli.cmd_tick(["--venue", "kraken"], fetch=fetch, transport=PublicTransport())


def _feed_records(home) -> dict[str, dict]:
    return {
        record["pack"]: record["detail"]
        for record in journal.read_records_lenient(home)
        if record.get("kind") == "tick" and record["detail"].get("reason") in {"fetch_error", "check_incomplete"}
    }


def _journal_text(home) -> str:
    return "".join(path.read_text(encoding="utf-8") for path in sorted((home / "journal").glob("*.jsonl")))


def _session(home):
    app = _build_app(home)
    headers = _authenticated_headers(_bootstrap_session(app, "boot-test-token"))
    return app, headers


def _health(home) -> tuple[dict, str]:
    app, headers = _session(home)
    base = [("Host", f"127.0.0.1:{TEST_PORT}"), *headers]
    status, _flat, body, _cookies = asgi_call(app, method="GET", path="/api/v1/paper/health", headers=base)
    assert status == 200
    wire = body.decode("utf-8")
    return json.loads(wire), wire


def _write_tick(home, detail: dict) -> None:
    directory = home / "journal"
    directory.mkdir(exist_ok=True)
    record = {
        "kind": "tick",
        "ts": ARMED_AT + 1,
        "venue": "kraken",
        "pack": "wave",
        "bar_ts": 0,
        "detail": {"pair": "SUIUSD", "mode": "paper", **detail},
    }
    with (directory / "hand-written.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


@pytest.mark.parametrize(("raiser", "expected"), CLASSIFIED)
def test_failure_code_comes_from_exception_class_and_http_status(armed, raiser, expected):
    assert _tick(raiser) == 1
    detail = _feed_records(armed)["wave"]
    assert detail["reason"] == "fetch_error"
    assert detail["failure_code"] == expected
    assert detail["failure_code"] in CODES
    text = _journal_text(armed)
    assert "sk-live-7f3a9" not in text
    assert "private.example.net" not in text


@pytest.mark.parametrize(
    "exc",
    [
        ValueError("HTTP Error 429: Too Many Requests"),
        RuntimeError("HTTP Error 403: Forbidden; 451 Unavailable For Legal Reasons"),
        ValueError("The read operation timed out"),
        RuntimeError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"),
        ValueError("[Errno -2] Name or service not known; Connection refused"),
        OSError("Network is unreachable"),
    ],
    ids=["says-429", "says-403-451", "says-timeout", "says-tls", "says-dns", "plain-oserror"],
)
def test_messages_never_decide_the_failure_code(armed, exc):
    assert _tick(_raise(exc)) == 1
    assert _feed_records(armed)["wave"]["failure_code"] == "venue_error"


def test_health_serves_the_code_and_no_exception_text(armed):
    assert _tick(_raise_from(RuntimeError(SECRET), _http_error(429))) == 1
    body, wire = _health(armed)
    assert body["state"] == "feed_error"
    assert body["failure_code"] == "rate_limited"
    for leaked in ("sk-live-7f3a9", "private.example.net", "token=", "RuntimeError", "HTTPError", "Traceback"):
        assert leaked not in wire


@pytest.mark.parametrize("code", CODES)
def test_health_serves_each_closed_code(armed, code):
    _write_tick(armed, {"reason": "fetch_error", "failure_code": code})
    body, _wire = _health(armed)
    assert body["state"] == "feed_error"
    assert body["failure_code"] == code


@pytest.mark.parametrize(
    "stored",
    [
        SECRET,
        "TIMEOUT",
        "<script>alert(1)</script>",
        {"code": "timeout"},
        ["timeout"],
        429,
        True,
        None,
    ],
    ids=["message", "wrong-case", "markup", "object", "list", "number", "bool", "null"],
)
def test_health_drops_a_stored_code_outside_the_closed_set(armed, stored):
    _write_tick(armed, {"reason": "fetch_error", "failure_code": stored, "exception": SECRET})
    body, wire = _health(armed)
    assert body["state"] == "feed_error"
    assert body["failure_code"] is None
    assert "sk-live-7f3a9" not in wire
    assert "script" not in wire


def test_legacy_fetch_error_without_a_code_serves_null(armed):
    _write_tick(armed, {"reason": "fetch_error", "exception": "ConnectionError"})
    body, wire = _health(armed)
    assert body["state"] == "feed_error"
    assert body["failure_code"] is None
    assert "ConnectionError" not in wire


@pytest.mark.parametrize("reason", ["flat", "check_incomplete", "no_candles"])
def test_failure_code_is_served_only_for_a_feed_error(armed, reason):
    _write_tick(armed, {"reason": reason, "failure_code": "timeout"})
    body, _wire = _health(armed)
    assert body["state"] != "feed_error"
    assert body["failure_code"] is None


def test_other_markets_in_an_aborted_tick_do_not_claim_the_failing_code(armed):
    second = _write_pack(armed, pack_id="second", venue="kraken", pair="BTCUSD")
    third = _write_pack(armed, pack_id="third", venue="kraken", pair="ETHUSD")
    _arm(armed, second, venue="kraken", paper_balance=Decimal(1000))
    _arm(armed, third, venue="kraken", paper_balance=Decimal(1000))
    assert _tick(_raise(_http_error(429)), fail_pair="BTCUSD") == 1
    records = _feed_records(armed)
    assert records["second"]["reason"] == "fetch_error"
    assert records["second"]["failure_code"] == "rate_limited"
    assert records["third"]["reason"] == "check_incomplete"
    assert "failure_code" not in records["third"]


def test_failure_code_stays_off_the_diagnostic_report(armed):
    assert _tick(_raise(_http_error(451))) == 1
    app, headers = _session(armed)
    status, report = _get(app, "/api/v1/diagnostics/report", headers=headers)
    assert status == 200
    assert set(report) == {"schema_version", "build", "os", "scheduler", "events", "reproduction_steps"}
    assert report["events"]
    for event in report["events"]:
        assert set(event) == {"ts", "venue", "pair", "source", "classification", "refusal_code"}
    assert "failure_code" not in json.dumps(report)
    assert "region_or_permission" not in json.dumps(report)


def test_health_with_a_failure_code_still_requires_a_session(armed):
    assert _tick(_raise(_http_error(429))) == 1
    app = _build_app(armed)
    status, _flat, body, _cookies = asgi_call(
        app, method="GET", path="/api/v1/paper/health", headers=[("Host", f"127.0.0.1:{TEST_PORT}")]
    )
    assert status in (401, 403)
    assert b"rate_limited" not in body

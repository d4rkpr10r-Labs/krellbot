"""TradingView previews preserve the loopback and paper execution boundaries."""

from __future__ import annotations

import json

import pytest

from krellbot.application.tradingview import MAX_ALERT_BYTES
from tests.test_ns06_api import TEST_PORT, _build_app, _post_json, asgi_call

PATH = "/api/v1/paper/tradingview/preview"
ORIGIN = f"http://127.0.0.1:{TEST_PORT}"
ALERT = {
    "schema_version": "1",
    "rule_id": "btc-entry",
    "ticker": "KRAKEN:BTCUSD",
    "signal": "entry",
    "occurred_at": "2026-11-22T18:00:01Z",
}


@pytest.fixture
def local_session(tmp_path):
    app = _build_app(tmp_path)
    status, _, body, cookies = _post_json(
        app, "/api/v1/session/bootstrap", {"token": "boot-test-token"}, headers=[("Origin", ORIGIN)]
    )
    assert status == 200
    cookie = next(value for value in cookies if value.startswith("krellbot_session=")).split(";", 1)[0]
    return app, [("Origin", ORIGIN), ("Cookie", cookie), ("X-Krellbot-CSRF", json.loads(body)["csrf_token"])]


def test_preview_is_paper_only_and_does_not_write(local_session, tmp_path):
    app, headers = local_session
    before = sorted(tmp_path.rglob("*"))
    status, response_headers, body, _ = _post_json(app, PATH, ALERT, headers=headers)
    assert status == 200
    assert dict(response_headers)["cache-control"] == "no-store"
    result = json.loads(body)
    assert result["code"] == "alert_previewed"
    alert = result["alert"]
    fingerprint = alert.pop("fingerprint")
    assert len(fingerprint) == 64
    assert alert == {**ALERT, "source": "tradingview", "mode": "paper"}
    _, _, reordered, _ = _post_json(app, PATH, dict(reversed(list(ALERT.items()))), headers=headers)
    assert json.loads(reordered)["alert"]["fingerprint"] == fingerprint
    assert sorted(tmp_path.rglob("*")) == before


@pytest.mark.parametrize(
    "fields",
    [
        {"mode": "live"},
        {"qty": "1"},
        {"ticker": "BTCUSD"},
        {"signal": "market_order"},
        {"occurred_at": "2026-02-30T12:00:00Z"},
        {"rule_id": "btc entry"},
    ],
)
def test_invalid_alert_fields_fail_closed(local_session, fields):
    app, headers = local_session
    status, _, body, _ = _post_json(app, PATH, {**ALERT, **fields}, headers=headers)
    assert status == 400
    assert json.loads(body)["code"] == "invalid_alert"


def test_preview_requires_session_origin_and_csrf(local_session):
    app, headers = local_session
    for omitted in ("Cookie", "Origin", "X-Krellbot-CSRF"):
        status, _, _, _ = _post_json(app, PATH, ALERT, headers=[header for header in headers if header[0] != omitted])
        assert status == 403


def test_preview_limits_streamed_body(local_session):
    app, headers = local_session
    status, _, body, _ = asgi_call(
        app,
        method="POST",
        path=PATH,
        body=b"x" * (MAX_ALERT_BYTES + 1),
        headers=[("Host", f"127.0.0.1:{TEST_PORT}"), *headers],
    )
    assert status == 413
    assert json.loads(body)["code"] == "alert_too_large"

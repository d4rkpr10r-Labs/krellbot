"""The scheduled tick delivers opted-in alert notifications itself.

``maybe_notify`` ran only from ``GET /api/v1/operations``, so an alert
reached the operator only while a workstation tab polled. The OS scheduler
runs ``<executable> tick`` (``service.runner.main(["tick"])``); after the
tick, the tick entry collects alerts and calls ``maybe_notify``.

Pins:

* A scheduled tick with no workstation running notifies, on each enabled
  channel, the alert the tick itself just raised (a stale candle read).
* The tick and the workstation share M28.1's occurrence store: whichever
  sees an occurrence first sends it, and neither sends it again.
* The store is locked for the whole read-send-record pass. A pass that
  finds it locked sends nothing and writes nothing, so a workstation poll
  that lands while the tick is sending adds no second notification.
* Settings stay opt-in: with every channel off the tick collects no alerts,
  calls no notifier and writes no notification file.
* A notifier that raises never fails the tick or changes its exit code. It
  is tried at most once per channel per tick, and only after the tick has
  journaled its own outcome. A failing alert read does not fail it either.

Every test uses an isolated home, a fake fetch and transport, and a fake
notifier: no venue, desktop bus or SMTP host is touched.
"""

from __future__ import annotations

import asyncio
import json
import time
from decimal import Decimal
from pathlib import Path

import pytest

from krellbot import notifications as kb_notifications
from krellbot.application import alerts
from krellbot.pack.model import Candle

TEST_PORT = 8080
BOOT_TOKEN = "boot-scheduled-tick-alerts"
HOUR_MS = 3_600_000
FEED_STALE_TITLE = "Krellbot warning: feed_stale"
LIVE_REFUSED_TITLE = "Krellbot warning: live_refused"


# ---- fixtures on disk -------------------------------------------------------


def _arm_paper(home: Path) -> None:
    from krellbot.config import ArmedPack, Config, save_config

    body = {
        "schema_version": 1,
        "id": "tick-alerts",
        "version": "1.0.0",
        "label": "Tick alerts",
        "author": "krellbot scheduled tick alert tests",
        "timeframe": "1h",
        "indicators": {"sma2": {"fn": "sma", "src": "close", "len": 2}},
        "entry": ["close", "crosses_above", "sma2"],
        "exit": ["close", "crosses_below", "sma2"],
        "risk": {"max_account_pct": 100, "stop": {"type": "pct", "pct": 50}},
        "markets": [{"venue": "kraken", "pair": "SUIUSD"}],
    }
    pack_path = home / "tick-alerts.json"
    pack_path.write_text(json.dumps(body), encoding="utf-8")
    save_config(
        home,
        Config(
            armed=[
                ArmedPack(
                    pack_path=str(pack_path),
                    pack_sha256="0" * 64,
                    pack_id="tick-alerts",
                    pack_version="1.0.0",
                    venue="kraken",
                    pair="SUIUSD",
                    cap=Decimal(100),
                    stop=Decimal(5),
                    mode="paper",
                    starting_cash=Decimal(1000),
                    requires_license=False,
                    armed_at_ts=1,
                )
            ]
        ),
    )


def _stale_candles(bars: int = 3) -> list[Candle]:
    """Closed 1h candles from 1970: every tick that reads them reads a stale feed."""

    return [
        Candle(i * HOUR_MS, Decimal(c), Decimal(c) + 1, Decimal(c) - 1, Decimal(c), Decimal(100))
        for i, c in enumerate([10, 9, 12, 11][:bars])
    ]


def _now() -> int:
    return int(time.time())


def _append_refusal(home: Path) -> None:
    """A second open alert, on a venue the tick does not touch."""

    journal = home / "journal"
    journal.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": 1_700_000_000,
        "kind": "tick",
        "venue": "coinbase",
        "pack": "other-pack",
        "bar_ts": 0,
        "detail": {"reason": "live_refused", "code": "live_disabled", "pair": "BTCUSD"},
    }
    with (journal / "2023-11.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


def _tick_records(home: Path) -> int:
    count = 0
    for path in sorted((home / "journal").glob("*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip() and json.loads(line).get("kind") == "tick":
                count += 1
    return count


def _sent_path(home: Path) -> Path:
    return home / "run" / kb_notifications.SENT_FILE


def _titles(rows: list[dict[str, str]]) -> list[tuple[str, str]]:
    return [(row["channel"], row["title"]) for row in rows]


class _SilentTransport:
    """Records every call; the paper tick never needs a private endpoint."""

    def __init__(self) -> None:
        self.posts: list[str] = []

    def get(self, url, headers=None):
        return {"error": [], "result": {}}

    def post(self, url, form, headers):
        self.posts.append(url)
        return {"error": [], "result": {}}


class _RaisingNotifier:
    """Raises on every call and records what the journal held at that moment."""

    def __init__(self, home: Path) -> None:
        self._home = home
        self.attempts: list[dict[str, object]] = []

    def notify(self, *, channel, title, body, alert_id) -> None:
        self.attempts.append({"channel": channel, "title": title, "tick_records": _tick_records(self._home)})
        raise RuntimeError("CANARY notifier down")


@pytest.fixture
def notifier(monkeypatch) -> kb_notifications.FakeNotifier:
    """The notifier production would build; never a desktop bus or SMTP host."""

    fake = kb_notifications.FakeNotifier()
    monkeypatch.setattr(kb_notifications, "default_notifier", lambda home: fake)
    return fake


def _scheduled_tick(monkeypatch, *, fetch=None) -> int:
    """Run the tick exactly as the OS scheduler does: ``<executable> tick``."""

    from krellbot.service import runner

    def stale_fetch(venue, pair, tf, transport):
        return _stale_candles()

    monkeypatch.setattr("krellbot.cli._default_fetch", fetch or stale_fetch)
    monkeypatch.setattr("krellbot.cli._default_transport", _SilentTransport)
    return runner.main(["tick"])


# ---- the workstation, through its real route --------------------------------


def _workstation(home: Path, fake: kb_notifications.FakeNotifier):
    from krellbot.api.app import create_app

    app = create_app(home=home, port=TEST_PORT, bootstrap_token=BOOT_TOKEN)
    app.state.krellbot.alert_notifier = fake
    status, _body, cookies = _asgi(
        app,
        method="POST",
        path="/api/v1/session/bootstrap",
        body=json.dumps({"token": BOOT_TOKEN}).encode("utf-8"),
        headers=[("Origin", f"http://127.0.0.1:{TEST_PORT}"), ("Content-Type", "application/json")],
    )
    assert status == 200, _body
    cookie = next(c for c in cookies if c.startswith("krellbot_session="))
    return app, [("Cookie", cookie.split(";", 1)[0])]


def _poll_operations(app, auth) -> list[dict]:
    status, body, _cookies = _asgi(app, method="GET", path="/api/v1/operations", headers=auth)
    assert status == 200, body
    return json.loads(body)["alerts"]


def _asgi(app, *, method: str, path: str, body: bytes = b"", headers=None):
    headers = [("Host", f"127.0.0.1:{TEST_PORT}"), *(headers or [])]
    if body:
        headers.append(("Content-Length", str(len(body))))
    sent = {"done": False}
    captured: dict[str, object] = {"status": 500, "headers": [], "body": bytearray()}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            captured["status"] = int(message["status"])
            captured["headers"] = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            captured["body"] = captured["body"] + message.get("body", b"")

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("latin-1"),
        "query_string": b"",
        "server": ("127.0.0.1", 0),
        "client": ("127.0.0.1", 12345),
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1")) for k, v in headers],
        "root_path": "",
    }
    asyncio.run(app(scope, receive, send))
    cookies = [v.decode("latin-1") for k, v in captured["headers"] if k.decode("latin-1").lower() == "set-cookie"]
    return int(captured["status"]), bytes(captured["body"]), cookies


# ---- delivery from the scheduled tick ----------------------------------------


def test_scheduled_tick_notifies_the_alert_it_raised_with_no_workstation_open(
    home: Path, fresh_keyring, monkeypatch, notifier, capsys
) -> None:
    _arm_paper(home)
    kb_notifications.update_settings(home, {"desktop_enabled": True})

    rc = _scheduled_tick(monkeypatch)

    assert rc == 0, capsys.readouterr()
    assert "kraken SUIUSD tick-alerts (paper):" in capsys.readouterr().out
    stale = [a for a in alerts.collect(home, now=_now()) if a.kind == alerts.KIND_FEED_STALE]
    assert len(stale) == 1, stale
    assert notifier.sent == [
        {
            "channel": "desktop",
            "title": FEED_STALE_TITLE,
            "body": "count=1 code=candle_stale venue=kraken pair=SUIUSD ack=in-app",
            "alert_id": stale[0].id,
        }
    ], notifier.sent

    desktop = kb_notifications.delivery_status(home)["channels"]["desktop"]
    assert desktop["last_sent_ts"] is not None, desktop
    assert desktop["pending"] == 0, desktop
    assert desktop["last_failure"] is None, desktop


def test_each_occurrence_is_sent_once_whether_the_tick_or_the_workstation_sees_it_first(
    home: Path, fresh_keyring, monkeypatch, notifier
) -> None:
    _arm_paper(home)
    kb_notifications.update_settings(home, {"desktop_enabled": True})

    # The tick sees the stale feed first; a later tick sends it no more.
    assert _scheduled_tick(monkeypatch) == 0
    assert _scheduled_tick(monkeypatch) == 0
    assert _titles(notifier.sent) == [("desktop", FEED_STALE_TITLE)], notifier.sent

    # The workstation opens later: the tick already sent the stale feed, so it
    # sends only the refusal that appeared since.
    _append_refusal(home)
    workstation = kb_notifications.FakeNotifier()
    app, auth = _workstation(home, workstation)
    kinds = {row["kind"] for row in _poll_operations(app, auth)}
    assert kinds == {alerts.KIND_FEED_STALE, "live_refused"}, kinds
    _poll_operations(app, auth)
    assert _titles(workstation.sent) == [("desktop", LIVE_REFUSED_TITLE)], workstation.sent

    # The next tick resends neither.
    assert _scheduled_tick(monkeypatch) == 0
    assert _titles(notifier.sent) == [("desktop", FEED_STALE_TITLE)], notifier.sent
    assert _titles(workstation.sent) == [("desktop", LIVE_REFUSED_TITLE)], workstation.sent


def test_a_workstation_poll_during_the_tick_send_sends_nothing_and_writes_nothing(
    home: Path, fresh_keyring, monkeypatch
) -> None:
    _arm_paper(home)
    kb_notifications.update_settings(home, {"desktop_enabled": True})
    workstation = kb_notifications.FakeNotifier()
    app, auth = _workstation(home, workstation)
    during: dict[str, object] = {}

    class _TickNotifier(kb_notifications.FakeNotifier):
        """The tick's notifier: the workstation polls while it is sending."""

        def notify(self, **kwargs) -> None:
            during["alerts"] = [row["kind"] for row in _poll_operations(app, auth)]
            during["store_written"] = _sent_path(home).exists()
            super().notify(**kwargs)

    tick_notifier = _TickNotifier()
    monkeypatch.setattr(kb_notifications, "default_notifier", lambda home: tick_notifier)

    assert _scheduled_tick(monkeypatch) == 0

    assert _titles(tick_notifier.sent) == [("desktop", FEED_STALE_TITLE)], tick_notifier.sent
    # The workstation saw the alert mid-send but neither sent it nor wrote the
    # store the tick was about to record its delivery in.
    assert during == {"alerts": [alerts.KIND_FEED_STALE], "store_written": False}, during
    assert workstation.sent == [] and workstation.attempts == [], workstation.attempts

    # Once the tick has recorded it, the workstation does not send it either.
    _poll_operations(app, auth)
    assert workstation.attempts == [], workstation.attempts
    assert kb_notifications.delivery_status(home)["channels"]["desktop"]["pending"] == 0


# ---- opt-in ------------------------------------------------------------------


def test_with_every_channel_off_the_tick_collects_and_notifies_nothing(
    home: Path, fresh_keyring, monkeypatch, notifier
) -> None:
    _arm_paper(home)
    reads: list[int] = []
    real_collect = alerts.collect

    def counting_collect(home_arg, *, now):
        reads.append(now)
        return real_collect(home_arg, now=now)

    monkeypatch.setattr(alerts, "collect", counting_collect)

    assert _scheduled_tick(monkeypatch) == 0

    assert reads == [], reads
    assert notifier.attempts == [], notifier.attempts
    assert not _sent_path(home).exists()
    assert not (home / kb_notifications.CONFIG_FILE).exists()
    run_files = sorted(p.name for p in (home / "run").iterdir()) if (home / "run").exists() else []
    assert [name for name in run_files if name.startswith("notifications")] == [], run_files
    # The alert exists; only the operator's opt-in is missing.
    assert [a.kind for a in real_collect(home, now=_now())] == [alerts.KIND_FEED_STALE]


# ---- failures never reach the tick --------------------------------------------


def _fetch_next_bar(venue, pair, tf, transport):
    """The next scheduled tick reads one more (still stale) closed bar."""

    return _stale_candles(bars=4)


def _fetch_down(venue, pair, tf, transport):
    raise ConnectionError("feed down")


@pytest.mark.parametrize(
    ("fetch", "expected_rc", "expected_line"),
    [
        (_fetch_next_bar, 0, "kraken SUIUSD tick-alerts (paper):"),
        (_fetch_down, 1, "ConnectionError"),
    ],
    ids=["clean_tick", "failed_fetch"],
)
def test_a_raising_notifier_never_fails_the_tick_or_changes_its_exit_code(
    home: Path, fresh_keyring, monkeypatch, capsys, fetch, expected_rc, expected_line
) -> None:
    _arm_paper(home)
    _append_refusal(home)
    # A first tick, with notifications still off, leaves the stale feed open.
    monkeypatch.setattr(kb_notifications, "default_notifier", lambda home: kb_notifications.FakeNotifier())
    assert _scheduled_tick(monkeypatch) == 0
    capsys.readouterr()

    kb_notifications.update_settings(
        home,
        {
            "desktop_enabled": True,
            "email_enabled": True,
            "email": {"to": "ops@example.invalid", "smtp_host": "smtp.example.invalid"},
        },
    )
    raising = _RaisingNotifier(home)
    monkeypatch.setattr(kb_notifications, "default_notifier", lambda home: raising)
    before = _tick_records(home)

    rc = _scheduled_tick(monkeypatch, fetch=fetch)

    assert rc == expected_rc
    assert expected_line in capsys.readouterr().out
    # Two open alerts, but each failing channel is tried once, not per alert,
    # and only after the tick journaled its own outcome.
    assert sorted(attempt["channel"] for attempt in raising.attempts) == ["desktop", "email"], raising.attempts
    assert all(attempt["tick_records"] > before for attempt in raising.attempts), (before, raising.attempts)

    status = kb_notifications.delivery_status(home)["channels"]
    for channel in ("desktop", "email"):
        assert status[channel]["last_sent_ts"] is None, status
        assert status[channel]["pending"] == 2, status
        assert status[channel]["last_failure"]["code"] == "notifier_error", status
        assert "CANARY" not in json.dumps(status), status


def test_a_failing_alert_read_never_fails_the_tick(home: Path, fresh_keyring, monkeypatch, notifier, capsys) -> None:
    _arm_paper(home)
    kb_notifications.update_settings(home, {"desktop_enabled": True})

    def broken_collect(home_arg, *, now):
        raise OSError("CANARY alert store unreadable")

    monkeypatch.setattr(alerts, "collect", broken_collect)

    rc = _scheduled_tick(monkeypatch)

    assert rc == 0
    out = capsys.readouterr()
    assert "kraken SUIUSD tick-alerts (paper):" in out.out
    assert "CANARY" not in out.out + out.err
    assert notifier.attempts == []

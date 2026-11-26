"""Email alerts never log in or send without TLS.

``EmailSmtpNotifier`` reads the SMTP password from ``KRELLBOT_SMTP_PASSWORD``.
It used to swallow a failed ``starttls()`` and log in anyway, so the password
could cross a plaintext session. It now fails closed.

Pins:

* A STARTTLS that fails, or a server that does not offer STARTTLS, means no
  ``login`` and no message: the delivery fails with ``smtp_tls_unavailable``.
* That failure is recorded like any other: the channel's ``last_failure``
  carries the code and its fixed message, the occurrence stays pending, the
  channel backs off for ``RETRY_SECONDS``, and nothing is recorded as sent.
* A STARTTLS that succeeds logs in and sends, once per occurrence, as before.
* The password, and server reply text that could carry it, never reach the
  exception, the store, the delivery API or the log.

Every test uses a fake ``smtplib.SMTP`` and an isolated home: no socket is
opened and no mail is sent.
"""

from __future__ import annotations

import json
import logging
import smtplib
import ssl
from pathlib import Path

import pytest

from krellbot import notifications as kb_notifications
from krellbot.application import alerts

T0 = 1_700_000_000
PASSWORD = "hunter2-smtp-secret"
TLS_CODE = "smtp_tls_unavailable"
EMAIL = {
    "to": "ops@example.com",
    "from_addr": "bot@example.com",
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
}


class _FakeSMTP:
    """Stands in for ``smtplib.SMTP``. Records each command; opens no socket.

    ``advertises_starttls`` is what the server's EHLO offers. ``starttls_error``
    is raised by ``starttls()`` when set. ``login`` and ``send_message`` record
    whether TLS was up when they ran.
    """

    def __init__(self, host: str, port: int, *, advertises_starttls: bool, starttls_error: BaseException | None):
        self.host = host
        self.port = port
        self.advertises_starttls = advertises_starttls
        self.starttls_error = starttls_error
        self.tls = False
        self.calls: list[str] = []
        self.logins: list[tuple[str, str, bool]] = []
        self.messages: list[tuple[object, bool]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        self.calls.append("quit")
        return False

    def ehlo(self, name: str = "") -> tuple[int, bytes]:
        self.calls.append("ehlo")
        return 250, b"smtp.example.com"

    def ehlo_or_helo_if_needed(self) -> None:
        pass

    def has_extn(self, opt: str) -> bool:
        return opt.lower() == "starttls" and self.advertises_starttls

    def starttls(self, *args: object, **kwargs: object) -> tuple[int, bytes]:
        self.calls.append("starttls")
        if not self.advertises_starttls:
            # What the real smtplib raises when EHLO did not offer STARTTLS.
            raise smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server.")
        if self.starttls_error is not None:
            raise self.starttls_error
        self.tls = True
        return 220, b"2.0.0 Ready to start TLS"

    def login(self, user: str, password: str, **kwargs: object) -> tuple[int, bytes]:
        self.calls.append("login")
        self.logins.append((user, password, self.tls))
        return 235, b"2.7.0 Authentication successful"

    def send_message(self, msg: object, *args: object, **kwargs: object) -> dict:
        self.calls.append("send_message")
        self.messages.append((msg, self.tls))
        return {}

    def sendmail(self, *args: object, **kwargs: object) -> dict:
        self.calls.append("sendmail")
        self.messages.append((args, self.tls))
        return {}


def _install_smtp(
    monkeypatch: pytest.MonkeyPatch,
    *,
    advertises_starttls: bool = True,
    starttls_error: BaseException | None = None,
) -> list[_FakeSMTP]:
    """Replace ``smtplib.SMTP`` with the fake; return every connection made."""

    connections: list[_FakeSMTP] = []

    def _connect(host: str, port: int = 0, *args: object, **kwargs: object) -> _FakeSMTP:
        smtp = _FakeSMTP(host, port, advertises_starttls=advertises_starttls, starttls_error=starttls_error)
        connections.append(smtp)
        return smtp

    monkeypatch.setattr(kb_notifications.smtplib, "SMTP", _connect)
    return connections


def _notify_direct(password: str = PASSWORD) -> None:
    kb_notifications.EmailSmtpNotifier(EMAIL, password=password).notify(
        channel="email", title="Krellbot critical: needs_reconcile", body="count=1", alert_id="a1"
    )


def _assert_nothing_crossed(smtp: _FakeSMTP) -> None:
    assert "login" not in smtp.calls, smtp.calls
    assert "send_message" not in smtp.calls, smtp.calls
    assert "sendmail" not in smtp.calls, smtp.calls
    assert smtp.logins == []
    assert smtp.messages == []


def _enable_email(home: Path, monkeypatch: pytest.MonkeyPatch) -> list[alerts.Alert]:
    from krellbot.storage.database import OperationalStore
    from krellbot.storage.outbox import audit_needs_reconcile

    audit_needs_reconcile(OperationalStore(home / "ops.sqlite"), coid="stuck-coid-tls")
    monkeypatch.setenv(kb_notifications.SMTP_PASSWORD_ENV, PASSWORD)
    kb_notifications.update_settings(home, {"email_enabled": True, "email": EMAIL})
    return alerts.collect(home, now=T0)


# ---- STARTTLS fails: no login, no message ------------------------------------


@pytest.mark.parametrize(
    "starttls_error",
    [
        smtplib.SMTPResponseException(454, b"4.7.0 TLS not available due to temporary reason"),
        smtplib.SMTPException("STARTTLS failed"),
        ssl.SSLError("certificate verify failed"),
    ],
    ids=["454-reply", "smtp-exception", "tls-handshake"],
)
def test_failed_starttls_never_logs_in_or_sends(monkeypatch: pytest.MonkeyPatch, starttls_error) -> None:
    connections = _install_smtp(monkeypatch, starttls_error=starttls_error)

    with pytest.raises(kb_notifications.NotificationFailed) as raised:
        _notify_direct()

    assert raised.value.code == TLS_CODE
    assert len(connections) == 1
    assert "starttls" in connections[0].calls
    _assert_nothing_crossed(connections[0])


def test_server_without_starttls_never_logs_in_or_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    connections = _install_smtp(monkeypatch, advertises_starttls=False)

    with pytest.raises(kb_notifications.NotificationFailed) as raised:
        _notify_direct()

    assert raised.value.code == TLS_CODE
    assert len(connections) == 1
    _assert_nothing_crossed(connections[0])


@pytest.mark.parametrize("advertises_starttls", [True, False], ids=["starttls-fails", "no-starttls"])
def test_no_password_still_never_sends_in_the_clear(monkeypatch: pytest.MonkeyPatch, advertises_starttls) -> None:
    connections = _install_smtp(
        monkeypatch,
        advertises_starttls=advertises_starttls,
        starttls_error=smtplib.SMTPResponseException(454, b"4.7.0 TLS not available"),
    )

    with pytest.raises(kb_notifications.NotificationFailed) as raised:
        _notify_direct(password="")

    assert raised.value.code == TLS_CODE
    _assert_nothing_crossed(connections[0])


def test_tls_failure_has_a_fixed_operator_message() -> None:
    message = kb_notifications.FAILURE_MESSAGES[TLS_CODE]
    assert "TLS" in message
    assert message != kb_notifications._UNKNOWN_FAILURE_MESSAGE


# ---- the failure is recorded as failed, not sent ------------------------------


def test_failed_starttls_is_recorded_as_failed_not_sent(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connections = _install_smtp(
        monkeypatch, starttls_error=smtplib.SMTPResponseException(454, b"4.7.0 TLS not available")
    )
    collected = _enable_email(home, monkeypatch)

    assert kb_notifications.maybe_notify(home, collected, now=T0) == 0

    email = kb_notifications.delivery_status(home)["channels"]["email"]
    assert email["enabled"] is True
    assert email["last_sent_ts"] is None
    assert email["pending"] == 1
    assert email["last_failure"] == {
        "ts": T0,
        "code": TLS_CODE,
        "message": kb_notifications.FAILURE_MESSAGES[TLS_CODE],
        "kind": "needs_reconcile",
    }

    # The channel backs off: no new connection inside RETRY_SECONDS.
    assert kb_notifications.maybe_notify(home, collected, now=T0 + 30) == 0
    assert len(connections) == 1

    # The retry fails closed the same way.
    assert kb_notifications.maybe_notify(home, collected, now=T0 + kb_notifications.RETRY_SECONDS) == 0
    assert len(connections) == 2
    for smtp in connections:
        _assert_nothing_crossed(smtp)
    assert kb_notifications.delivery_status(home)["channels"]["email"]["last_sent_ts"] is None


def test_server_without_starttls_is_recorded_as_failed(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connections = _install_smtp(monkeypatch, advertises_starttls=False)
    collected = _enable_email(home, monkeypatch)

    assert kb_notifications.maybe_notify(home, collected, now=T0) == 0

    email = kb_notifications.delivery_status(home)["channels"]["email"]
    assert email["last_failure"]["code"] == TLS_CODE
    assert email["last_sent_ts"] is None
    assert email["pending"] == 1
    _assert_nothing_crossed(connections[0])


# ---- STARTTLS succeeds: log in and send, once ---------------------------------


def test_successful_starttls_logs_in_and_sends_once(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connections = _install_smtp(monkeypatch)
    collected = _enable_email(home, monkeypatch)

    assert kb_notifications.maybe_notify(home, collected, now=T0) == 1
    assert kb_notifications.maybe_notify(home, collected, now=T0 + 5) == 0
    assert kb_notifications.maybe_notify(home, collected, now=T0 + 300) == 0

    assert len(connections) == 1
    smtp = connections[0]
    assert smtp.calls.index("starttls") < smtp.calls.index("login") < smtp.calls.index("send_message")
    assert smtp.logins == [(EMAIL["from_addr"], PASSWORD, True)]
    assert len(smtp.messages) == 1
    msg, tls = smtp.messages[0]
    assert tls is True
    assert msg["To"] == EMAIL["to"]
    assert msg["Subject"].endswith(": needs_reconcile")

    email = kb_notifications.delivery_status(home)["channels"]["email"]
    assert email["last_sent_ts"] == T0
    assert email["pending"] == 0
    assert email["last_failure"] is None


# ---- the password never reaches the failure record ----------------------------


def test_password_never_appears_in_the_recorded_failure(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # A reply that echoes the secret: its text must not be kept anywhere.
    echoing = smtplib.SMTPResponseException(454, f"4.7.0 TLS unavailable for {PASSWORD}".encode())
    _install_smtp(monkeypatch, starttls_error=echoing)
    collected = _enable_email(home, monkeypatch)
    caplog.set_level(logging.DEBUG)

    with pytest.raises(kb_notifications.NotificationFailed) as raised:
        _notify_direct()
    exc = raised.value
    assert str(exc) == TLS_CODE
    assert exc.args == (TLS_CODE,)
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True

    assert kb_notifications.maybe_notify(home, collected, now=T0) == 0

    store_text = (home / "run" / kb_notifications.SENT_FILE).read_text(encoding="utf-8")
    status_text = json.dumps(kb_notifications.delivery_status(home))
    assert TLS_CODE in store_text
    for text in (store_text, status_text, caplog.text):
        assert PASSWORD not in text
        assert "TLS unavailable for" not in text
    for path in home.rglob("*"):
        if path.is_file():
            assert PASSWORD.encode() not in path.read_bytes(), path

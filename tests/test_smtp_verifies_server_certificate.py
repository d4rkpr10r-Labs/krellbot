"""Email alerts verify the SMTP server's TLS certificate.

``EmailSmtpNotifier`` reads the SMTP password from ``KRELLBOT_SMTP_PASSWORD``
and refuses to log in without STARTTLS. It used to call ``starttls()`` with no
``context``, so Python wrapped the socket with an unverified context
(``CERT_NONE``, no hostname check): an active man-in-the-middle could present
any certificate and read the password. It now verifies the chain against the
system trust store and the configured SMTP host.

Pins:

* ``starttls`` receives a verifying context: ``check_hostname`` is True and
  ``verify_mode`` is ``CERT_REQUIRED``. The connection is opened to the
  configured host, which is the name the certificate is checked against.
* A certificate that fails verification means no ``login`` and no message: the
  delivery fails with ``smtp_tls_unverified``, recorded like any other failure,
  and neither the password nor the certificate or server text is kept.
* Any other STARTTLS failure still records ``smtp_tls_unavailable``.
* A verified STARTTLS logs in and sends, once per occurrence, as before.

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
UNVERIFIED_CODE = "smtp_tls_unverified"
UNAVAILABLE_CODE = "smtp_tls_unavailable"
EMAIL = {
    "to": "ops@example.com",
    "from_addr": "bot@example.com",
    "smtp_host": "smtp.example.com",
    "smtp_port": 587,
}
# Text a verification error can carry: certificate subject and issuer names.
CERT_TEXT = "CN=mitm-relay.invalid"


class _FakeSMTP:
    """Stands in for ``smtplib.SMTP``. Records each command; opens no socket.

    ``starttls_error`` is raised by ``starttls()`` when set. ``starttls_contexts``
    keeps the ``context`` each ``starttls()`` call received (None when absent).
    ``login`` and ``send_message`` record whether TLS was up when they ran.
    """

    def __init__(self, host: str, port: int, *, starttls_error: BaseException | None) -> None:
        self.host = host
        self.port = port
        self.starttls_error = starttls_error
        self.tls = False
        self.calls: list[str] = []
        self.starttls_contexts: list[object] = []
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
        return opt.lower() == "starttls"

    def starttls(self, keyfile: object = None, certfile: object = None, context: object = None) -> tuple[int, bytes]:
        self.calls.append("starttls")
        self.starttls_contexts.append(context)
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


def _install_smtp(monkeypatch: pytest.MonkeyPatch, *, starttls_error: BaseException | None = None) -> list[_FakeSMTP]:
    """Replace ``smtplib.SMTP`` with the fake; return every connection made."""

    connections: list[_FakeSMTP] = []

    def _connect(host: str, port: int = 0, *args: object, **kwargs: object) -> _FakeSMTP:
        smtp = _FakeSMTP(host, port, starttls_error=starttls_error)
        connections.append(smtp)
        return smtp

    monkeypatch.setattr(kb_notifications.smtplib, "SMTP", _connect)
    return connections


def _cert_verification_error(reason: str) -> ssl.SSLCertVerificationError:
    """The error ``SSLContext.wrap_socket`` raises when the server's certificate fails."""

    exc = ssl.SSLCertVerificationError(
        1, f"[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: {reason} (_ssl.c:1000)"
    )
    exc.verify_code = 18
    exc.verify_message = reason
    return exc


# A self-signed relay, a certificate for another host, and an error whose text
# echoes the password: none of that text may be kept.
CERT_FAILURES = [
    _cert_verification_error(f"self-signed certificate ({CERT_TEXT})"),
    _cert_verification_error(f"Hostname mismatch, certificate is not valid for 'smtp.example.com'. {CERT_TEXT}"),
    _cert_verification_error(f"unable to get local issuer certificate {CERT_TEXT} {PASSWORD}"),
]
CERT_FAILURE_IDS = ["self-signed", "hostname-mismatch", "echoes-password"]


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


def _assert_verifying_context(context: object) -> None:
    assert isinstance(context, ssl.SSLContext), context
    assert context.check_hostname is True
    assert context.verify_mode == ssl.CERT_REQUIRED


def _enable_email(home: Path, monkeypatch: pytest.MonkeyPatch) -> list[alerts.Alert]:
    from krellbot.storage.database import OperationalStore
    from krellbot.storage.outbox import audit_needs_reconcile

    audit_needs_reconcile(OperationalStore(home / "ops.sqlite"), coid="stuck-coid-cert")
    monkeypatch.setenv(kb_notifications.SMTP_PASSWORD_ENV, PASSWORD)
    kb_notifications.update_settings(home, {"email_enabled": True, "email": EMAIL})
    return alerts.collect(home, now=T0)


# ---- starttls verifies the certificate and the host ---------------------------


def test_starttls_receives_a_verifying_context(monkeypatch: pytest.MonkeyPatch) -> None:
    connections = _install_smtp(monkeypatch)

    _notify_direct()

    assert len(connections) == 1
    smtp = connections[0]
    # The certificate's name is checked against the host smtplib connected to.
    assert smtp.host == EMAIL["smtp_host"]
    assert smtp.port == EMAIL["smtp_port"]
    assert len(smtp.starttls_contexts) == 1
    _assert_verifying_context(smtp.starttls_contexts[0])


# ---- verification fails: no login, no message, smtp_tls_unverified ------------


@pytest.mark.parametrize("starttls_error", CERT_FAILURES, ids=CERT_FAILURE_IDS)
def test_unverified_certificate_never_logs_in_or_sends(monkeypatch: pytest.MonkeyPatch, starttls_error) -> None:
    connections = _install_smtp(monkeypatch, starttls_error=starttls_error)

    with pytest.raises(kb_notifications.NotificationFailed) as raised:
        _notify_direct()

    exc = raised.value
    assert exc.code == UNVERIFIED_CODE
    assert str(exc) == UNVERIFIED_CODE
    assert exc.args == (UNVERIFIED_CODE,)
    # ``from None``: the certificate text is neither the cause nor shown as context.
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True
    assert len(connections) == 1
    assert "starttls" in connections[0].calls
    _assert_nothing_crossed(connections[0])


def test_unverified_certificate_without_a_password_still_never_sends(monkeypatch: pytest.MonkeyPatch) -> None:
    connections = _install_smtp(monkeypatch, starttls_error=CERT_FAILURES[0])

    with pytest.raises(kb_notifications.NotificationFailed) as raised:
        _notify_direct(password="")

    assert raised.value.code == UNVERIFIED_CODE
    _assert_nothing_crossed(connections[0])


def test_unverified_certificate_has_a_fixed_operator_message() -> None:
    message = kb_notifications.FAILURE_MESSAGES[UNVERIFIED_CODE]
    assert "certificate" in message
    assert "did not log in or send" in message
    assert message != kb_notifications.FAILURE_MESSAGES[UNAVAILABLE_CODE]
    assert message != kb_notifications._UNKNOWN_FAILURE_MESSAGE


@pytest.mark.parametrize("starttls_error", CERT_FAILURES, ids=CERT_FAILURE_IDS)
def test_unverified_certificate_is_recorded_without_password_or_cert_text(
    home: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, starttls_error
) -> None:
    connections = _install_smtp(monkeypatch, starttls_error=starttls_error)
    collected = _enable_email(home, monkeypatch)
    caplog.set_level(logging.DEBUG)

    assert kb_notifications.maybe_notify(home, collected, now=T0) == 0

    email = kb_notifications.delivery_status(home)["channels"]["email"]
    assert email["enabled"] is True
    assert email["last_sent_ts"] is None
    assert email["pending"] == 1
    assert email["last_failure"] == {
        "ts": T0,
        "code": UNVERIFIED_CODE,
        "message": kb_notifications.FAILURE_MESSAGES[UNVERIFIED_CODE],
        "kind": "needs_reconcile",
    }

    # The channel backs off, then retries and fails closed the same way.
    assert kb_notifications.maybe_notify(home, collected, now=T0 + 30) == 0
    assert len(connections) == 1
    assert kb_notifications.maybe_notify(home, collected, now=T0 + kb_notifications.RETRY_SECONDS) == 0
    assert len(connections) == 2
    for smtp in connections:
        _assert_verifying_context(smtp.starttls_contexts[0])
        _assert_nothing_crossed(smtp)

    store_text = (home / "run" / kb_notifications.SENT_FILE).read_text(encoding="utf-8")
    status_text = json.dumps(kb_notifications.delivery_status(home))
    assert UNVERIFIED_CODE in store_text
    for text in (store_text, status_text, caplog.text):
        assert PASSWORD not in text
        assert CERT_TEXT not in text
        assert "CERTIFICATE_VERIFY_FAILED" not in text
    for path in home.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            assert PASSWORD.encode() not in data, path
            assert CERT_TEXT.encode() not in data, path


# ---- other TLS failures stay smtp_tls_unavailable ------------------------------


@pytest.mark.parametrize(
    "starttls_error",
    [
        ssl.SSLError(1, "[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1000)"),
        ssl.SSLEOFError(8, "EOF occurred in violation of protocol (_ssl.c:1000)"),
        smtplib.SMTPResponseException(454, b"4.7.0 TLS not available due to temporary reason"),
        smtplib.SMTPNotSupportedError("STARTTLS extension not supported by server."),
        ConnectionResetError(104, "Connection reset by peer"),
    ],
    ids=["ssl-error", "ssl-eof", "454-reply", "not-supported", "connection-reset"],
)
def test_other_tls_failures_stay_tls_unavailable(home: Path, monkeypatch: pytest.MonkeyPatch, starttls_error) -> None:
    connections = _install_smtp(monkeypatch, starttls_error=starttls_error)

    with pytest.raises(kb_notifications.NotificationFailed) as raised:
        _notify_direct()
    assert raised.value.code == UNAVAILABLE_CODE
    _assert_nothing_crossed(connections[0])

    collected = _enable_email(home, monkeypatch)
    assert kb_notifications.maybe_notify(home, collected, now=T0) == 0
    email = kb_notifications.delivery_status(home)["channels"]["email"]
    assert email["last_failure"]["code"] == UNAVAILABLE_CODE
    assert email["last_sent_ts"] is None
    assert email["pending"] == 1
    _assert_nothing_crossed(connections[1])


# ---- verified STARTTLS: log in and send, once ---------------------------------


def test_verified_starttls_logs_in_and_sends_once(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    connections = _install_smtp(monkeypatch)
    collected = _enable_email(home, monkeypatch)

    assert kb_notifications.maybe_notify(home, collected, now=T0) == 1
    assert kb_notifications.maybe_notify(home, collected, now=T0 + 5) == 0
    assert kb_notifications.maybe_notify(home, collected, now=T0 + 300) == 0

    assert len(connections) == 1
    smtp = connections[0]
    _assert_verifying_context(smtp.starttls_contexts[0])
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

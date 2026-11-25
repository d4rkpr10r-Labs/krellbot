"""Diagnostic exports contain only bounded, authenticated paper evidence."""

import json
from decimal import Decimal

from krellbot.application.paper import PaperService
from tests.test_community_pack_paper_arm import _auth, _bootstrap, _get, _pack_dict
from tests.test_ns06_api import TEST_PORT, _build_app, asgi_call


def test_report_requires_loopback_session_and_is_no_store(home):
    app = _build_app(home)
    status, _, _, _ = asgi_call(
        app, method="GET", path="/api/v1/diagnostics/report", headers=[("Host", f"127.0.0.1:{TEST_PORT}")]
    )
    assert status == 403
    headers = _auth(*_bootstrap(app))
    status, response_headers, body, _ = asgi_call(
        app, method="GET", path="/api/v1/diagnostics/report", headers=[("Host", f"127.0.0.1:{TEST_PORT}"), *headers]
    )
    assert status == 200
    assert ("cache-control", "no-store") in response_headers
    report = json.loads(body)
    assert report["events"] == []
    assert report["reproduction_steps"] == ""
    assert set(report) == {"schema_version", "build", "os", "scheduler", "events", "reproduction_steps"}


def test_report_bounds_and_allowlists_evidence(home):
    pack = _pack_dict("paper-report")
    path = home / "pack.json"
    path.write_text(json.dumps(pack), encoding="utf-8")
    result = PaperService(home=home).arm(path, venue="kraken", mode="paper", paper_balance=Decimal(1000))
    assert result.ok
    from krellbot.config import load_config

    armed = load_config(home).armed[0]
    journal = home / "journal"
    journal.mkdir(exist_ok=True)
    private = "sensitive-key-/private/user/path"
    records = []
    for n in range(105):
        records.append(
            {
                "kind": "tick",
                "ts": armed.armed_at_ts + n,
                "venue": "kraken",
                "pack": "paper-report",
                "detail": {
                    "pair": "SUIUSD",
                    "metadata_refusal": "metadata_stale",
                    "balance": private,
                    "secret": private,
                },
                "headers": private,
            }
        )
    records.append(
        {
            "kind": "tick",
            "ts": armed.armed_at_ts + 200,
            "venue": "kraken",
            "pack": "paper-report",
            "detail": {"pair": "SUIUSD", "metadata_refusal": private},
        }
    )
    records.append({"kind": "tick", "ts": 0, "venue": [], "detail": {"pair": "SUIUSD"}})
    (journal / "test.jsonl").write_text("broken json\n" + "\n".join(json.dumps(x) for x in records), encoding="utf-8")
    app = _build_app(home)
    status, body = _get(app, "/api/v1/diagnostics/report", headers=_auth(*_bootstrap(app)))
    assert status == 200
    assert private not in body.decode()
    report = json.loads(body)
    assert len(report["events"]) == 100
    assert report["events"][0]["ts"] == armed.armed_at_ts + 6
    assert report["events"][-1]["refusal_code"] is None
    assert report["events"][-2]["refusal_code"] == "metadata_stale"
    assert all(set(event) == {"ts", "venue", "pair", "source", "refusal_code"} for event in report["events"])


def test_unavailable_scheduler_and_malformed_files_do_not_break_report(home, monkeypatch):
    from krellbot.service import supervise

    def unavailable(*args, **kwargs):
        raise OSError("sensitive-error")

    monkeypatch.setattr(supervise, "scheduler_status", unavailable)
    journal = home / "journal"
    journal.mkdir(exist_ok=True)
    (journal / "malformed.jsonl").write_bytes(b"\xff\xfe")
    app = _build_app(home)
    status, body = _get(app, "/api/v1/diagnostics/report", headers=_auth(*_bootstrap(app)))
    assert status == 200
    assert b"sensitive-error" not in body
    assert json.loads(body)["scheduler"] is None

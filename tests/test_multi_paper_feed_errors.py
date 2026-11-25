"""An aborted venue tick records the status of each armed paper market."""

from decimal import Decimal

from krellbot import cli, journal
from tests.test_multi_venue_paper_status import _arm, _write_pack


class PublicTransport:
    def get(self, url, headers=None):
        return {"error": [], "result": {}}

    def post(self, *args, **kwargs):
        raise AssertionError("paper ticks must not call private endpoints")


def test_venue_fetch_failure_marks_every_affected_paper_run(home, fresh_keyring, monkeypatch):
    first = _write_pack(home, pack_id="first", venue="kraken", pair="SUIUSD")
    second = _write_pack(home, pack_id="second", venue="kraken", pair="BTCUSD")
    third = _write_pack(home, pack_id="third", venue="kraken", pair="ETHUSD")
    _arm(home, first, venue="kraken", paper_balance=Decimal(1000))
    _arm(home, second, venue="kraken", paper_balance=Decimal(1000))
    _arm(home, third, venue="kraken", paper_balance=Decimal(1000))

    def fetch(venue, pair, timeframe, transport):
        if pair == "BTCUSD":
            raise ConnectionError("sensitive exchange response")
        return []

    assert cli.cmd_tick(["--venue", "kraken"], fetch=fetch, transport=PublicTransport()) == 1
    records = [record for record in journal.read_records_lenient(home) if record.get("kind") == "tick"]
    refusals = {
        record["pack"]: record["detail"]
        for record in records
        if record["detail"].get("reason") in {"fetch_error", "check_incomplete"}
    }
    assert "first" not in refusals
    assert refusals["second"]["reason"] == "fetch_error"
    assert refusals["third"]["reason"] == "check_incomplete"
    assert all(record["exception"] == "ConnectionError" for record in refusals.values())
    assert all(record["mode"] == "paper" for record in refusals.values())
    assert "sensitive exchange response" not in str(records)

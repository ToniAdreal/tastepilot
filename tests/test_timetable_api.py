"""Timetable API provider: scheduled inter-leg services fetched over
HTTP from a user-supplied endpoint serving the same schema as a
timetable file (run 16), behind the same feasibility interface.
Deterministic: every fetch goes through an injectable fake transport
(or a real loopback HTTP server for the CLI end-to-end test) — no
external API, no account, no key. Only the endpoint's host is ever
recorded; the URL's query string appears in no plan, error, or repr.
"""
import json
import re
import threading
import urllib.error
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tastepilot import cli
from tastepilot.agent import TastePilotAgent
from tastepilot.qloo import MockQlooClient
from tastepilot.timetable import (ApiTimetableProvider,
                                  ChainedTimetableProvider,
                                  fetch_timetable, provider_for)
from tastepilot.travel import assess_transfers

REF = date(2026, 10, 10)
ROOT = Path(__file__).resolve().parents[1]
SECRET_URL = "https://schedules.example.com:8443/v1/services.json?token=SECRETTOKEN"

SERVICES_JSON = json.dumps({"services": [
    {"from": "Kyoto", "to": "Paris", "duration_hours": 11.0,
     "mode": "flight", "service": "Demo Air 101"},
]}).encode()


def leg(location, start=None, end=None):
    return {"location": location, "start": start, "end": end,
            "nights": None, "dates": []}


def fake_transport(responses):
    """Transport stub: pops (status, body) or raises queued errors."""
    calls = []

    def transport(url, timeout):
        calls.append((url, timeout))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    return transport, calls


def test_fetch_json_success_provenance_is_host_only():
    transport, calls = fake_transport([(200, SERVICES_JSON)])
    parsed = fetch_timetable(SECRET_URL, transport=transport)
    assert parsed["format"] == "timetable_api_json"
    assert parsed["source_kind"] == "api"
    # Host + port only: no scheme, path, userinfo, or query string.
    assert parsed["source_file"] == "schedules.example.com:8443"
    assert parsed["entries"][0]["travel_hours"] == 11.0
    assert parsed["entries"][0]["service"] == "Demo Air 101"
    assert "SECRETTOKEN" not in json.dumps(parsed)
    assert "services.json" not in json.dumps(parsed)
    assert len(calls) == 1


def test_fetch_csv_success():
    body = b"from,to,duration_hours,mode\nParis,New York,7.5,flight\n"
    transport, _ = fake_transport([(200, body)])
    parsed = fetch_timetable("https://api.example.com/services.csv",
                             transport=transport)
    assert parsed["format"] == "timetable_api_csv"
    assert parsed["entries"][0]["travel_hours"] == 7.5


def test_fetch_retries_transient_status_then_succeeds():
    transport, calls = fake_transport([(503, b""), (200, SERVICES_JSON)])
    sleeps = []
    parsed = fetch_timetable("https://api.example.com/s.json",
                             transport=transport, sleeper=sleeps.append)
    assert parsed["entries"][0]["travel_hours"] == 11.0
    assert len(calls) == 2 and sleeps == [0.5]


def test_fetch_transient_exhaustion_is_categorised_http_error():
    transport, calls = fake_transport([(503, b"")] * 3)
    sleeps = []
    with pytest.raises(ValueError, match="http_error"):
        fetch_timetable(SECRET_URL, transport=transport,
                        sleeper=sleeps.append)
    assert len(calls) == 3 and sleeps == [0.5, 1.0]


def test_fetch_non_transient_status_fails_immediately_without_echo():
    transport, calls = fake_transport([(404, b"SECRETROW body")])
    with pytest.raises(ValueError) as excinfo:
        fetch_timetable(SECRET_URL, transport=transport,
                        sleeper=lambda s: None)
    message = str(excinfo.value)
    assert "http_error" in message and "404" in message
    assert "SECRETTOKEN" not in message and "SECRETROW" not in message
    assert len(calls) == 1


def test_fetch_network_error_retries_then_categorised_without_url():
    boom = urllib.error.URLError("dial failed for " + SECRET_URL)
    transport, calls = fake_transport([boom, boom, boom])
    with pytest.raises(ValueError) as excinfo:
        fetch_timetable(SECRET_URL, transport=transport,
                        sleeper=lambda s: None)
    message = str(excinfo.value)
    assert "network_error" in message and "URLError" in message
    assert "SECRETTOKEN" not in message
    assert "schedules.example.com" not in message
    assert len(calls) == 3
    # A network failure that recovers is just a retry.
    transport2, calls2 = fake_transport(
        [urllib.error.URLError("reset"), (200, SERVICES_JSON)])
    parsed = fetch_timetable("https://api.example.com/s.json",
                             transport=transport2, sleeper=lambda s: None)
    assert parsed["entries"] and len(calls2) == 2


def test_fetch_rejects_non_http_urls_before_any_call():
    transport, calls = fake_transport([(200, SERVICES_JSON)])
    for bad in ("ftp://api.example.com/s.json", "api.example.com/s.json",
                "https:///no-host", ""):
        with pytest.raises(ValueError, match="unsupported"):
            fetch_timetable(bad, transport=transport)
    assert calls == []


def test_fetch_parse_failures_keep_file_categories():
    transport, _ = fake_transport([(200, b"{not json")])
    with pytest.raises(ValueError, match="bad_json"):
        fetch_timetable("https://api.example.com/s.json",
                        transport=transport)
    transport, _ = fake_transport([(200, b" " * 1_048_577)])
    with pytest.raises(ValueError, match="too_large"):
        fetch_timetable("https://api.example.com/s.json",
                        transport=transport)
    transport, _ = fake_transport([(200, b'{"services": []}')])
    with pytest.raises(ValueError, match="no_items"):
        fetch_timetable("https://api.example.com/s.json",
                        transport=transport)


def test_api_provider_exact_reverse_and_miss():
    transport, _ = fake_transport([(200, SERVICES_JSON)])
    parsed = fetch_timetable("https://api.example.com/s.json",
                             transport=transport)
    provider = ApiTimetableProvider([parsed])
    hit = provider.lookup("Kyoto", "Paris")
    assert hit["source"] == "timetable_api"
    assert hit["travel_hours"] == 11.0 and hit["service"] == "Demo Air 101"
    rev = provider.lookup("Paris", "Kyoto")
    assert rev["travel_hours"] == 11.0 and rev["reversed"] is True
    assert provider.lookup("Kyoto", "Tokyo") is None


def test_provider_for_api_is_chained_and_falls_back_to_estimate():
    transport, _ = fake_transport([(200, SERVICES_JSON)])
    parsed = fetch_timetable("https://api.example.com/s.json",
                             transport=transport)
    provider = provider_for([parsed])
    assert isinstance(provider, ChainedTimetableProvider)
    assert provider.lookup("Kyoto", "Paris")["source"] == "timetable_api"
    fallback = provider.lookup("Paris", "New York")
    assert fallback["source"] == "offline_estimate"


def test_api_scheduled_duration_changes_the_verdict():
    transport, _ = fake_transport([(200, SERVICES_JSON)])
    parsed = fetch_timetable("https://api.example.com/s.json",
                             transport=transport)
    transfers, warnings = assess_transfers(
        [leg("Kyoto", "2026-11-03", "2026-11-05"),
         leg("Paris", "2026-11-05", "2026-11-07")],
        provider=provider_for([parsed]))
    t = transfers[0]
    # The 15.3 h offline estimate is infeasible same-day; the API's
    # scheduled 11 h service is tight in the 12 h window.
    assert t["source"] == "timetable_api" and t["travel_hours"] == 11.0
    assert t["status"] == "tight"
    assert "scheduled" in t["detail"] and "Demo Air 101" in t["detail"]
    assert [w["code"] for w in warnings] == ["transfer_tight"]


def test_mixed_file_and_api_sources_in_one_plan():
    agent = TastePilotAgent(MockQlooClient())
    file_tt = {"source_file": "services.json", "format": "timetable_json",
               "entries": [{"from": "Kyoto", "to": "Paris",
                            "travel_hours": 13.5, "mode": "flight",
                            "service": None}]}
    transport, _ = fake_transport([(200, json.dumps({"services": [
        {"from": "Paris", "to": "New York", "duration_hours": 8.0,
         "mode": "flight"}]}).encode())])
    api_tt = fetch_timetable("https://api.example.com/s.json",
                              transport=transport)
    plan = agent.run("a trip: Kyoto Nov 3-5, then Paris Nov 6-8, "
                     "then New York Nov 9-10, I love Amelie",
                     reference_date=REF, timetables=[file_tt, api_tt])
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]
    assert [t["source"] for t in plan["transfers"]] == [
        "timetable_file", "timetable_api"]
    assert plan["timetable"][1]["format"] == "timetable_api_json"
    assert plan["timetable"][1]["source_file"] == "api.example.com"


def test_agent_plan_carries_api_provenance_and_no_qloo_leak():
    agent = TastePilotAgent(MockQlooClient())
    transport, _ = fake_transport([(200, SERVICES_JSON)])
    api_tt = fetch_timetable(SECRET_URL, transport=transport)
    plan = agent.run("a trip: Kyoto Nov 3-5, then Paris Nov 5, "
                     "I love Amelie", reference_date=REF,
                     timetables=[api_tt])
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]
    assert plan["timetable"] == [{
        "source_file": "schedules.example.com:8443",
        "format": "timetable_api_json", "entries": 1}]
    assert plan["transfers"][0]["source"] == "timetable_api"
    assert plan["transfers"][0]["status"] == "tight"
    assert "SECRETTOKEN" not in json.dumps(plan)
    for entry in plan["evidence"]["entries"]:
        if entry["kind"] == "qloo_call":
            assert "Demo Air 101" not in str(entry)
            assert "travel_hours" not in str(entry)


class _TimetableHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib handler name)
        if self.path.startswith("/services"):
            body = SERVICES_JSON
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
        else:
            body = b"nope"
            self.send_response(404)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture()
def timetable_server():
    server = HTTPServer(("127.0.0.1", 0), _TimetableHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


def test_cli_timetable_url_end_to_end(timetable_server, capsys):
    code = cli.main(["plan", "Kyoto Nov 3-5, then Paris Nov 5, "
                             "I love Amelie",
                     "--timetable-url",
                     timetable_server + "/services.json?token=SECRETTOKEN"])
    out = capsys.readouterr().out
    assert code == 0  # a tight transfer never blocks the plan
    assert "timetable_api_json" in out and "1 matched" in out
    assert "source=timetable_api" in out and "status=tight" in out
    assert "SECRETTOKEN" not in out


def test_cli_timetable_url_failures_exit_2(timetable_server, capsys):
    code = cli.main(["plan", "a weekend in Kyoto",
                     "--timetable-url", timetable_server + "/missing"])
    err = capsys.readouterr().err
    assert code == 2 and "timetable failed: http_error" in err
    assert "Traceback" not in err
    code = cli.main(["plan", "a weekend in Kyoto",
                     "--timetable-url", "ftp://example.com/s.json"])
    err = capsys.readouterr().err
    assert code == 2 and "unsupported" in err
    code = cli.main(["plan", "a weekend in Kyoto",
                     "--timetable-url", "http://127.0.0.1:1/s.json"])
    err = capsys.readouterr().err
    assert code == 2 and "network_error" in err
    assert "Traceback" not in err


def test_cli_help_and_ui_and_docs_carry_api_wiring(capsys):
    with pytest.raises(SystemExit):
        cli.main(["plan", "--help"])
    assert "--timetable-url" in capsys.readouterr().out
    html = (ROOT / "tastepilot" / "static" / "index.html").read_text()
    assert "timetable_api" in html
    readme = (ROOT / "README.md").read_text()
    assert "timetable-url" in readme and "timetable_api" in readme
    submission = (ROOT / "SUBMISSION.md").read_text()
    assert re.search(r"timetable API", submission)

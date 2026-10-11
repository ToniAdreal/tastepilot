"""Taste-feed API imports: exported taste lists fetched over HTTP
from a user-supplied endpoint serving the same schema as an import
file (run 15), through the same sealed seed path. Deterministic:
every fetch goes through an injectable fake transport (or a real
loopback HTTP server for the CLI end-to-end test) — no external
API, no account, no OAuth, no key. Only the endpoint's host is ever
recorded; the URL's query string appears in no plan, error, or repr.
"""
import json
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from tastepilot import cli
from tastepilot.agent import TastePilotAgent
from tastepilot.imports import fetch_import
from tastepilot.qloo import MockQlooClient

ROOT = Path(__file__).resolve().parents[1]
SECRET_URL = "https://taste.example.com:8443/v1/Playlist.json?token=SECRETTOKEN"

SPOTIFY = json.dumps({"playlists": [{
    "name": "Faves",
    "items": [
        {"track": {"trackName": "Kind of Blue",
                   "artistName": "Miles Davis"}},
        {"track": {"trackName": "An Unknown Song",
                   "artistName": "Someone New"}},
    ]}]}).encode()

LETTERBOXD = (b"Date,Name,Year,Letterboxd URI\n"
              b"2024-01-01,Spirited Away,2001,https://boxd.it/x\n")


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
    transport, calls = fake_transport([(200, SPOTIFY)])
    imp = fetch_import(SECRET_URL, transport=transport)
    assert imp["format"] == "spotify_api_json"
    assert imp["source_kind"] == "api"
    # Host + port only: no scheme, path, userinfo, or query string.
    assert imp["source_file"] == "taste.example.com:8443"
    assert [i["name"] for i in imp["items"]] == [
        "Kind of Blue (Miles Davis)", "An Unknown Song (Someone New)"]
    assert all(i["source"] == "spotify_api_json" for i in imp["items"])
    assert imp["seed_names"][0] == "Kind of Blue (Miles Davis)"
    assert "SECRETTOKEN" not in json.dumps(imp)
    assert "Playlist.json" not in json.dumps(imp)
    assert len(calls) == 1


def test_fetch_csv_and_text_success():
    transport, _ = fake_transport([(200, LETTERBOXD)])
    imp = fetch_import("https://api.example.com/watchlist.csv",
                       transport=transport)
    assert imp["format"] == "letterboxd_api_csv"
    assert imp["seed_names"] == ["Spirited Away"]
    transport, _ = fake_transport([(200, b"Spirited Away\nBon Iver\n")])
    imp = fetch_import("https://api.example.com/list.txt",
                       transport=transport)
    assert imp["format"] == "text_api_list"
    assert imp["seed_names"] == ["Spirited Away", "Bon Iver"]


def test_fetch_retries_transient_status_then_succeeds():
    transport, calls = fake_transport([(503, b""), (200, SPOTIFY)])
    sleeps = []
    imp = fetch_import("https://api.example.com/Playlist.json",
                       transport=transport, sleeper=sleeps.append)
    assert imp["items"] and len(calls) == 2 and sleeps == [0.5]


def test_fetch_transient_exhaustion_is_categorised_http_error():
    transport, calls = fake_transport([(503, b"")] * 3)
    sleeps = []
    with pytest.raises(ValueError, match="http_error"):
        fetch_import(SECRET_URL, transport=transport,
                     sleeper=sleeps.append)
    assert len(calls) == 3 and sleeps == [0.5, 1.0]


def test_fetch_non_transient_status_fails_immediately_without_echo():
    transport, calls = fake_transport([(404, b"SECRETROW body")])
    with pytest.raises(ValueError) as excinfo:
        fetch_import(SECRET_URL, transport=transport,
                     sleeper=lambda s: None)
    message = str(excinfo.value)
    assert "http_error" in message and "404" in message
    assert "SECRETTOKEN" not in message and "SECRETROW" not in message
    assert len(calls) == 1


def test_fetch_network_error_retries_then_categorised_without_url():
    boom = urllib.error.URLError("dial failed for " + SECRET_URL)
    transport, calls = fake_transport([boom, boom, boom])
    with pytest.raises(ValueError) as excinfo:
        fetch_import(SECRET_URL, transport=transport,
                     sleeper=lambda s: None)
    message = str(excinfo.value)
    assert "network_error" in message and "URLError" in message
    assert "SECRETTOKEN" not in message
    assert "taste.example.com" not in message
    assert len(calls) == 3
    # A network failure that recovers is just a retry.
    transport2, calls2 = fake_transport(
        [urllib.error.URLError("reset"), (200, SPOTIFY)])
    imp = fetch_import("https://api.example.com/Playlist.json",
                       transport=transport2, sleeper=lambda s: None)
    assert imp["items"] and len(calls2) == 2


def test_fetch_rejects_non_http_urls_before_any_call():
    transport, calls = fake_transport([(200, SPOTIFY)])
    for bad in ("ftp://api.example.com/x.json", "api.example.com/x.json",
                "https:///no-host", ""):
        with pytest.raises(ValueError, match="unsupported"):
            fetch_import(bad, transport=transport)
    assert calls == []


def test_fetch_parse_failures_keep_file_categories():
    transport, _ = fake_transport([(200, b"{not json")])
    with pytest.raises(ValueError, match="bad_json"):
        fetch_import("https://api.example.com/x.json",
                     transport=transport)
    transport, _ = fake_transport([(200, b" " * 1_048_577)])
    with pytest.raises(ValueError, match="too_large"):
        fetch_import("https://api.example.com/x.json",
                     transport=transport)
    transport, _ = fake_transport([(200, b'{"playlists": []}')])
    with pytest.raises(ValueError, match="no_items"):
        fetch_import("https://api.example.com/x.json",
                     transport=transport)


class RecordingMock(MockQlooClient):
    def __init__(self):
        super().__init__()
        self.searched = []

    def search(self, query, limit=5):
        self.searched.append(query)
        return super().search(query, limit=limit)


def test_agent_plan_carries_api_provenance_and_searches_verbatim():
    client = RecordingMock()
    agent = TastePilotAgent(client)
    transport, _ = fake_transport([(200, SPOTIFY)])
    imp = fetch_import(SECRET_URL, transport=transport)
    plan = agent.run("a slow weekend in Kyoto", imported=[imp])
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]
    prov = plan["imports"][0]
    assert prov["source_file"] == "taste.example.com:8443"
    assert prov["format"] == "spotify_api_json"
    assert [i["source"] for i in prov["items"]] == ["spotify_api_json"] * 2
    # The unknown API title is searched verbatim, like a file import.
    assert "An Unknown Song (Someone New)" in client.searched
    assert "SECRETTOKEN" not in json.dumps(plan)


def test_mixed_file_and_api_imports_in_one_plan():
    agent = TastePilotAgent(MockQlooClient())
    file_imp = {"source_file": "watchlist.csv", "format": "letterboxd_csv",
                "items": [{"name": "Spirited Away", "title": "Spirited Away",
                           "creator": None, "source": "letterboxd_csv",
                           "kind": "film"}],
                "seed_names": ["Spirited Away"]}
    transport, _ = fake_transport([(200, SPOTIFY)])
    api_imp = fetch_import("https://api.example.com/Playlist.json",
                           transport=transport)
    plan = agent.run("a slow weekend in Kyoto",
                     imported=[file_imp, api_imp])
    assert plan["checker"]["passed"] and plan["checker"]["chain_valid"]
    assert [i["format"] for i in plan["imports"]] == [
        "letterboxd_csv", "spotify_api_json"]


class _TasteHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (stdlib handler name)
        if self.path.startswith("/Playlist"):
            body = SPOTIFY
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
def taste_server():
    server = HTTPServer(("127.0.0.1", 0), _TasteHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    thread.join(timeout=5)


def test_cli_import_url_end_to_end(taste_server, capsys):
    url = f"{taste_server}/Playlist.json?token=SECRETTOKEN"
    rc = cli.main(["plan", "a slow weekend in Kyoto",
                   "--import-url", url])
    out = capsys.readouterr().out
    assert rc == 0
    assert "spotify_api_json" in out and "2 seed(s)" in out
    assert "SECRETTOKEN" not in out


def test_cli_import_url_failures_exit_2_without_traceback(taste_server,
                                                         capsys):
    rc = cli.main(["plan", "a slow weekend in Kyoto", "--import-url",
                   f"{taste_server}/missing.json"])
    err = capsys.readouterr().err
    assert rc == 2 and "http_error" in err and "Traceback" not in err
    rc = cli.main(["plan", "a slow weekend in Kyoto", "--import-url",
                   "ftp://api.example.com/x.json"])
    err = capsys.readouterr().err
    assert rc == 2 and "unsupported" in err and "Traceback" not in err
    rc = cli.main(["plan", "a slow weekend in Kyoto", "--import-url",
                   "http://127.0.0.1:1/x.json"])
    err = capsys.readouterr().err
    assert rc == 2 and "network_error" in err and "Traceback" not in err


def test_cli_help_and_ui_and_docs_carry_api_wiring(capsys):
    with pytest.raises(SystemExit):
        cli.main(["plan", "--help"])
    out = capsys.readouterr().out
    assert "--import-url" in out
    html = (ROOT / "tastepilot" / "static" / "index.html").read_text()
    assert "Imports:" in html  # plan imports summary renders API sources too
    readme = (ROOT / "README.md").read_text()
    assert "--import-url" in readme and "spotify_api_json" in readme
    submission = (ROOT / "SUBMISSION.md").read_text()
    assert "taste-feed endpoint" in submission

"""External taste-file imports: exported files from the user's own
taste accounts (Spotify JSON, Letterboxd / Goodreads CSV, generic
CSV/JSON/text) are parsed offline into seeds that flow through the
same sealed search -> insights -> checker path, with provenance in
the plan and no path leakage."""
import json

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.cli import main as cli_main
from tastepilot.imports import (MAX_IMPORT_ITEMS, load_import,
                                parse_import_bytes)
from tastepilot.qloo import MockQlooClient

SPOTIFY = json.dumps({"playlists": [{
    "name": "Faves",
    "items": [
        {"track": {"trackName": "Kind of Blue",
                   "artistName": "Miles Davis"}},
        {"track": {"trackName": "An Unknown Song",
                   "artistName": "Someone New"}},
        {"track": {"trackName": "Kind of Blue",
                   "artistName": "Miles Davis"}},
    ]}]}).encode()

LETTERBOXD = b"Date,Name,Year,Letterboxd URI\n2024-01-01,Spirited Away,2001,https://boxd.it/x\n2024-01-02,An Obscure Reel,1999,https://boxd.it/y\n"

GOODREADS = ("Title,Author,My Rating,Bookshelves,Date Read\n"
             "Norwegian Wood,Haruki Murakami,5,to-read,2024/01/01\n"
             "Klara and the Sun,Kazuo Ishiguro,4,read,2024/02/01\n").encode()


def test_spotify_playlist_export_parses_dedupes_and_canonicalises():
    imp = parse_import_bytes(SPOTIFY, filename="Playlist.json")
    assert imp["format"] == "spotify_json"
    assert imp["source_file"] == "Playlist.json"
    assert [i["name"] for i in imp["items"]] == [
        "Kind of Blue (Miles Davis)", "An Unknown Song (Someone New)"]
    assert imp["items"][0]["kind"] == "music"
    # The catalogue title resolves canonically, exactly as a typed seed.
    assert imp["seed_names"][0] == "Kind of Blue (Miles Davis)"
    assert "An Unknown Song (Someone New)" in imp["seed_names"]


def test_letterboxd_csv_uses_bare_film_titles():
    imp = parse_import_bytes(LETTERBOXD, filename="watchlist.csv")
    assert imp["format"] == "letterboxd_csv"
    assert imp["seed_names"] == ["Spirited Away", "An Obscure Reel"]
    assert all(i["kind"] == "film" for i in imp["items"])


def test_goodreads_csv_carries_author_in_parentheses():
    imp = parse_import_bytes(GOODREADS, filename="goodreads_library_export.csv")
    assert imp["format"] == "goodreads_csv"
    assert imp["seed_names"] == ["Norwegian Wood",
                                 "Klara and the Sun"]
    assert imp["items"][0]["creator"] == "Haruki Murakami"
    assert imp["items"][0]["kind"] == "book"


def test_generic_json_list_and_text_list():
    imp = parse_import_bytes(
        json.dumps(["Spirited Away", {"title": "Neuromancer"}]).encode(),
        filename="t.json")
    assert imp["format"] == "generic_json"
    assert imp["seed_names"] == ["Spirited Away", "Neuromancer"]
    txt = parse_import_bytes(
        b"# my list\n- Spirited Away\n2. Parasite\n\n", filename="list.txt")
    assert txt["format"] == "text_list"
    assert txt["seed_names"] == ["Spirited Away", "Parasite"]


def test_malformed_inputs_raise_categorised_value_errors():
    with pytest.raises(ValueError, match="bad_json"):
        parse_import_bytes(b"{not json", filename="x.json")
    with pytest.raises(ValueError, match="no_items"):
        parse_import_bytes(b"[]", filename="x.json")
    with pytest.raises(ValueError, match="no_items"):
        parse_import_bytes(b"Title,Author\n", filename="x.csv")
    with pytest.raises(ValueError, match="undecodable"):
        parse_import_bytes(b"\xff\xfe\x00bad", filename="x.json")
    with pytest.raises(ValueError, match="too_large"):
        parse_import_bytes(b"x" * 1_048_577, filename="x.txt")


def test_item_cap_is_enforced():
    data = json.dumps([f"Title Number {i}" for i in range(500)]).encode()
    imp = parse_import_bytes(data, filename="big.json")
    assert len(imp["items"]) == MAX_IMPORT_ITEMS


def test_agent_plan_carries_import_provenance_and_verifies():
    imp = parse_import_bytes(SPOTIFY, filename="Playlist.json")
    result = TastePilotAgent().run("a slow weekend in Kyoto",
                                   imported=[imp])
    assert result["imports"][0]["format"] == "spotify_json"
    assert result["imports"][0]["source_file"] == "Playlist.json"
    assert "Kind of Blue (Miles Davis)" in result["seeds"]
    assert result["items"] and result["checker"]["passed"] is True
    assert result["checker"]["chain_valid"] is True


def test_imported_unknown_title_is_searched_verbatim():
    searched = []

    class Recording(MockQlooClient):
        def search(self, query, limit=5):
            searched.append(query)
            return super().search(query, limit=limit)

    imp = parse_import_bytes(LETTERBOXD, filename="watchlist.csv")
    TastePilotAgent(Recording()).run("a weekend in Kyoto", imported=[imp])
    assert "An Obscure Reel" in searched


def test_no_import_means_empty_provenance():
    result = TastePilotAgent().run("a slow weekend in Kyoto")
    assert result["imports"] == []


def test_cli_import_file_end_to_end(tmp_path, capsys):
    path = tmp_path / "watchlist.csv"
    path.write_bytes(LETTERBOXD)
    code = cli_main(["plan", "a weekend in Kyoto",
                     "--import-file", str(path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "import: watchlist.csv (letterboxd_csv)" in out


def test_cli_bad_import_exits_2_without_traceback(tmp_path, capsys):
    path = tmp_path / "bad.json"
    path.write_text("{oops", encoding="utf-8")
    code = cli_main(["plan", "a weekend in Kyoto",
                     "--import-file", str(path)])
    err = capsys.readouterr().err
    assert code == 2 and "bad_json" in err and "Traceback" not in err
    missing = cli_main(["plan", "a weekend in Kyoto",
                        "--import-file", str(tmp_path / "nope.csv")])
    assert missing == 2


def test_load_import_reports_basename_only(tmp_path):
    path = tmp_path / "sub" / "list.txt"
    path.parent.mkdir()
    path.write_text("Spirited Away\n", encoding="utf-8")
    imp = load_import(path)
    assert imp["source_file"] == "list.txt"
    assert "sub" not in json.dumps(imp)


def test_ui_carries_import_summary():
    html = (pytest.importorskip("pathlib").Path(__file__).parent.parent
            / "tastepilot" / "static" / "index.html").read_text()
    assert "data.imports" in html and "Imports:" in html

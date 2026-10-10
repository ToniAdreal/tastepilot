"""Oct 13-16 milestone: per-item checker reports, domain-grouped plan
timeline, and the evidence-record viewer endpoint."""
import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.checker import check_item, item_reports
from tastepilot.server import make_handler

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"


def test_item_reports_all_admissible_for_agent_plan():
    result = TastePilotAgent().run(KYOTO)
    reports = item_reports(result["items"], result["evidence"])
    assert len(reports) == len(result["items"])
    for rep in reports:
        assert rep["admissible"] is True
        assert rep["reason"] == "ok"
        assert rep["evidence"]["endpoint"] == "insights"
        assert rep["evidence"]["source"] == "mock"
        assert rep["evidence"]["affinity_in_response"] is not None


def test_check_item_reports_hallucination_reason():
    result = TastePilotAgent().run(KYOTO)
    fake = {"entity": {"id": "invented", "name": "Invented Venue"},
            "domain": "place", "affinity": 0.99,
            "evidence_ref": "mock:insights:deadbeefdeadbeef"}
    rep = check_item(fake, result["evidence"])
    assert rep["admissible"] is False
    assert rep["reason"] == "missing_or_unknown_evidence_ref"
    assert rep["evidence"] is None


def test_check_item_reports_forged_affinity_with_response_value():
    result = TastePilotAgent().run(KYOTO)
    item = dict(result["items"][0])
    item["affinity"] = 0.01
    rep = check_item(item, result["evidence"])
    assert rep["admissible"] is False
    assert rep["reason"] == "affinity_mismatch"
    assert rep["evidence"]["affinity_in_response"] == \
        result["items"][0]["affinity"]


def test_agent_result_groups_items_by_domain_in_plan_order():
    result = TastePilotAgent().run(KYOTO)
    groups = result["groups"]
    assert [g["domain"] for g in groups] == \
        [d for d in result["domains"]
         if any(i["domain"] == d for i in result["items"])]
    grouped = [i for g in groups for i in g["items"]]
    assert [i["entity"]["id"] for i in grouped] == \
        [i["entity"]["id"] for d in result["domains"]
         for i in result["items"] if i["domain"] == d]
    # every admitted item carries its per-item verification, and the
    # checker summary embeds the same reports
    assert all(i["verification"]["admissible"] for i in result["items"])
    assert len(result["checker"]["items"]) == len(result["items"])
    assert result["rejected_details"] == []


@pytest.fixture()
def server_url():
    server = ThreadingHTTPServer(("127.0.0.1", 0),
                                 make_handler(TastePilotAgent()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


def _get(url):
    with urllib.request.urlopen(url) as resp:
        return resp.status, resp.read().decode()


def test_server_plan_and_evidence_viewer_flow(server_url):
    req = urllib.request.Request(
        server_url + "/api/plan", method="POST",
        data=json.dumps({"text": KYOTO}).encode(),
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req) as resp:
        plan = json.loads(resp.read())
    assert plan["groups"], "plan must be grouped for the timeline UI"
    ref = plan["items"][0]["evidence_ref"]
    status, body = _get(server_url + "/api/evidence/" +
                        urllib.parse.quote(ref, safe=""))
    ev = json.loads(body)
    assert status == 200
    assert ev["response_id"] == ref
    assert ev["endpoint"] == "insights"
    assert ev["chain_valid"] is True
    assert ev["record_hash"] == plan["evidence"]["record_hash"]
    assert any(r["entity"]["id"] == plan["items"][0]["entity"]["id"]
               for r in ev["results"])


def test_server_evidence_unknown_id_404s(server_url):
    with pytest.raises(urllib.error.HTTPError) as exc:
        _get(server_url + "/api/evidence/mock:insights:nope")
    assert exc.value.code == 404


def test_ui_page_has_grouping_and_viewer(server_url):
    status, html = _get(server_url + "/")
    assert status == 200
    assert "Evidence record" in html
    assert "data.groups" in html
    assert "verification" in html
    assert "/api/evidence/" in html

"""Bounded suggestion scores: on the live Qloo graph an entity
carries 100+ keyword tags, so the guided-suggestion score's two
non-affinity components must saturate — the coverage bonus caps
at COVERAGE_GAIN_CAP tags and the learned adjustment clamps to
+-LEARNED_ADJUSTMENT_CAP. The 2026-10-13 real-mode run showed
the uncapped formula ranking almost entirely by raw tag count
(top Kyoto candidate: coverage 185, score 9.75, of which 9.25
was the coverage bonus alone, on affinities of 0.5). Raw counts
are still reported in full; only the score components — and the
tag lists spelled out in rationales — are bounded."""
import copy
import json
from pathlib import Path

import pytest

from tastepilot.agent import TastePilotAgent
from tastepilot.swap import (COVERAGE_BONUS, COVERAGE_GAIN_CAP,
                             LEARNED_ADJUSTMENT_CAP,
                             _learned_adjustment, suggest_branches)

APP_ROOT = Path(__file__).resolve().parent.parent
README = APP_ROOT / "README.md"
SUBMISSION = APP_ROOT / "SUBMISSION.md"
REAL_KYOTO = (APP_ROOT.parent / "real-evidence" / "2026-10-13"
              / "plan_kyoto-weekend.json")

KYOTO = "a slow weekend in Kyoto, I love Spirited Away and Kind of Blue"
MUSEUM = "Kyoto National Museum"
MANY_TAGS = [f"tag{i:03d}" for i in range(200)]


def _kyoto():
    return TastePilotAgent().run(KYOTO)


def _many_tagged_plan():
    plan = _kyoto()
    for alt in plan["alternatives"]:
        if (alt.get("entity") or {}).get("name") == MUSEUM:
            alt["entity"]["tags"] = list(MANY_TAGS)
    return plan


def test_coverage_bonus_below_cap_unchanged():
    top = suggest_branches(_kyoto())["candidates"][0]
    assert top["coverage_gain"] == 1
    assert top["coverage_bonus"] == pytest.approx(COVERAGE_BONUS)
    assert top["score"] == pytest.approx(0.73)


def test_coverage_bonus_saturates_but_count_stays_raw():
    result = suggest_branches(_many_tagged_plan())
    cand = next(c for c in result["candidates"]
                if c["alternative"] == MUSEUM)
    assert cand["coverage_gain"] == len(MANY_TAGS)
    assert len(cand["new_tags"]) == len(MANY_TAGS)
    assert cand["coverage_bonus"] == pytest.approx(
        COVERAGE_BONUS * COVERAGE_GAIN_CAP)
    assert cand["score"] == pytest.approx(
        cand["alternative_affinity"] + cand["affinity_delta"]
        + COVERAGE_BONUS * COVERAGE_GAIN_CAP)


def test_learned_adjustment_clamped_both_directions():
    alternative = {"entity": {"tags": MANY_TAGS[:10]},
                   "shared_tags": []}
    profile = {"tag_weights": {t: 0.12 for t in MANY_TAGS[:10]},
               "domain_weights": {}}
    adjustment, matched = _learned_adjustment(
        alternative, "place", profile)
    assert adjustment == pytest.approx(LEARNED_ADJUSTMENT_CAP)
    assert matched == sorted(MANY_TAGS[:10])
    profile["tag_weights"] = {t: -0.12 for t in MANY_TAGS[:10]}
    adjustment, _ = _learned_adjustment(alternative, "place", profile)
    assert adjustment == pytest.approx(-LEARNED_ADJUSTMENT_CAP)


def test_learned_adjustment_below_cap_unchanged():
    alternative = {"entity": {"tags": ["art", "quiet"]},
                   "shared_tags": []}
    profile = {"tag_weights": {"art": 0.12, "quiet": 0.04},
               "domain_weights": {"place": 0.02}}
    adjustment, matched = _learned_adjustment(
        alternative, "place", profile)
    assert adjustment == pytest.approx(0.18)
    assert matched == ["art", "quiet"]


def test_rationale_truncates_long_tag_lists():
    result = suggest_branches(_many_tagged_plan())
    sug = result["suggestions"][0]
    assert "+195 more" in sug["rationale"]
    assert len(sug["rationale"]) < 400


def test_real_evidence_scores_stay_on_affinity_scale():
    if not REAL_KYOTO.exists():
        pytest.skip("real-mode evidence run not present")
    plan = json.loads(REAL_KYOTO.read_text())
    result = suggest_branches(plan)
    assert result["candidates"], "real plan must yield candidates"
    # The live shape is genuinely exercised: raw coverage is
    # in the hundreds, yet every score component is bounded.
    assert max(c["coverage_gain"] for c in result["candidates"]) > 100
    for cand in result["candidates"]:
        assert cand["coverage_bonus"] <= (
            COVERAGE_BONUS * COVERAGE_GAIN_CAP + 1e-9)
        assert abs(cand["learned_adjustment"]) <= (
            LEARNED_ADJUSTMENT_CAP + 1e-9)
        assert cand["score"] <= (
            cand["alternative_affinity"] + cand["affinity_delta"]
            + COVERAGE_BONUS * COVERAGE_GAIN_CAP
            + LEARNED_ADJUSTMENT_CAP + 1e-9)


def test_docs_describe_score_bounds():
    readme = README.read_text()
    assert "coverage bonus saturates" in readme
    assert "LEARNED_ADJUSTMENT_CAP" in readme or "±0.30" in readme
    assert "saturates" in SUBMISSION.read_text()

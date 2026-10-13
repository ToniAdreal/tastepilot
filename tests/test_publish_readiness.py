"""Pre-publish readiness: the artifacts the Oct 27 public repo and the
Oct 29 Devpost submission depend on, plus secret hygiene over every
tracked file. Nothing here talks to the network or needs a key."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# A QLOO_API_KEY assignment carrying a value of 8+ chars. The committed
# .env.example ships an empty value; tests inject keys via variables,
# never as literal assignments — so any hit is a real leak.
KEY_ASSIGNMENT = re.compile(r"QLOO_API_KEY\s*=\s*[\"']?\S{8,}")


def _tracked() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout
    return [ROOT / line for line in out.splitlines() if line]


def test_ci_workflow_runs_suite_and_sample_verification():
    wf = ROOT / ".github" / "workflows" / "tests.yml"
    assert wf.is_file()
    text = wf.read_text(encoding="utf-8")
    assert "pytest" in text
    assert "3.11" in text and "3.12" in text
    # CI re-verifies every shipped sample against its sealed evidence.
    for sample in ("plan_weekend_kyoto", "plan_gift_amelie", "plan_date_nyc"):
        assert sample in text


def test_no_env_file_tracked_and_gitignore_covers_it():
    names = [p.relative_to(ROOT).as_posix() for p in _tracked()]
    assert ".env" not in names
    assert ".env" in (ROOT / ".gitignore").read_text(encoding="utf-8").split()


def test_no_key_material_in_any_tracked_file():
    offenders = []
    for path in _tracked():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if KEY_ASSIGNMENT.search(text):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == [], f"possible key material in: {offenders}"


def test_submission_draft_complete_and_truthful():
    doc = (ROOT / "SUBMISSION.md").read_text(encoding="utf-8")
    for section in (
        "## What it does",
        "## How we built it",
        "## Challenges we ran into",
        "## Accomplishments we're proud of",
        "## What's next for TastePilot",
        "## Built with",
    ):
        assert section in doc, section
    # §8.2 truthful-role wording must survive any later edit of the draft.
    assert "Toni Adreal" in doc
    assert "AI assistance" in doc
    # The draft must not claim results the build has not produced yet:
    # unearned facts stay in explicit FINALIZE slots. The real-mode
    # example and the test count were filled from the verified
    # 2026-10-13 real-mode run; the two remaining slots are the repo
    # URL (Oct 27) and the hosted demo URL (Oct 21-24), both still
    # unearned at draft time.
    assert doc.count("[FINALIZE:") >= 2
    assert KEY_ASSIGNMENT.search(doc) is None


def test_readme_points_at_submission_and_license_is_mit():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "SUBMISSION.md" in readme
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in license_text
    for topic in ("qloo", "taste-ai", "agentic-ai", "evidence-verification"):
        assert topic in readme

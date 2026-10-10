"""LLM adapters: explanations only — never verdicts, never recommendations.

Two implementations behind one interface:
* ``TemplateExplainer`` — deterministic, zero-key (Judge Mode default).
* ``NebiusExplainer`` — calls the Nebius Token Factory connector CLI
  (Nemotron) for richer wording; any failure falls back to the template,
  and the fallback is recorded, never hidden.
Recommendations themselves always come from Qloo responses; the LLM only
writes the "why this fits your taste" line for an already-traced item.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

DEFAULT_MODEL = "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B"
NEBIUS_CLI = Path.home() / "workspace/skills/nebius-token-factory/bin/nebius_tf.py"


class TemplateExplainer:
    source = "template"

    def explain(self, seed_names: list[str], item: dict) -> tuple[str, dict]:
        ent = item["entity"]
        shared = item.get("shared_tags") or []
        seeds = ", ".join(seed_names) if seed_names else "your taste profile"
        if shared:
            why = (f"Qloo's taste graph links {ent['name']} to {seeds} "
                   f"(shared signals: {', '.join(shared)}; affinity {item['affinity']:.2f}).")
        else:
            why = (f"Qloo's taste graph links {ent['name']} to {seeds} "
                   f"with affinity {item['affinity']:.2f}.")
        return why, {"source": self.source, "model": None}


class NebiusExplainer:
    source = "nebius"

    def __init__(self, model: str = DEFAULT_MODEL, cli_path: Path = NEBIUS_CLI,
                 runner=None, timeout: int = 90):
        self.model = model
        self.cli_path = Path(cli_path)
        self._runner = runner  # callable(argv) -> (returncode, stdout) for tests
        self._fallback = TemplateExplainer()

    def explain(self, seed_names: list[str], item: dict) -> tuple[str, dict]:
        ent = item["entity"]
        prompt = (
            "Write ONE short sentence (max 30 words) explaining why this "
            "recommendation fits the user's taste. Use only the facts given; "
            "do not invent venues, facts, or scores.\n"
            f"User likes: {', '.join(seed_names)}\n"
            f"Recommendation: {ent['name']} ({ent['domain']})\n"
            f"Qloo affinity: {item['affinity']:.2f}; shared signals: "
            f"{', '.join(item.get('shared_tags') or []) or 'cross-domain taste correlation'}"
        )
        try:
            argv = ["python3", str(self.cli_path), "chat", "--model", self.model,
                    "--prompt", prompt, "--max-tokens", "300", "--temperature", "0.3"]
            if self._runner is not None:
                rc, out = self._runner(argv)
            else:
                proc = subprocess.run(argv, capture_output=True, text=True,
                                      timeout=self.timeout)
                rc, out = proc.returncode, proc.stdout
            payload = json.loads(out)
            if rc == 0 and payload.get("status") == 200 and payload.get("content"):
                text = str(payload["content"]).strip().splitlines()[0].strip()
                if text:
                    return text, {"source": self.source, "model": self.model,
                                  "usage": payload.get("usage")}
            raise RuntimeError(f"nebius cli rc={rc} status={payload.get('status')}")
        except Exception as exc:  # graceful, recorded fallback
            text, meta = self._fallback.explain(seed_names, item)
            meta = {**meta, "fallback_from": self.source, "fallback_reason": type(exc).__name__}
            return text, meta

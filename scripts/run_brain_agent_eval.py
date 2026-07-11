#!/usr/bin/env python3
"""Verify real Claude and Codex read the same canonical brain freshness marker."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def load_expected(project_id: str, state_path: Path) -> dict[str, str]:
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    record = payload["brains"][project_id]
    if record.get("status") != "fresh":
        raise ValueError(f"brain is not fresh: {project_id}")
    token = str(record["evidence"]["token"])
    return {
        "PROJECT": project_id,
        "BRANCH": str(record["branch"]),
        "COMMIT": str(record["commit"]),
        "TOKEN": token,
    }


def select_project_id(project_id: str | None, state_path: Path) -> str:
    if project_id:
        return project_id
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    candidates = sorted(
        key for key, value in payload.get("brains", {}).items()
        if isinstance(value, dict) and value.get("status") == "fresh"
    )
    if not candidates:
        raise ValueError("no fresh registered brain found; pass --project-id after brain-sync")
    return candidates[0]


def prompt(project_id: str) -> str:
    return f"""Usa esclusivamente il tool MCP LLM Wiki `llm_wiki_read_file`.
Leggi `wiki/memoryhub-freshness.md` nel progetto `{project_id}`.
Non usare file locali, shell o web. Restituisci esattamente:
PROJECT=<project id>
BRANCH=<canonical branch>
COMMIT=<canonical commit completo>
TOKEN=<title MEMORYHUB_FRESH completo>
"""


def parse(output: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for key in ("PROJECT", "BRANCH", "COMMIT", "TOKEN"):
        match = re.search(rf"(?mi)^\s*{key}\s*=\s*([^\r\n`]+)", output)
        if match:
            result[key] = match.group(1).strip()
    return result


def score(output: str, expected: dict[str, str]) -> dict[str, Any]:
    actual = parse(output)
    mismatched = {
        key: {"expected": value, "actual": actual.get(key)}
        for key, value in expected.items() if actual.get(key) != value
    }
    return {
        "passed": not mismatched,
        "score": (len(expected) - len(mismatched)) / len(expected),
        "actual": actual,
        "mismatched": mismatched,
    }


def run_claude(text: str, cwd: Path, budget: float) -> str:
    completed = subprocess.run(
        [
            "claude", "-p", "--no-session-persistence", "--max-budget-usd", str(budget),
            "--output-format", "json", "--allowed-tools",
            "mcp__llm-wiki__llm_wiki_read_file",
        ],
        cwd=cwd, input=text, capture_output=True, text=True, timeout=300,
    )
    payload = json.loads(completed.stdout or "{}")
    if completed.returncode or payload.get("is_error"):
        raise RuntimeError(str(payload.get("result") or completed.stderr or "Claude failed"))
    return str(payload.get("result") or "")


def run_codex(text: str, cwd: Path) -> str:
    output = cwd / "codex-output.txt"
    command = [
        "codex", "exec", "--skip-git-repo-check", "--ephemeral", "--color", "never",
        "--output-last-message", str(output),
    ]
    if os.environ.get("MEMORYHUB_EVAL_CODEX_MODEL"):
        command.extend(["--model", os.environ["MEMORYHUB_EVAL_CODEX_MODEL"]])
    command.append(text)
    completed = subprocess.run(
        command,
        cwd=cwd, capture_output=True, text=True, timeout=360,
    )
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "Codex failed")[-1000:])
    return output.read_text(encoding="utf-8")


def execute(agent: str, text: str, expected: dict[str, str], budget: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"brain-freshness-{agent}-") as temp:
        cwd = Path(temp)
        subprocess.run(["git", "init", "-q"], cwd=cwd, check=True)
        output = run_claude(text, cwd, budget) if agent == "claude" else run_codex(text, cwd)
        return {"agent": agent, "output": output, **score(output, expected)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--project-id", help="Fresh registered brain; defaults to the first one")
    parser.add_argument(
        "--state", type=Path,
        default=Path(os.environ.get("MEMORYHUB_HOME", Path.home() / ".local/share/memoryhub")) / "brain-sync-state.json",
    )
    parser.add_argument("--max-budget-usd", type=float, default=0.30)
    parser.add_argument("--report", type=Path, default=ROOT / "evals" / "latest-brain-agent-report.json")
    args = parser.parse_args()
    project_id = select_project_id(args.project_id, args.state)
    expected = load_expected(project_id, args.state)
    if not args.live:
        print(json.dumps({"mode": "dry-run", "project_id": project_id, "agents": ["claude", "codex"]}, indent=2))
        return 0
    for binary in ("claude", "codex"):
        if not shutil.which(binary):
            parser.error(f"Missing executable: {binary}")
    text = prompt(project_id)
    results = []
    for agent in ("claude", "codex"):
        try:
            results.append(execute(agent, text, expected, args.max_budget_usd))
        except Exception as error:
            results.append({"agent": agent, "passed": False, "score": 0.0, "error": str(error)})
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": all(item.get("passed") for item in results),
        "expected": expected,
        "results": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

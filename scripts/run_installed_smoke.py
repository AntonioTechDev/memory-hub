#!/usr/bin/env python3
"""Smoke-test the globally installed Memory Hub hooks with real agents."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from run_real_agent_eval import ROOT, clean_environment, run_agent, score_output

TOKENS = [
    "INSTALLED-OBJ-731",
    "INSTALLED-STATE-41",
    "INSTALLED-NEXT-09",
    "INSTALLED-BLOCK-552",
    "INSTALLED-DEC-01",
    "INSTALLED-DEC-02",
    "INSTALLED-DEC-03",
    "INSTALLED-DEC-04",
    "INSTALLED-DEC-05",
    "INSTALLED-CHECK",
]


def seed(workspace: Path) -> None:
    binary = Path.home() / ".local" / "bin" / "memoryhub"
    if not binary.exists():
        raise RuntimeError(f"Memory Hub is not installed: {binary}")
    command = [
        str(binary),
        "checkpoint",
        "--actor",
        "installed-smoke",
        "--cwd",
        str(workspace),
        "--status",
        "blocked",
        "--objective",
        f"Validate global install {TOKENS[0]}",
        "--summary",
        f"Global hook state {TOKENS[1]}",
        "--next-action",
        f"Execute {TOKENS[2]}",
        "--blocker",
        TOKENS[3],
        "--validation",
        TOKENS[9],
    ]
    for token in TOKENS[4:9]:
        command.extend(["--decision", token])
    subprocess.run(
        command,
        cwd=workspace,
        env=clean_environment(workspace),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def prompt() -> str:
    return (
        "Senza leggere file, senza web e senza tool, usa esclusivamente la memoria operativa "
        "iniettata globalmente all'avvio. Restituisci obiettivo, stato, prossima azione, "
        "blocker, cinque decisioni e validazione mantenendo letterali tutti i codici INSTALLED."
    )


def execute(target: str, budget: float, keep: bool) -> dict[str, Any]:
    workspace = Path(tempfile.mkdtemp(prefix=f"memoryhub-installed-{target}-"))
    try:
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        seed(workspace)
        output = run_agent(target, prompt(), workspace, budget, source=False)
        return {
            "target": target,
            "workspace": str(workspace) if keep else None,
            "target_output": output,
            **score_output(output, TOKENS),
        }
    finally:
        if not keep:
            shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--max-budget-usd", type=float, default=0.20)
    parser.add_argument(
        "--report",
        type=Path,
        default=ROOT / "evals" / "latest-installed-smoke-report.json",
    )
    args = parser.parse_args()
    if not args.live:
        print(json.dumps({"mode": "dry-run", "targets": ["codex", "claude-code"]}, indent=2))
        return 0
    results = []
    for target in ("codex", "claude-code"):
        try:
            results.append(execute(target, args.max_budget_usd, args.keep))
        except Exception as error:
            results.append({"target": target, "passed": False, "score": 0.0, "error": str(error)})
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": all(result.get("passed") for result in results),
        "results": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

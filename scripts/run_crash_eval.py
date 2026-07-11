#!/usr/bin/env python3
"""Validate ten abrupt-terminal recoveries with fresh real receiving agents."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from run_real_agent_eval import ROOT, clean_environment, prepare_workspace, run_agent, score_output


def source_program(workspace: Path, index: int) -> tuple[str, list[str]]:
    crash_token = f"CRASH-{index:02d}-OBJECTIVE"
    next_token = f"CRASH-{index:02d}-NEXT"
    session = f"crash-source-{index:02d}"
    prompt = (
        f"Ripristinare il task {crash_token}. "
        f"Dopo la diagnosi la prossima azione obbligatoria sarà {next_token}."
    )
    payload_prompt = {
        "event_id": f"crash-prompt-{index:02d}",
        "session_id": session,
        "cwd": str(workspace),
        "prompt": prompt,
    }
    payload_tool = {
        "event_id": f"crash-tool-{index:02d}",
        "session_id": session,
        "cwd": str(workspace),
        "tool_name": "diagnostic",
        "tool_response": f"Diagnosi completata. Eseguire esattamente {next_token}.",
    }
    program = f"""
import time
from memoryhub.core import MemoryStore
store = MemoryStore()
store.capture_hook("user-prompt", "crashed-agent", {payload_prompt!r})
store.capture_hook("tool", "crashed-agent", {payload_tool!r})
print("READY", flush=True)
time.sleep(300)
"""
    return program, [crash_token, next_token]


def create_abrupt_session(workspace: Path, index: int) -> list[str]:
    program, expected = source_program(workspace, index)
    process = subprocess.Popen(
        [sys.executable, "-c", program],
        cwd=workspace,
        env=clean_environment(workspace),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    ready = process.stdout.readline().strip()
    if ready != "READY":
        detail = process.stderr.read() if process.stderr else ""
        process.kill()
        process.wait(timeout=10)
        process.stdout.close()
        if process.stderr:
            process.stderr.close()
        raise RuntimeError(f"crash source did not become ready: {ready} {detail[-1000:]}")
    process.kill()
    return_code = process.wait(timeout=10)
    process.stdout.close()
    if process.stderr:
        process.stderr.close()
    if return_code == 0:
        raise RuntimeError("crash source exited normally; SIGKILL was not observed")

    database = workspace / ".memoryhub-eval" / "memory.db"
    import sqlite3

    with sqlite3.connect(database) as db:
        counts = dict(
            db.execute(
                "SELECT type, COUNT(*) FROM events GROUP BY type"
            ).fetchall()
        )
    if counts.get("user-prompt") != 1 or counts.get("tool") != 1:
        raise RuntimeError(f"progressive events missing after crash: {counts}")
    if counts.get("stop", 0) or counts.get("checkpoint", 0):
        raise RuntimeError(f"crash scenario unexpectedly finalized: {counts}")
    return expected


def receiver_prompt() -> str:
    return (
        "Il terminale dell'agente precedente è stato terminato forzatamente prima di Stop e "
        "checkpoint. Senza leggere file, senza web e senza tool, usa soltanto la memoria "
        "operativa iniettata all'avvio. Restituisci letteralmente il codice CRASH dell'obiettivo "
        "e il codice CRASH della prossima azione."
    )


def execute(index: int, budget: float, keep: bool) -> dict[str, Any]:
    target = "codex" if index % 2 == 0 else "claude-code"
    workspace = Path(tempfile.mkdtemp(prefix=f"memoryhub-crash-{index:02d}-"))
    started = time.monotonic()
    try:
        prepare_workspace(workspace, f"crash-{index:02d}")
        expected = create_abrupt_session(workspace, index)
        output = run_agent(target, receiver_prompt(), workspace, budget, source=False)
        score = score_output(output, expected)
        return {
            "id": f"crash_{index:02d}",
            "target": target,
            "workspace": str(workspace) if keep else None,
            "duration_seconds": round(time.monotonic() - started, 3),
            "target_output": output,
            **score,
        }
    finally:
        if not keep:
            shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--max-budget-usd", type=float, default=0.25)
    parser.add_argument(
        "--report", type=Path, default=ROOT / "evals" / "latest-crash-report.json"
    )
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be positive")
    if not args.live:
        print(
            json.dumps(
                {
                    "mode": "dry-run",
                    "count": args.count,
                    "targets": ["codex" if i % 2 == 0 else "claude-code" for i in range(args.count)],
                },
                indent=2,
            )
        )
        return 0

    results: list[dict[str, Any]] = []
    for index in range(args.count):
        try:
            results.append(execute(index, args.max_budget_usd, args.keep))
        except Exception as error:
            results.append(
                {
                    "id": f"crash_{index:02d}",
                    "target": "codex" if index % 2 == 0 else "claude-code",
                    "passed": False,
                    "score": 0.0,
                    "error": str(error),
                }
            )
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": all(result.get("passed") for result in results),
        "passed_count": sum(bool(result.get("passed")) for result in results),
        "total_count": len(results),
        "results": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

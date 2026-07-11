#!/usr/bin/env python3
"""Abuse-oriented acceptance tests for Memory Hub phases 1 and 2.1."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


class FoolproofEval:
    def __init__(self, events: int, *, keep: bool = False) -> None:
        self.events = events
        self.temp = None if keep else tempfile.TemporaryDirectory(prefix="memoryhub-foolproof-")
        self.base = Path(tempfile.mkdtemp(prefix="memoryhub-foolproof-")) if keep else Path(self.temp.name)
        self.home = self.base / "home"
        self.memory_home = self.home / ".local" / "share" / "memoryhub"
        self.home.mkdir()
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "MEMORYHUB_HOME": str(self.memory_home),
            "PYTHONPATH": str(ROOT),
        }
        self.results: list[dict[str, Any]] = []

    def close(self) -> None:
        if self.temp is not None:
            self.temp.cleanup()

    def run(
        self,
        args: list[str],
        *,
        cwd: Path,
        payload: str | dict[str, Any] | None = None,
        expect: int = 0,
    ) -> subprocess.CompletedProcess[str]:
        input_text = json.dumps(payload) if isinstance(payload, dict) else payload
        completed = subprocess.run(
            [sys.executable, "-m", "memoryhub", *args], cwd=cwd, env=self.env,
            input=input_text, capture_output=True, text=True, timeout=60,
        )
        if completed.returncode != expect:
            raise AssertionError(
                f"exit {completed.returncode}, expected {expect}: {' '.join(args)}\n"
                f"{(completed.stderr or completed.stdout)[-1000:]}"
            )
        return completed

    def check(self, name: str, callback: Any) -> None:
        try:
            evidence = callback()
            self.results.append({"name": name, "passed": True, "evidence": evidence})
        except Exception as error:
            for suffix in ("", "-wal", "-shm"):
                source = self.memory_home / f"memory.db{suffix}"
                if source.exists():
                    try:
                        shutil.copy2(source, self.base / f"failure-memory.db{suffix}")
                    except OSError:
                        pass
            self.results.append({
                "name": name, "passed": False, "error": str(error),
                "traceback": traceback.format_exc(limit=8),
            })

    def database(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.memory_home / "memory.db", timeout=10)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

    def phase1(self) -> None:
        workspace = self.base / "workspace"
        workspace.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)

        def malformed_inputs() -> str:
            self.run(["hook", "--event", "user-prompt", "--actor", "codex"], cwd=workspace, payload="{totally broken")
            self.run(["hook", "--event", "tool", "--actor", "claude-code"], cwd=workspace, payload="")
            return "malformed and empty stdin accepted without traceback"

        def duplicates_and_crash() -> str:
            prompt = {
                "event_id": "fool-duplicate", "session_id": "crashed-session",
                "cwd": str(workspace), "prompt": "Recover FOOL-CRASH-OBJECTIVE",
            }
            for _ in range(100):
                self.run(["hook", "--event", "user-prompt", "--actor", "claude-code"], cwd=workspace, payload=prompt)
            # No Stop event: a new provider must still recover the prompt.
            context = self.run(
                ["hook", "--event", "session-start", "--actor", "codex"], cwd=workspace,
                payload={"thread-id": "fresh-codex", "cwd": str(workspace)},
            ).stdout
            if "FOOL-CRASH-OBJECTIVE" not in context:
                raise AssertionError("fresh provider missed crash objective")
            with self.database() as db:
                count = db.execute("SELECT COUNT(*) FROM events WHERE dedupe_key='claude-code:fool-duplicate'").fetchone()[0]
            if count != 1:
                raise AssertionError(f"duplicate event count={count}")
            return "100 duplicate submissions -> one event; crash handoff recovered"

        def secrets_and_bounds() -> str:
            secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
            huge = "X" * 200_000
            self.run(
                ["hook", "--event", "tool", "--actor", "codex"], cwd=workspace,
                payload={
                    "event_id": "huge-secret", "session_id": "crashed-session",
                    "cwd": str(workspace), "tool_output": f"token={secret} {huge}",
                },
            )
            raw = (self.memory_home / "memory.db").read_bytes()
            if secret.encode() in raw:
                raise AssertionError("raw token persisted")
            with self.database() as db:
                largest = db.execute("SELECT MAX(length(content_text)) FROM events").fetchone()[0]
                integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
            if largest > 16_000 or integrity != "ok":
                raise AssertionError(f"largest={largest}, integrity={integrity}")
            return f"secret absent; max event={largest}; integrity={integrity}"

        def workspace_isolation() -> str:
            other = self.base / "wrong-customer"
            other.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=other, check=True)
            self.run(
                ["hook", "--event", "user-prompt", "--actor", "codex"], cwd=other,
                payload={"event_id": "other", "session_id": "other", "cwd": str(other), "prompt": "OTHER-CUSTOMER-CANARY"},
            )
            context = self.run(["context", "--cwd", str(workspace)], cwd=workspace).stdout
            if "OTHER-CUSTOMER-CANARY" in context:
                raise AssertionError("cross-workspace leak")
            return "wrong customer canary absent from active workspace"

        def bad_handoff_is_rejected() -> str:
            result = self.run(
                ["checkpoint", "--actor", "codex", "--status", "blocked", "--summary", "forgot next"],
                cwd=workspace, expect=2,
            )
            if "next action" not in result.stderr:
                raise AssertionError("missing actionable error")
            result = self.run(["resume", "task_does_not_exist"], cwd=workspace, expect=2)
            if "not found" not in result.stderr:
                raise AssertionError("missing task-not-found error")
            return "missing next action and invalid task rejected cleanly"

        def repeated_install() -> str:
            for _ in range(3):
                completed = subprocess.run(
                    [str(ROOT / "install.sh"), "--target-home", str(self.home), "--skip-agent-commands", "--json"],
                    cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=60,
                )
                if completed.returncode:
                    raise AssertionError(completed.stderr)
            claude = json.loads((self.home / ".claude" / "settings.json").read_text())
            groups = claude["hooks"]["SessionStart"]
            hooks = [handler for group in groups for handler in group["hooks"] if "memoryhub" in handler.get("command", "")]
            agents = (self.home / ".codex" / "AGENTS.md").read_text()
            if len(hooks) != 1 or agents.count("<!-- memoryhub:managed:start -->") != 1:
                raise AssertionError(f"hooks={len(hooks)}, instruction blocks={agents.count('memoryhub:managed:start')}")
            doctor = self.run(["doctor", "--target-home", str(self.home)], cwd=workspace)
            if "OK SQLite integrity" not in doctor.stdout:
                raise AssertionError("installed doctor did not confirm integrity")
            return "three installs -> one hook and one instruction block; runtime doctor green"

        def chat_storm() -> str:
            for index in range(self.events):
                actor = "codex" if index % 2 else "claude-code"
                self.run(
                    ["hook", "--event", "tool", "--actor", actor], cwd=workspace,
                    payload={
                        "event_id": f"storm-{index}", "session_id": f"chat-{index % 17}",
                        "cwd": str(workspace), "output": f"storm evidence {index}",
                    },
                )
            history = self.run(
                ["history", "--limit", str(self.events + 30)], cwd=workspace
            ).stdout
            stored = sum(f"storm evidence {index}" in history for index in range(self.events))
            doctor = self.run(["doctor", "--target-home", str(self.home)], cwd=workspace)
            if stored != self.events or "OK SQLite integrity" not in doctor.stdout:
                raise AssertionError(f"stored={stored}/{self.events}, doctor={doctor.stdout[-300:]}")
            return f"{stored}/{self.events} alternating chat events durable; runtime doctor green"

        for name, callback in (
            ("P1 malformed/empty hook payloads", malformed_inputs),
            ("P1 duplicate clicks + terminal crash", duplicates_and_crash),
            ("P1 huge payload + secret canary", secrets_and_bounds),
            ("P1 wrong customer workspace", workspace_isolation),
            ("P1 incomplete/invalid handoff", bad_handoff_is_rejected),
            ("P1 triple installer click", repeated_install),
            ("P1 alternating chat storm", chat_storm),
        ):
            self.check(name, callback)

    def phase2(self) -> None:
        def brain_suite() -> str:
            completed = subprocess.run(
                [sys.executable, "-m", "unittest", "tests.test_brain_sync", "-v"],
                cwd=ROOT, env={**self.env, "PYTHONWARNINGS": "error::ResourceWarning"},
                capture_output=True, text=True, timeout=180,
            )
            if completed.returncode:
                raise AssertionError((completed.stderr or completed.stdout)[-3000:])
            count = completed.stderr.count(" ... ok") + completed.stdout.count(" ... ok")
            return f"{count} branch/merge/secret/concurrency scenarios passed"

        def unsafe_remote_rejected() -> str:
            completed = subprocess.run(
                [sys.executable, "-m", "memoryhub", "brain-doctor", "--api-url", "http://example.com:19828"],
                cwd=ROOT, env=self.env, capture_output=True, text=True, timeout=30,
            )
            if completed.returncode != 2 or "must use local HTTP" not in completed.stderr:
                raise AssertionError(f"exit={completed.returncode}: {completed.stderr}")
            return "remote LLM Wiki endpoint rejected before connection"

        self.check("P2 canonical-only + huge refactor abuse", brain_suite)
        self.check("P2 remote endpoint typo", unsafe_remote_rejected)

    def report(self) -> dict[str, Any]:
        phase1 = [item for item in self.results if item["name"].startswith("P1")]
        phase2 = [item for item in self.results if item["name"].startswith("P2")]
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "passed": all(item["passed"] for item in self.results),
            "summary": {
                "total": len(self.results),
                "passed": sum(item["passed"] for item in self.results),
                "phase1": f"{sum(item['passed'] for item in phase1)}/{len(phase1)}",
                "phase2": f"{sum(item['passed'] for item in phase2)}/{len(phase2)}",
            },
            "results": self.results,
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=250)
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--report", type=Path, default=ROOT / "evals" / "latest-foolproof-report.json")
    args = parser.parse_args()
    if args.events < 1:
        parser.error("--events must be positive")
    evaluation = FoolproofEval(args.events, keep=args.keep)
    try:
        evaluation.phase1()
        evaluation.phase2()
        report = evaluation.report()
    finally:
        if args.keep:
            print(f"Kept evaluation workspace: {evaluation.base}", file=sys.stderr)
        else:
            evaluation.close()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

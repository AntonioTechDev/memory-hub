from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "agent_memory.py"


class AgentMemoryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".agent-memory").mkdir()
        self.cli("init", "--project-id", "phoenix", "--force")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def cli(
        self,
        *args: str,
        payload: dict | None = None,
        check: bool = True,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.root,
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            check=check,
            env={**os.environ, **(env or {})},
        )

    def state(self) -> dict:
        return json.loads((self.root / ".agent-memory" / "state.json").read_text())

    def test_claude_to_codex_handoff_preserves_operational_facts(self) -> None:
        args = [
            "checkpoint", "--actor", "claude-code", "--status", "in_progress",
            "--objective", "Deploy Phoenix API", "--summary", "systemd unit created; health endpoint pending",
            "--next-action", "Run curl http://127.0.0.1:8420/health",
        ]
        facts = [f"decision-{index}" for index in range(1, 11)]
        for fact in facts:
            args.extend(["--decision", fact])
        self.cli(*args)
        context = self.cli("hook", "--event", "session-start", "--actor", "codex", payload={"thread-id": "c-1"}).stdout
        self.assertIn("Deploy Phoenix API", context)
        self.assertIn("Run curl http://127.0.0.1:8420/health", context)
        for fact in facts:
            self.assertIn(fact, context)

    def test_codex_to_claude_handoff(self) -> None:
        self.cli(
            "checkpoint", "--actor", "codex", "--status", "blocked",
            "--summary", "Migration applied in staging", "--next-action", "Request production approval",
            "--blocker", "Missing change ticket",
        )
        context = self.cli("hook", "--event", "session-start", "--actor", "claude-code", payload={"session_id": "a-1"}).stdout
        self.assertIn("Migration applied in staging", context)
        self.assertIn("Missing change ticket", context)

    def test_terminal_crash_marks_checkpoint_stale_and_keeps_last_response(self) -> None:
        self.cli("checkpoint", "--actor", "claude-code", "--status", "in_progress", "--next-action", "Run smoke test")
        self.cli(
            "hook", "--event", "stop", "--actor", "claude-code",
            payload={"last_assistant_message": "Unit installed; terminal died before smoke test."},
        )
        handoff = (self.root / ".agent-memory" / "HANDOFF.md").read_text()
        self.assertIn("possibly stale", handoff)
        self.assertIn("terminal died before smoke test", handoff)

    def test_secrets_are_redacted_from_state_and_history(self) -> None:
        self.cli(
            "checkpoint", "--actor", "codex", "--summary", "Used api_key=super-secret-value",
            "--next-action", "Rotate sk-abcdefghijklmnopqrstuvwxyz123456",
        )
        self.cli(
            "hook", "--event", "user-prompt", "--actor", "codex",
            payload={"prompt": "Authorization: Bearer abcdefghijklmnop"},
        )
        combined = (self.root / ".agent-memory" / "state.json").read_text() + (self.root / ".agent-memory" / "private" / "events.jsonl").read_text()
        self.assertNotIn("super-secret-value", combined)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz123456", combined)
        self.assertNotIn("abcdefghijklmnop", combined)
        self.assertIn("REDACTED", combined)

    def test_projects_are_isolated(self) -> None:
        other = Path(tempfile.mkdtemp())
        try:
            (other / ".agent-memory").mkdir()
            subprocess.run([sys.executable, str(SCRIPT), "init", "--project-id", "other", "--force"], cwd=other, check=True, capture_output=True, text=True)
            self.cli("checkpoint", "--actor", "codex", "--summary", "phoenix-only")
            other_state = json.loads((other / ".agent-memory" / "state.json").read_text())
            self.assertEqual("other", other_state["project_id"])
            self.assertNotIn("phoenix-only", json.dumps(other_state))
        finally:
            import shutil
            shutil.rmtree(other)

    def test_concurrent_checkpoints_are_atomic(self) -> None:
        def write(index: int) -> None:
            self.cli("checkpoint", "--actor", f"agent-{index}", "--summary", f"write-{index}")

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(write, range(100)))
        state = self.state()
        self.assertEqual(100, state["revision"])
        self.assertRegex(state["task"]["summary"], r"write-\d+")

    def test_validation_rejects_missing_next_action(self) -> None:
        self.cli("checkpoint", "--actor", "codex", "--status", "in_progress", "--summary", "working")
        result = self.cli("validate", check=False)
        self.assertEqual(1, result.returncode)
        self.assertIn("no next_action", result.stderr)

    def test_clear_then_replace_blockers(self) -> None:
        self.cli("checkpoint", "--actor", "codex", "--blocker", "old")
        self.cli("checkpoint", "--actor", "codex", "--clear-blockers", "--blocker", "new")
        self.assertEqual(["new"], self.state()["task"]["blockers"])


if __name__ == "__main__":
    unittest.main()

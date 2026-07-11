from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from memoryhub.claude_worker import WorkspaceBusyError, run_delegation, workspace_lock
from memoryhub.core import workspace_identity


FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

prompt = sys.stdin.read()
mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
if mode == "success":
    target = Path("src/worker.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("implemented by worker\n", encoding="utf-8")
    print(json.dumps({"structured_output": {
        "status": "done",
        "summary": "Implemented the fixture",
        "next_action": "Review the diff",
        "files": ["src/worker.txt"],
        "validations": ["fixture validation passed"],
        "blockers": []
    }}))
elif mode == "outside":
    Path("outside.txt").write_text("outside scope\n", encoding="utf-8")
    print(json.dumps({"structured_output": {
        "status": "done", "summary": "Changed outside", "next_action": "Review",
        "files": ["outside.txt"], "validations": [], "blockers": []
    }}))
elif mode == "malformed":
    print("not-json")
elif mode == "failed":
    print("worker failed", file=sys.stderr)
    raise SystemExit(7)
elif mode == "timeout":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(3600)"
    child = subprocess.Popen([sys.executable, "-c", child_code])
    Path(os.environ["FAKE_CHILD_PID_FILE"]).write_text(str(child.pid), encoding="utf-8")
    while True:
        time.sleep(1)
elif mode == "background":
    child_code = "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(3600)"
    child = subprocess.Popen([sys.executable, "-c", child_code])
    Path(os.environ["FAKE_CHILD_PID_FILE"]).write_text(str(child.pid), encoding="utf-8")
    print(json.dumps({"structured_output": {
        "status": "done", "summary": "Spawned forbidden child", "next_action": "Review",
        "files": [], "validations": [], "blockers": []
    }}))
'''


class ClaudeWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.repo = self.base / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("fixture\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=self.repo, check=True)
        self.fake = self.base / "fake-claude"
        self.fake.write_text(FAKE_CLAUDE, encoding="utf-8")
        self.fake.chmod(0o755)
        self.memory_home = self.home / ".local" / "share" / "memoryhub"
        self.env = {
            "HOME": str(self.home),
            "MEMORYHUB_HOME": str(self.memory_home),
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_worker(self, mode: str = "success", **kwargs: object) -> dict:
        with patch.dict(os.environ, {**self.env, "FAKE_CLAUDE_MODE": mode}, clear=False):
            return run_delegation(
                objective="Implement the fixture",
                cwd=self.repo,
                claude_binary=str(self.fake),
                timeout_seconds=int(kwargs.pop("timeout_seconds", 10)),
                grace_seconds=float(kwargs.pop("grace_seconds", 0.2)),
                **kwargs,
            )

    def test_success_is_structured_checkpointed_and_scoped(self) -> None:
        result = self.run_worker(
            allowed_paths=["src"], validations=["fixture validation passed"]
        )
        self.assertEqual("success", result["status"])
        self.assertEqual(["src/worker.txt"], result["changed_paths"])
        self.assertEqual([], result["scope_violations"])
        self.assertTrue(result["cleanup"]["reaped"])
        self.assertTrue(Path(result["log_path"]).is_file())
        self.assertEqual(0o600, Path(result["log_path"]).stat().st_mode & 0o777)

    def test_malformed_output_fails_closed(self) -> None:
        result = self.run_worker("malformed")
        self.assertEqual("invalid-output", result["status"])
        self.assertIn("not JSON", result["error"])

    def test_nonzero_exit_is_reported(self) -> None:
        result = self.run_worker("failed")
        self.assertEqual("failed", result["status"])
        self.assertEqual(7, result["returncode"])

    def test_scope_violation_is_not_accepted(self) -> None:
        result = self.run_worker("outside", allowed_paths=["src"])
        self.assertEqual("scope-violation", result["status"])
        self.assertEqual(["outside.txt"], result["scope_violations"])

    def test_timeout_kills_term_ignoring_process_group(self) -> None:
        pid_file = self.base / "child.pid"
        with patch.dict(
            os.environ,
            {**self.env, "FAKE_CLAUDE_MODE": "timeout", "FAKE_CHILD_PID_FILE": str(pid_file)},
            clear=False,
        ):
            started = time.monotonic()
            result = run_delegation(
                objective="Never finish",
                cwd=self.repo,
                claude_binary=str(self.fake),
                timeout_seconds=1,
                grace_seconds=0.2,
            )
        elapsed = time.monotonic() - started
        self.assertEqual("timeout", result["status"])
        self.assertLess(elapsed, 4)
        self.assertTrue(result["cleanup"]["term_sent"])
        self.assertTrue(result["cleanup"]["kill_sent"])
        self.assertTrue(result["cleanup"]["reaped"])
        self.assertFalse(result["cleanup"]["group_alive"])
        child_pid = int(pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 2
        while Path(f"/proc/{child_pid}").exists() and time.monotonic() < deadline:
            stat = Path(f"/proc/{child_pid}/stat")
            if stat.exists() and stat.read_text(encoding="utf-8").split()[2] == "Z":
                break
            time.sleep(0.05)
        if Path(f"/proc/{child_pid}/stat").exists():
            self.assertEqual("Z", Path(f"/proc/{child_pid}/stat").read_text().split()[2])

    def test_workspace_lock_rejects_a_second_worker(self) -> None:
        workspace_id = workspace_identity(self.repo)["id"]
        with patch.dict(os.environ, self.env, clear=False):
            with workspace_lock(workspace_id):
                with self.assertRaises(WorkspaceBusyError):
                    self.run_worker()

    def test_successful_parent_cannot_leave_a_background_child(self) -> None:
        pid_file = self.base / "background-child.pid"
        with patch.dict(
            os.environ,
            {**self.env, "FAKE_CLAUDE_MODE": "background", "FAKE_CHILD_PID_FILE": str(pid_file)},
            clear=False,
        ):
            result = run_delegation(
                objective="Try to leak a child",
                cwd=self.repo,
                claude_binary=str(self.fake),
                timeout_seconds=5,
                grace_seconds=0.2,
            )
        self.assertEqual("success", result["status"])
        self.assertTrue(result["cleanup"]["term_sent"])
        self.assertFalse(result["cleanup"]["group_alive"])

    def test_dry_run_never_launches_claude(self) -> None:
        result = self.run_worker("success", dry_run=True)
        self.assertEqual("dry-run", result["status"])
        self.assertFalse((self.repo / "src" / "worker.txt").exists())


if __name__ == "__main__":
    unittest.main()

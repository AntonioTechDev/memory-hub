from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from memoryhub.core import MemoryStore
from memoryhub.install import END_MARKER, START_MARKER, install
from memoryhub.mcp_server import handle

ROOT = Path(__file__).resolve().parents[1]


class MemoryHubTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.workspace = self.base / "workspace"
        self.memory_home = self.home / ".local" / "share" / "memoryhub"
        self.home.mkdir()
        self.workspace.mkdir()
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "MEMORYHUB_HOME": str(self.memory_home),
            "PYTHONPATH": str(ROOT),
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def cli(
        self,
        *args: str,
        cwd: Path | None = None,
        payload: dict | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "memoryhub", *args],
            cwd=cwd or self.workspace,
            env=self.env,
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            check=check,
        )

    def db(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.memory_home / "memory.db")
        connection.row_factory = sqlite3.Row
        return connection

    def test_init_creates_private_local_sqlite(self) -> None:
        result = self.cli("init")
        db_path = self.memory_home / "memory.db"
        self.assertIn(str(db_path), result.stdout)
        self.assertEqual(0o600, db_path.stat().st_mode & 0o777)
        with self.db() as db:
            self.assertEqual("2", db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0])

    def test_suppressed_worker_hook_is_a_no_op(self) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "memoryhub", "hook", "--event", "session-start", "--actor", "codex"],
            cwd=self.workspace,
            env={**self.env, "MEMORYHUB_SUPPRESS_HOOKS": "1"},
            input=json.dumps({"thread-id": "nested", "cwd": str(self.workspace)}),
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual("", result.stdout)
        self.assertFalse((self.memory_home / "memory.db").exists())

    def test_claude_to_codex_checkpoint_uses_same_global_task(self) -> None:
        self.cli(
            "checkpoint",
            "--actor", "claude-code",
            "--objective", "Deploy Phoenix",
            "--summary", "Unit installed",
            "--next-action", "Run the smoke test",
            "--decision", "Use systemd",
            "--file", "deploy/phoenix.service",
            "--validation", "12 tests passed",
        )
        context = self.cli(
            "hook", "--event", "session-start", "--actor", "codex",
            payload={"thread-id": "codex-1", "cwd": str(self.workspace)},
        ).stdout
        self.assertIn("Deploy Phoenix", context)
        self.assertIn("Run the smoke test", context)
        self.assertIn("Use systemd", context)
        self.assertIn("deploy/phoenix.service", context)
        self.assertIn("12 tests passed", context)

    def test_codex_to_claude_checkpoint(self) -> None:
        self.cli(
            "checkpoint",
            "--actor", "codex",
            "--summary", "Migration staged",
            "--status", "blocked",
            "--next-action", "Renew staging access",
            "--blocker", "Credential expired",
        )
        context = self.cli(
            "hook", "--event", "session-start", "--actor", "claude-code",
            payload={"session_id": "claude-1", "cwd": str(self.workspace)},
        ).stdout
        self.assertIn("Migration staged", context)
        self.assertIn("Credential expired", context)
        self.assertIn("Renew staging access", context)

    def test_prompt_creates_task_and_stop_survives_terminal_crash(self) -> None:
        self.cli(
            "hook", "--event", "user-prompt", "--actor", "claude-code",
            payload={
                "event_id": "prompt-1",
                "session_id": "crash-session",
                "cwd": str(self.workspace),
                "prompt": "Repair the corrupted terminal workflow",
            },
        )
        self.cli(
            "hook", "--event", "stop", "--actor", "claude-code",
            payload={
                "event_id": "stop-1",
                "session_id": "crash-session",
                "cwd": str(self.workspace),
                "last_assistant_message": "Service restored; validation still pending.",
            },
        )
        context = self.cli("context", "--cwd", str(self.workspace)).stdout
        self.assertIn("Repair the corrupted terminal workflow", context)
        self.assertIn("Service restored; validation still pending", context)

    def test_explicit_hook_event_id_is_idempotent(self) -> None:
        payload = {
            "event_id": "same-event",
            "session_id": "s1",
            "cwd": str(self.workspace),
            "prompt": "Do the task",
        }
        self.cli("hook", "--event", "user-prompt", "--actor", "codex", payload=payload)
        self.cli("hook", "--event", "user-prompt", "--actor", "codex", payload=payload)
        with self.db() as db:
            count = db.execute("SELECT COUNT(*) FROM events WHERE dedupe_key='codex:same-event'").fetchone()[0]
        self.assertEqual(1, count)

    def test_secrets_are_redacted_before_sqlite_persistence(self) -> None:
        self.cli(
            "hook", "--event", "user-prompt", "--actor", "codex",
            payload={
                "session_id": "secret-session",
                "cwd": str(self.workspace),
                "prompt": "Use api_key=super-secret-value and Authorization: Bearer abcdefghijklmnop",
            },
        )
        with self.db() as db:
            text = "\n".join(
                str(value)
                for row in db.execute("SELECT content_text, content_json FROM events")
                for value in row
            )
            objective = db.execute("SELECT objective FROM tasks").fetchone()[0]
        combined = text + objective
        self.assertNotIn("super-secret-value", combined)
        self.assertNotIn("abcdefghijklmnop", combined)
        self.assertIn("REDACTED", combined)

    def test_workspaces_are_logically_isolated_but_globally_listable(self) -> None:
        other = self.base / "other"
        other.mkdir()
        self.cli("checkpoint", "--actor", "codex", "--summary", "workspace-one")
        self.cli(
            "checkpoint", "--actor", "claude-code", "--summary", "workspace-two",
            cwd=other,
        )
        local_context = self.cli("context", cwd=other).stdout
        self.assertIn("workspace-two", local_context)
        self.assertNotIn("workspace-one", local_context)
        all_tasks = self.cli("tasks", "--all", "--json", cwd=other).stdout
        self.assertIn("workspace-one", all_tasks)
        self.assertIn("workspace-two", all_tasks)

    def test_activity_shows_active_ended_and_stale_sessions(self) -> None:
        self.cli(
            "hook", "--event", "user-prompt", "--actor", "codex",
            payload={
                "event_id": "activity-prompt-1",
                "session_id": "activity-codex",
                "cwd": str(self.workspace),
                "prompt": "Implement activity dashboard",
            },
        )
        task_id = self.cli(
            "checkpoint", "--actor", "codex", "--session-id", "activity-codex",
            "--summary", "Activity dashboard staged",
            "--next-action", "Run activity tests",
        ).stdout.rsplit(" ", 1)[-1].strip()
        self.cli(
            "hook", "--event", "user-prompt", "--actor", "claude-code",
            payload={
                "event_id": "activity-prompt-2",
                "session_id": "activity-claude",
                "cwd": str(self.workspace),
                "prompt": "Review dashboard output",
            },
        )
        self.cli(
            "hook", "--event", "stop", "--actor", "claude-code",
            payload={
                "event_id": "activity-stop-2",
                "session_id": "activity-claude",
                "cwd": str(self.workspace),
                "last_assistant_message": "Review complete",
            },
        )

        rows = json.loads(self.cli("activity", "--json").stdout)
        by_session = {row["session_id"]: row for row in rows}
        self.assertEqual("active", by_session["activity-codex"]["state"])
        self.assertEqual(task_id, by_session["activity-codex"]["task_id"])
        self.assertEqual("Run activity tests", by_session["activity-codex"]["next_action"])
        self.assertEqual("ended", by_session["activity-claude"]["state"])

        old = "2020-01-01T00:00:00.000+00:00"
        with self.db() as db:
            db.execute(
                "UPDATE sessions SET last_event_at=? WHERE id=? AND actor=?",
                (old, "activity-codex", "codex"),
            )
        stale = json.loads(self.cli("activity", "--json", "--stale-after", "1d").stdout)
        stale_by_session = {row["session_id"]: row for row in stale}
        self.assertEqual("stale", stale_by_session["activity-codex"]["state"])

    def test_timeline_is_chronological_and_filterable(self) -> None:
        self.cli(
            "hook", "--event", "user-prompt", "--actor", "codex",
            payload={
                "event_id": "timeline-prompt",
                "session_id": "timeline-codex",
                "cwd": str(self.workspace),
                "prompt": "Timeline prompt canary",
            },
        )
        self.cli(
            "hook", "--event", "tool", "--actor", "codex",
            payload={
                "event_id": "timeline-tool",
                "session_id": "timeline-codex",
                "cwd": str(self.workspace),
                "tool_name": "pytest",
                "tool_response": "Timeline tool canary",
            },
        )
        rows = json.loads(
            self.cli("timeline", "--agent", "codex", "--limit", "5", "--json").stdout
        )
        texts = [row["content_text"] for row in rows]
        self.assertIn("Timeline prompt canary", texts)
        self.assertIn("pytest: Timeline tool canary", texts)
        self.assertLess(
            texts.index("Timeline prompt canary"),
            texts.index("pytest: Timeline tool canary"),
        )
        for row in rows:
            self.assertEqual("codex", row["actor"])

    def test_cleanup_dry_run_reports_stale_sessions_and_dirty_tasks(self) -> None:
        self.cli(
            "hook", "--event", "user-prompt", "--actor", "codex",
            payload={
                "event_id": "cleanup-prompt",
                "session_id": "cleanup-session",
                "cwd": str(self.workspace),
                "prompt": "Cleanup stale canary",
            },
        )
        old = "2020-01-01T00:00:00.000+00:00"
        with self.db() as db:
            db.execute(
                "UPDATE sessions SET last_event_at=? WHERE id=? AND actor=?",
                (old, "cleanup-session", "codex"),
            )
            db.execute(
                "UPDATE tasks SET updated_at=? WHERE title=?",
                (old, "Cleanup stale canary"),
            )

        report = json.loads(self.cli("cleanup", "--dry-run", "--stale", "10d", "--json").stdout)
        self.assertEqual("dry-run", report["mode"])
        self.assertGreaterEqual(report["counts"]["stale_sessions"], 1)
        self.assertGreaterEqual(report["counts"]["stale_tasks"], 1)
        self.assertGreaterEqual(report["counts"]["missing_next_action"], 1)
        self.assertTrue(
            any(item["session_id"] == "cleanup-session" for item in report["stale_sessions"])
        )

    def test_foolish_cross_workspace_checkpoint_cannot_mutate_another_task(self) -> None:
        other = self.base / "other-cross"
        other.mkdir()
        created = self.cli(
            "checkpoint", "--actor", "codex", "--summary", "workspace-one"
        ).stdout.rsplit(" ", 1)[-1].strip()
        result = self.cli(
            "checkpoint", "--actor", "claude-code", "--task-id", created,
            "--summary", "wrong workspace mutation", cwd=other, check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("different workspace", result.stderr)
        self.assertNotIn("wrong workspace mutation", self.cli("context").stdout)

    def test_explicit_status_without_next_action_is_rejected(self) -> None:
        result = self.cli(
            "checkpoint", "--actor", "codex", "--status", "blocked",
            "--summary", "I forgot what comes next", check=False,
        )
        self.assertEqual(2, result.returncode)
        self.assertIn("requires a concrete next action", result.stderr)

    def test_git_remote_keeps_workspace_identity_after_move(self) -> None:
        first = self.base / "repo-one"
        second = self.base / "repo-two"
        for repo in (first, second):
            repo.mkdir()
            subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
            subprocess.run(
                ["git", "remote", "add", "origin", "git@example.test:acme/project.git"],
                cwd=repo,
                check=True,
            )
        self.cli("checkpoint", "--actor", "codex", "--summary", "portable workspace", cwd=first)
        context = self.cli("context", cwd=second).stdout
        self.assertIn("portable workspace", context)

    def test_git_remote_credentials_are_not_persisted(self) -> None:
        repo = self.base / "secret-remote"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            [
                "git", "remote", "add", "origin",
                "https://service-user:remote-password-canary@example.test/acme/project.git",
            ],
            cwd=repo,
            check=True,
        )
        self.cli("checkpoint", "--actor", "codex", "--summary", "remote test", cwd=repo)
        with self.db() as db:
            remote = db.execute("SELECT git_remote FROM workspaces").fetchone()[0]
        self.assertNotIn("remote-password-canary", remote)
        self.assertIn("REDACTED", remote)

    def test_concurrent_events_are_durable(self) -> None:
        def capture(index: int) -> None:
            self.cli(
                "hook", "--event", "user-prompt", "--actor", "codex",
                payload={
                    "event_id": f"event-{index}",
                    "session_id": "concurrent",
                    "cwd": str(self.workspace),
                    "prompt": f"event number {index}",
                },
            )

        with ThreadPoolExecutor(max_workers=12) as pool:
            list(pool.map(capture, range(100)))
        with self.db() as db:
            count = db.execute("SELECT COUNT(*) FROM events WHERE type='user-prompt'").fetchone()[0]
            integrity = db.execute("PRAGMA integrity_check").fetchone()[0]
        self.assertEqual(100, count)
        self.assertEqual("ok", integrity)

    def test_mcp_lists_tools_and_returns_context(self) -> None:
        self.cli("checkpoint", "--actor", "codex", "--summary", "MCP-visible state")
        previous = os.environ.get("MEMORYHUB_HOME")
        os.environ["MEMORYHUB_HOME"] = str(self.memory_home)
        try:
            listed = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            called = handle(
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/call",
                    "params": {
                        "name": "memory_context",
                        "arguments": {"cwd": str(self.workspace)},
                    },
                }
            )
        finally:
            if previous is None:
                os.environ.pop("MEMORYHUB_HOME", None)
            else:
                os.environ["MEMORYHUB_HOME"] = previous
        names = {tool["name"] for tool in listed["result"]["tools"]}
        self.assertEqual(
            {"memory_context", "memory_checkpoint", "memory_tasks", "memory_resume"},
            names,
        )
        self.assertIn("MCP-visible state", called["result"]["content"][0]["text"])

    def test_installer_is_idempotent_and_preserves_existing_config(self) -> None:
        claude_settings = self.home / ".claude" / "settings.json"
        claude_settings.parent.mkdir()
        claude_settings.write_text(
            json.dumps({"model": "opus", "hooks": {"SessionEnd": [{"hooks": []}]}}),
            encoding="utf-8",
        )
        first = install(self.home, configure_agents=False)
        second = install(self.home, configure_agents=False)
        self.assertTrue(Path(first["binary"]).exists())
        self.assertEqual(first["database"], second["database"])
        config = json.loads(claude_settings.read_text(encoding="utf-8"))
        self.assertEqual("opus", config["model"])
        commands = [
            handler["command"]
            for group in config["hooks"]["SessionStart"]
            for handler in group["hooks"]
        ]
        self.assertEqual(1, len([command for command in commands if "memoryhub" in command]))
        codex_agents = (self.home / ".codex" / "AGENTS.md").read_text(encoding="utf-8")
        claude_agents = (self.home / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
        self.assertEqual(1, codex_agents.count(START_MARKER))
        self.assertEqual(1, codex_agents.count(END_MARKER))
        self.assertEqual(1, claude_agents.count(START_MARKER))
        self.assertEqual(1, claude_agents.count(END_MARKER))
        self.assertFalse(second["delegation_skill"]["changed"])
        self.assertFalse(second["autopilot_skills"]["codex"]["changed"])
        self.assertFalse(second["autopilot_skills"]["claude"]["changed"])
        skill = self.home / ".codex" / "skills" / "delegate-to-claude"
        self.assertTrue((skill / "SKILL.md").is_file())
        self.assertTrue((skill / "scripts" / "delegate.py").is_file())
        self.assertFalse(any(item.name == "__pycache__" for item in skill.rglob("*")))
        self.assertFalse(any(item.suffix == ".pyc" for item in skill.rglob("*")))
        app_dir = Path(first["app_dir"])
        self.assertFalse(any(item.name == "__pycache__" for item in app_dir.rglob("*")))
        self.assertFalse(any(item.suffix == ".pyc" for item in app_dir.rglob("*")))
        self.assertIn("PostCompact", config["hooks"])
        self.assertIn("$delegate-to-claude", codex_agents)
        self.assertNotIn("$delegate-to-claude", claude_agents)
        self.assertTrue((self.home / ".codex" / "skills" / "autopilot" / "SKILL.md").is_file())
        self.assertTrue((self.home / ".claude" / "skills" / "autopilot" / "SKILL.md").is_file())
        self.assertIn("$autopilot", codex_agents)
        self.assertIn("/autopilot", claude_agents)
        self.assertNotIn("compact", str(config["hooks"]["SessionStart"]))
        delegated = subprocess.run(
            [
                sys.executable, str(skill / "scripts" / "delegate.py"),
                "--objective", "Validate installed launcher", "--cwd", str(self.workspace),
                "--dry-run",
            ],
            env=self.env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertEqual("dry-run", json.loads(delegated.stdout)["status"])

    def test_installer_migrates_legacy_compact_session_start_hook(self) -> None:
        install(self.home, configure_agents=False)
        settings = self.home / ".claude" / "settings.json"
        config = json.loads(settings.read_text(encoding="utf-8"))
        config["hooks"]["SessionStart"][0]["matcher"] = "startup|resume|compact"
        settings.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        install(self.home, configure_agents=False)

        migrated = json.loads(settings.read_text(encoding="utf-8"))
        matchers = [
            group.get("matcher", "")
            for group in migrated["hooks"]["SessionStart"]
            if any(
                "memoryhub" in handler.get("command", "")
                for handler in group.get("hooks", [])
            )
        ]
        self.assertEqual(["startup|resume"], matchers)

    def test_installer_removes_ignored_skill_artifacts(self) -> None:
        install(self.home, configure_agents=False)
        artifact = (
            self.home / ".codex" / "skills" / "delegate-to-claude" /
            "scripts" / "__pycache__" / "delegate.cpython-999.pyc"
        )
        artifact.parent.mkdir(parents=True)
        artifact.write_bytes(b"bytecode")

        result = install(self.home, configure_agents=False)

        self.assertTrue(result["delegation_skill"]["changed"])
        skill = self.home / ".codex" / "skills" / "delegate-to-claude"
        self.assertFalse(any(item.name == "__pycache__" for item in skill.rglob("*")))
        self.assertFalse(any(item.suffix == ".pyc" for item in skill.rglob("*")))

    def test_compaction_snapshot_is_verified_and_reinjected(self) -> None:
        self.cli(
            "checkpoint", "--actor", "codex",
            "--objective", "Survive context compaction",
            "--summary", "Implementation is half complete",
            "--next-action", "Finish the integration test",
            "--decision", "Use deterministic snapshots",
        )
        payload = {"session_id": "compact-1", "cwd": str(self.workspace)}
        self.cli("hook", "--event", "pre-compact", "--actor", "codex", payload=payload)
        restored = self.cli(
            "hook", "--event", "post-compact", "--actor", "codex", payload=payload
        ).stdout
        self.assertIn("Survive context compaction", restored)
        self.assertIn("Finish the integration test", restored)
        report = json.loads(
            self.cli("compaction-doctor", "--cwd", str(self.workspace), "--json").stdout
        )
        self.assertTrue(report["ok"])
        self.assertEqual(1, report["counts"]["paired"])
        self.assertEqual(1, report["counts"]["verified"])

    def test_compaction_doctor_detects_memory_mutation_between_hooks(self) -> None:
        self.cli(
            "checkpoint", "--actor", "codex", "--summary", "before",
            "--next-action", "compact safely",
        )
        payload = {"session_id": "compact-mismatch", "cwd": str(self.workspace)}
        self.cli("hook", "--event", "pre-compact", "--actor", "codex", payload=payload)
        self.cli(
            "checkpoint", "--actor", "codex", "--summary", "mutated during compaction",
            "--next-action", "investigate mismatch",
        )
        self.cli("hook", "--event", "post-compact", "--actor", "codex", payload=payload)
        result = self.cli(
            "compaction-doctor", "--cwd", str(self.workspace), "--json", check=False
        )
        self.assertEqual(1, result.returncode)
        self.assertEqual(1, json.loads(result.stdout)["counts"]["failed"])

    def test_doctor_confirms_no_network_listener(self) -> None:
        install(self.home, configure_agents=False)
        binary = self.home / ".local" / "bin" / "memoryhub"
        result = subprocess.run(
            [str(binary), "doctor", "--target-home", str(self.home)],
            env={key: value for key, value in self.env.items() if key != "MEMORYHUB_HOME"},
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("no TCP/HTTP listener", result.stdout)
        self.assertIn(str(self.memory_home / "memory.db"), result.stdout)


if __name__ == "__main__":
    unittest.main()

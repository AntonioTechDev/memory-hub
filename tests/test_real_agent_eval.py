from __future__ import annotations

import json
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_real_agent_eval as runner  # noqa: E402


class RealAgentEvalRunnerTests(unittest.TestCase):
    def test_oracle_and_checkpoint_commands_are_complete(self) -> None:
        scenarios = runner.load_oracle(ROOT / "evals" / "real-agent-oracle.json")
        self.assertEqual({"claude_to_codex", "codex_to_claude"}, {item["id"] for item in scenarios})
        for scenario in scenarios:
            command = runner.checkpoint_command(scenario)
            for decision in scenario["checkpoint"]["decisions"]:
                self.assertIn(decision, command)
            self.assertIn(scenario["checkpoint"]["next_action"], command)

    def test_prepare_workspace_creates_isolated_agent_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            runner.prepare_workspace(workspace, "eval-isolated")
            database = workspace / ".memoryhub-eval" / "memory.db"
            with sqlite3.connect(database) as connection:
                schema = connection.execute(
                    "SELECT value FROM meta WHERE key='schema_version'"
                ).fetchone()[0]
            self.assertEqual("3", schema)
            self.assertTrue((workspace / ".codex" / "hooks.json").is_file())
            self.assertTrue((workspace / ".codex" / "config.toml").is_file())
            self.assertTrue((workspace / ".claude" / "settings.json").is_file())
            self.assertTrue((workspace / "memoryhub" / "core.py").is_file())
            self.assertTrue((workspace / ".memoryhub-eval-bin" / "memoryhub").is_file())
            self.assertTrue((workspace / ".git").is_dir())

    def test_exact_token_scoring_reports_missing_items(self) -> None:
        score = runner.score_output("alpha and GAMMA", ["alpha", "beta", "gamma"])
        self.assertEqual(2 / 3, score["score"])
        self.assertEqual(["beta"], score["missing"])
        self.assertFalse(score["passed"])

    def test_claude_api_error_is_reported_with_status(self) -> None:
        result = subprocess.CompletedProcess(
            ["claude"],
            1,
            stdout=json.dumps({"is_error": True, "api_error_status": 401, "result": "Invalid credentials"}),
            stderr="",
        )
        with self.assertRaisesRegex(RuntimeError, "status=401"):
            runner.parse_claude_result(result)


if __name__ == "__main__":
    unittest.main()

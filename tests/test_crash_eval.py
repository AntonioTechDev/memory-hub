from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_crash_eval as crash  # noqa: E402
import run_real_agent_eval as runner  # noqa: E402


class CrashEvalTests(unittest.TestCase):
    def test_abrupt_source_persists_progress_without_finalization(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            workspace = Path(temp)
            runner.prepare_workspace(workspace, "crash-unit")
            expected = crash.create_abrupt_session(workspace, 7)
            self.assertEqual(["CRASH-07-OBJECTIVE", "CRASH-07-NEXT"], expected)
            context = runner.run_process(
                [sys.executable, "-m", "memoryhub", "context"],
                workspace,
                timeout=30,
            ).stdout
            for token in expected:
                self.assertIn(token, context)

    def test_source_program_has_unique_tokens(self) -> None:
        _, first = crash.source_program(Path("/tmp/one"), 1)
        _, second = crash.source_program(Path("/tmp/two"), 2)
        self.assertTrue(set(first).isdisjoint(second))


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sys
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_brain_agent_eval as runner  # noqa: E402


class BrainAgentEvalTests(unittest.TestCase):
    def test_prompt_does_not_contain_commit_or_token(self) -> None:
        text = runner.prompt("prj-postez")
        self.assertNotIn("0e5d6f", text)
        self.assertNotIn("MEMORYHUB_FRESH_PRJ_POSTEZ_", text)

    def test_exact_parser_and_score(self) -> None:
        expected = {
            "PROJECT": "prj-postez", "BRANCH": "main",
            "COMMIT": "a" * 40, "TOKEN": "MEMORYHUB_FRESH_X",
        }
        output = "\n".join(f"{key}={value}" for key, value in expected.items())
        self.assertTrue(runner.score(output, expected)["passed"])
        bad = runner.score(output.replace("BRANCH=main", "BRANCH=feature"), expected)
        self.assertFalse(bad["passed"])
        self.assertIn("BRANCH", bad["mismatched"])

    def test_default_project_is_first_fresh_brain(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state.json"
            state.write_text(json.dumps({"brains": {
                "prj-z": {"status": "pending"},
                "prj-b": {"status": "fresh"},
                "prj-a": {"status": "fresh"},
            }}), encoding="utf-8")
            self.assertEqual("prj-a", runner.select_project_id(None, state))


if __name__ == "__main__":
    unittest.main()

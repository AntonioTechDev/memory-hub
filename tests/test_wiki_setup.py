from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from memoryhub.wiki_setup import (
    canonical_skill,
    discover_mcp_entry,
    doctor,
    file_hash,
    setup,
    validate_local_url,
)


class WikiSetupTests(unittest.TestCase):
    def test_only_local_api_urls_are_accepted(self) -> None:
        self.assertEqual("http://127.0.0.1:19828", validate_local_url("http://127.0.0.1:19828/"))
        for invalid in (
            "https://127.0.0.1:19828",
            "http://example.com:19828",
            "http://127.0.0.1:19828/private",
            "http://127.0.0.1",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                validate_local_url(invalid)

    def test_setup_installs_identical_skills_for_both_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            entry = home / "index.js"
            entry.write_text("// test", encoding="utf-8")
            result = setup(
                home, mcp_entry=str(entry), configure_agents=False
            )
            expected = file_hash(canonical_skill())
            self.assertEqual(expected, result["skills"]["codex"]["sha256"])
            self.assertEqual(expected, result["skills"]["claude"]["sha256"])

    def test_setup_is_idempotent_and_only_backs_up_a_changed_skill(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            entry = home / "index.js"
            entry.write_text("// test", encoding="utf-8")
            old = home / ".codex" / "skills" / "second-brain" / "SKILL.md"
            old.parent.mkdir(parents=True)
            old.write_text("old", encoding="utf-8")
            first = setup(home, mcp_entry=str(entry), configure_agents=False)
            second = setup(home, mcp_entry=str(entry), configure_agents=False)
            self.assertTrue(first["skills"]["codex"]["changed"])
            self.assertIsNotNone(first["skills"]["codex"]["backup"])
            self.assertFalse(second["skills"]["codex"]["changed"])
            self.assertIsNone(second["skills"]["codex"]["backup"])
            self.assertEqual(1, len(list(old.parent.glob("SKILL.md.memoryhub-backup-*"))))

    def test_explicit_mcp_entry_must_exist(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            missing = Path(temp) / "missing.js"
            with self.assertRaisesRegex(ValueError, "not found"):
                discover_mcp_entry(str(missing))

    def test_doctor_reports_missing_runtime_without_hiding_skill_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            entry = home / "index.js"
            entry.write_text("process.exit(1)", encoding="utf-8")
            setup(home, mcp_entry=str(entry), configure_agents=False)
            result = doctor(home, mcp_entry=str(entry))
            self.assertFalse(result["ok"])
            self.assertTrue(result["skills"]["codex"]["matches"])
            self.assertTrue(result["skills"]["claude"]["matches"])
            self.assertFalse(result["mcp"]["ok"])


if __name__ == "__main__":
    unittest.main()

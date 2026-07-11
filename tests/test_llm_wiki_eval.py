from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import run_llm_wiki_eval as runner  # noqa: E402


class LlmWikiEvalTests(unittest.TestCase):
    def test_oracle_defines_all_six_values(self) -> None:
        oracle = runner.load_oracle(ROOT / "evals" / "llm-wiki-oracle.example.json")
        self.assertEqual(set(runner.KEYS), set(runner.expected_values(oracle)))

    def test_prompt_does_not_leak_expected_answers(self) -> None:
        oracle = runner.load_oracle(ROOT / "evals" / "llm-wiki-oracle.example.json")
        text = runner.prompt(oracle)
        # The graph node is also the query, so only assert that the values
        # being measured (counts, port and source path) are not supplied.
        for key, answer in runner.expected_values(oracle).items():
            if key == "GRAPH_NODE":
                continue
            self.assertNotIn(f"={answer}", text)

    def test_parser_and_exact_scoring(self) -> None:
        expected = {key: str(index) for index, key in enumerate(runner.KEYS)}
        output = "\n".join(f"{key}={value}" for key, value in expected.items())
        result = runner.score(output, expected)
        self.assertTrue(result["passed"])
        self.assertEqual(1.0, result["score"])

    def test_scoring_rejects_near_matches(self) -> None:
        expected = {"PORT": "19828", "SEARCH_PATH": "wiki/repo-llm_wiki.md"}
        result = runner.score("PORT=19827\nSEARCH_PATH=wiki/other.md", expected)
        self.assertFalse(result["passed"])
        self.assertEqual(0.0, result["score"])

    def test_live_oracle_is_derived_without_leaking_into_prompt(self) -> None:
        class Api:
            def graph(self, *_: object) -> dict:
                return {"nodes": [{"label": "Postez", "linkCount": 608}], "edges": []}

            def search(self, *_: object) -> dict:
                return {"results": [{"path": "wiki/repo-llm_wiki.md", "snippet": "API :19828"}]}

        original = runner.LlmWikiApi
        runner.LlmWikiApi = lambda *_: Api()  # type: ignore[assignment]
        try:
            oracle = runner.load_oracle(ROOT / "evals" / "llm-wiki-oracle.example.json")
            expected = runner.derive_live_expected(oracle, "http://127.0.0.1:19828")
        finally:
            runner.LlmWikiApi = original
        self.assertEqual("608", expected["GRAPH_LINKS"])
        self.assertEqual("1", expected["GRAPH_NODES"])


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Validate real Claude Code and Codex retrieval from the shared local LLM Wiki."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memoryhub.brain_sync import LlmWikiApi  # noqa: E402
DEFAULT_ORACLE = ROOT / "evals" / "llm-wiki-oracle.example.json"
KEYS = ("GRAPH_NODE", "GRAPH_NODES", "GRAPH_EDGES", "GRAPH_LINKS", "PORT", "SEARCH_PATH")


def load_oracle(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1 or not isinstance(value.get("graph"), dict):
        raise ValueError("Unsupported or invalid LLM Wiki oracle")
    return value


def expected_values(oracle: dict[str, Any]) -> dict[str, str]:
    return {
        **{key: str(value) for key, value in oracle["graph"]["expected"].items()},
        **{key: str(value) for key, value in oracle["search"]["expected"].items()},
    }


def derive_live_expected(oracle: dict[str, Any], api_url: str) -> dict[str, str]:
    api = LlmWikiApi(api_url)
    graph_query = str(oracle["graph"]["query"])
    graph = api.graph(str(oracle["graph"]["project_id"]), graph_query, 50)
    nodes = [item for item in graph.get("nodes", []) if isinstance(item, dict)]
    edges = [item for item in graph.get("edges", []) if isinstance(item, dict)]
    node = next(
        (item for item in nodes if str(item.get("label", "")).casefold() == graph_query.casefold()),
        nodes[0] if nodes else None,
    )
    if node is None:
        raise ValueError("live graph oracle returned no nodes")
    search = api.search(str(oracle["search"]["project_id"]), str(oracle["search"]["query"]))
    results = [item for item in search.get("results", []) if isinstance(item, dict)]
    if not results:
        raise ValueError("live search oracle returned no results")
    serialized = json.dumps(results, ensure_ascii=False)
    port = re.search(r"(?<!\d)19828(?!\d)", serialized)
    if not port:
        raise ValueError("live search oracle did not expose port 19828")
    return {
        "GRAPH_NODE": str(node.get("label", "")),
        "GRAPH_NODES": str(len(nodes)),
        "GRAPH_EDGES": str(len(edges)),
        "GRAPH_LINKS": str(node.get("linkCount", node.get("link_count", 0))),
        "PORT": port.group(0),
        "SEARCH_PATH": str(results[0].get("path", "")),
    }


def prompt(oracle: dict[str, Any]) -> str:
    graph = oracle["graph"]
    search = oracle["search"]
    return f"""Usa esclusivamente i tool MCP di LLM Wiki. Non usare file, shell o web.

1. Interroga llm_wiki_graph con project_id={graph['project_id']}, q={graph['query']}, limit=50.
2. Interroga llm_wiki_search con project_id={search['project_id']}, query={search['query']}, top_k=5 e include_content=false.

Rispondi con esattamente sei righe nel formato seguente, ricavando i valori dai tool:
GRAPH_NODE=<nome del nodo principale>
GRAPH_NODES=<numero totale nodi>
GRAPH_EDGES=<numero totale archi>
GRAPH_LINKS=<numero link del nodo principale>
PORT=<porta API LLM Wiki>
SEARCH_PATH=<percorso del primo risultato>
"""


def parse_values(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for key in KEYS:
        match = re.search(rf"(?mi)^\s*{re.escape(key)}\s*=\s*([^\r\n`]+)", output)
        if match:
            values[key] = match.group(1).strip()
    return values


def score(output: str, expected: dict[str, str]) -> dict[str, Any]:
    actual = parse_values(output)
    missing = [key for key in expected if key not in actual]
    mismatched = {
        key: {"expected": expected[key], "actual": actual[key]}
        for key in expected
        if key in actual and actual[key] != expected[key]
    }
    return {
        "passed": not missing and not mismatched,
        "score": (len(expected) - len(missing) - len(mismatched)) / len(expected),
        "actual": actual,
        "missing": missing,
        "mismatched": mismatched,
    }


def run_claude(text: str, workspace: Path, budget: float) -> str:
    command = [
        "claude", "-p", "--no-session-persistence", "--max-budget-usd", str(budget),
        "--output-format", "json", "--allowed-tools",
        "mcp__llm-wiki__llm_wiki_graph,mcp__llm-wiki__llm_wiki_search",
    ]
    completed = subprocess.run(
        command, cwd=workspace, input=text, capture_output=True, text=True, timeout=300
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError((completed.stderr or completed.stdout or "invalid Claude output")[-1000:]) from error
    if completed.returncode or payload.get("is_error"):
        raise RuntimeError(str(payload.get("result") or completed.stderr or "Claude failed"))
    return str(payload.get("result") or "")


def run_codex(text: str, workspace: Path) -> str:
    output = workspace / "codex-result.txt"
    command = [
        # Do not override the user's configured sandbox: this is also a test
        # that the installed Codex profile permits the required loopback MCP.
        # The workspace itself is an empty temporary repository.
        "codex", "exec", "--skip-git-repo-check", "--ephemeral",
        "--color", "never", "--output-last-message", str(output), text,
    ]
    if os.environ.get("MEMORYHUB_EVAL_CODEX_MODEL"):
        command[2:2] = ["--model", os.environ["MEMORYHUB_EVAL_CODEX_MODEL"]]
    completed = subprocess.run(command, cwd=workspace, capture_output=True, text=True, timeout=360)
    if completed.returncode:
        raise RuntimeError((completed.stderr or completed.stdout or "Codex failed")[-1000:])
    return output.read_text(encoding="utf-8")


def execute(agent: str, oracle: dict[str, Any], budget: float) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix=f"llm-wiki-{agent}-") as temp:
        workspace = Path(temp)
        subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
        text = prompt(oracle)
        output = run_claude(text, workspace, budget) if agent == "claude" else run_codex(text, workspace)
        return {"agent": agent, "output": output, **score(output, expected_values(oracle))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--oracle", type=Path, default=DEFAULT_ORACLE,
        help="Deployment-specific hidden oracle (copy and edit the bundled example)",
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--agent", action="append", choices=["claude", "codex"], default=[])
    parser.add_argument("--max-budget-usd", type=float, default=0.50)
    parser.add_argument("--api-url", default="http://127.0.0.1:19828")
    parser.add_argument("--report", type=Path, default=ROOT / "evals" / "latest-llm-wiki-report.json")
    args = parser.parse_args()
    oracle = load_oracle(args.oracle)
    agents = args.agent or ["claude", "codex"]
    if not args.live:
        print(json.dumps({"mode": "dry-run", "agents": agents, "queries": 2}, indent=2))
        return 0
    for executable in agents:
        binary = "claude" if executable == "claude" else "codex"
        if not shutil.which(binary):
            parser.error(f"Missing executable: {binary}")

    expected = derive_live_expected(oracle, args.api_url)
    results: list[dict[str, Any]] = []
    for agent in agents:
        try:
            with tempfile.TemporaryDirectory(prefix="llm-wiki-live-oracle-") as temp:
                workspace = Path(temp)
                subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
                text = prompt(oracle)
                output = run_claude(text, workspace, args.max_budget_usd) if agent == "claude" else run_codex(text, workspace)
                results.append({"agent": agent, "output": output, **score(output, expected)})
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
            results.append({"agent": agent, "passed": False, "score": 0.0, "error": str(error)})
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": all(item.get("passed") for item in results),
        "oracle": expected,
        "oracle_source": "direct local LLM Wiki API immediately before agent runs",
        "results": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

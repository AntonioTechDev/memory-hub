#!/usr/bin/env python3
"""Run opt-in, model-level Claude Code <-> Codex Memory Hub evaluations."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memoryhub.install import merge_hooks  # noqa: E402

DEFAULT_ORACLE = ROOT / "evals" / "real-agent-oracle.json"
BACKEND_ENV_KEYS = {
    "AGENT_MEMORY_BACKEND",
    "AGENT_MEMORY_TENCENT_URL",
    "AGENT_MEMORY_TENCENT_API_KEY",
    "AGENT_MEMORY_BACKEND_TIMEOUT",
}


def load_oracle(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 1 or not isinstance(payload.get("scenarios"), list):
        raise ValueError("Unsupported or invalid real-agent oracle")
    return payload["scenarios"]


def checkpoint_command(scenario: dict[str, Any]) -> list[str]:
    checkpoint = scenario["checkpoint"]
    command = [
        "python3",
        "-m",
        "memoryhub",
        "checkpoint",
        "--actor",
        scenario["source"],
        "--status",
        checkpoint["status"],
        "--objective",
        checkpoint["objective"],
        "--summary",
        checkpoint["summary"],
        "--next-action",
        checkpoint["next_action"],
        "--blocker",
        checkpoint["blocker"],
        "--validation",
        checkpoint["validation"],
    ]
    for decision in checkpoint["decisions"]:
        command.extend(["--decision", decision])
    return command


def prepare_workspace(destination: Path, project_id: str) -> None:
    run_process(["git", "init", "-q"], destination, timeout=30)
    for name in ("AGENTS.md", "CLAUDE.md"):
        shutil.copy2(ROOT / name, destination / name)
    for directory in (".codex", ".claude"):
        (destination / directory).mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "memoryhub", destination / "memoryhub")
    launcher = destination / ".memoryhub-eval-bin" / "memoryhub"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    launcher.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.path.insert(0, {str(destination)!r})\n"
        "from memoryhub.cli import main\n"
        "raise SystemExit(main())\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    merge_hooks(destination / ".codex" / "hooks.json", launcher, "codex")
    merge_hooks(destination / ".claude" / "settings.json", launcher, "claude-code")
    (destination / ".codex" / "config.toml").write_text(
        "[features]\nhooks = true\n", encoding="utf-8"
    )
    run_process(
        [sys.executable, "-m", "memoryhub", "init"],
        destination,
        timeout=30,
    )


def clean_environment(cwd: Path) -> dict[str, str]:
    environment = dict(os.environ)
    for key in BACKEND_ENV_KEYS:
        environment.pop(key, None)
    environment["MEMORYHUB_HOME"] = str(cwd / ".memoryhub-eval")
    environment["PYTHONPATH"] = str(cwd)
    return environment


def run_process(
    command: list[str],
    cwd: Path,
    timeout: int,
    *,
    check: bool = True,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=clean_environment(cwd),
        input=input_text,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=check,
    )


def source_prompt(scenario: dict[str, Any]) -> str:
    command = shlex.join(checkpoint_command(scenario))
    return (
        "Esegui esattamente questo checkpoint nel workspace e non cambiare altri file:\n"
        f"{command}\n"
        "Dopo il comando, conferma soltanto che il checkpoint è stato scritto."
    )


def receiver_prompt() -> str:
    return (
        "Senza leggere file, senza web e senza eseguire tool, usa esclusivamente il contesto "
        "caricato automaticamente all'avvio. Restituisci obiettivo, stato, prossima azione, "
        "blocker, decisioni e validazione del handoff. Mantieni letterali codici e comandi."
    )


def run_claude(prompt: str, workspace: Path, budget: float, source: bool) -> str:
    command = [
        "claude",
        "-p",
        "--no-session-persistence",
        "--max-budget-usd",
        str(budget),
        "--output-format",
        "json",
    ]
    if source:
        command.extend(["--dangerously-skip-permissions", "--allowed-tools", "Bash"])
    else:
        command.extend(["--tools", ""])
    result = run_process(command, workspace, timeout=300, check=False, input_text=prompt)
    return parse_claude_result(result)


def parse_claude_result(result: subprocess.CompletedProcess[str]) -> str:
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        detail = (result.stderr or result.stdout or "no output").strip()[-1000:]
        raise RuntimeError(f"Claude returned invalid JSON (exit {result.returncode}): {detail}") from error
    if result.returncode != 0 or payload.get("is_error"):
        status = payload.get("api_error_status")
        reason = payload.get("result") or result.stderr or "unknown error"
        raise RuntimeError(f"Claude API error status={status}, exit={result.returncode}: {reason}")
    return str(payload.get("result") or "")


def run_codex(prompt: str, workspace: Path, source: bool) -> str:
    output_path = workspace / ".codex-last-message.txt"
    command = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--ephemeral",
        "-c",
        f'projects.{json.dumps(str(workspace))}.trust_level="trusted"',
        "--dangerously-bypass-hook-trust",
        "--color",
        "never",
        "--output-last-message",
        str(output_path),
    ]
    if os.environ.get("MEMORYHUB_EVAL_CODEX_MODEL"):
        command.extend(["--model", os.environ["MEMORYHUB_EVAL_CODEX_MODEL"]])
    if source:
        command.append("--dangerously-bypass-approvals-and-sandbox")
    else:
        command.extend(["--sandbox", "workspace-write"])
    command.append(prompt)
    result = run_process(command, workspace, timeout=360, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "no output").strip()[-1000:]
        raise RuntimeError(f"Codex error exit={result.returncode}: {detail}")
    return output_path.read_text(encoding="utf-8")


def run_agent(agent: str, prompt: str, workspace: Path, budget: float, source: bool) -> str:
    if agent == "claude-code":
        return run_claude(prompt, workspace, budget, source)
    if agent == "codex":
        return run_codex(prompt, workspace, source)
    raise ValueError(f"Unknown agent: {agent}")


def score_output(output: str, expected_tokens: list[str]) -> dict[str, Any]:
    normalized = output.casefold()
    found = [token for token in expected_tokens if token.casefold() in normalized]
    missing = [token for token in expected_tokens if token.casefold() not in normalized]
    return {
        "found": found,
        "missing": missing,
        "score": len(found) / len(expected_tokens) if expected_tokens else 1.0,
        "passed": not missing,
    }


def validate_source_state(workspace: Path, scenario: dict[str, Any]) -> None:
    checkpoint = scenario["checkpoint"]
    database = workspace / ".memoryhub-eval" / "memory.db"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        state = connection.execute("SELECT * FROM tasks ORDER BY updated_at DESC LIMIT 1").fetchone()
    if state is None or state["updated_by"] != scenario["source"]:
        raise RuntimeError("Source agent did not write the expected Memory Hub checkpoint actor")
    for field in ("objective", "summary", "next_action"):
        if state[field] != checkpoint[field]:
            raise RuntimeError(f"Source Memory Hub checkpoint mismatch: {field}")


def execute_scenario(
    scenario: dict[str, Any], budget: float, keep: bool, *, seed_source: bool = False
) -> dict[str, Any]:
    workspace = Path(tempfile.mkdtemp(prefix=f"agent-memory-{scenario['id']}-"))
    try:
        prepare_workspace(workspace, scenario["project_id"])
        if seed_source:
            seeded = run_process(checkpoint_command(scenario), workspace, timeout=30)
            source_output = seeded.stdout
        else:
            source_output = run_agent(
                scenario["source"], source_prompt(scenario), workspace, budget, source=True
            )
        validate_source_state(workspace, scenario)
        target_output = run_agent(scenario["target"], receiver_prompt(), workspace, budget, source=False)
        score = score_output(target_output, scenario["expected_tokens"])
        return {
            "id": scenario["id"],
            "source": scenario["source"],
            "target": scenario["target"],
            "workspace": str(workspace) if keep else None,
            "source_output": source_output,
            "target_output": target_output,
            **score,
        }
    finally:
        if not keep:
            shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--oracle", type=Path, default=DEFAULT_ORACLE)
    parser.add_argument("--scenario", action="append", default=[])
    parser.add_argument("--live", action="store_true", help="Actually invoke both paid/authenticated agent CLIs")
    parser.add_argument("--keep", action="store_true", help="Keep temporary evaluation workspaces")
    parser.add_argument(
        "--seed-source",
        action="store_true",
        help="Diagnostic only: seed the checkpoint locally and invoke only the receiving real agent",
    )
    parser.add_argument("--max-budget-usd", type=float, default=0.50, help="Per Claude invocation")
    parser.add_argument("--report", type=Path, default=ROOT / "evals" / "latest-real-agent-report.json")
    args = parser.parse_args()

    scenarios = load_oracle(args.oracle)
    if args.scenario:
        wanted = set(args.scenario)
        scenarios = [scenario for scenario in scenarios if scenario["id"] in wanted]
        unknown = wanted - {scenario["id"] for scenario in scenarios}
        if unknown:
            parser.error(f"Unknown scenario(s): {', '.join(sorted(unknown))}")

    plan = [
        {
            "id": scenario["id"],
            "source": scenario["source"],
            "target": scenario["target"],
            "expected_tokens": len(scenario["expected_tokens"]),
        }
        for scenario in scenarios
    ]
    if not args.live:
        print(json.dumps({"mode": "dry-run", "scenarios": plan}, ensure_ascii=False, indent=2))
        print("Re-run with --live after both `claude auth status` and Codex authentication are healthy.")
        return 0

    for executable in ("claude", "codex"):
        if not shutil.which(executable):
            parser.error(f"Missing executable: {executable}")

    results: list[dict[str, Any]] = []
    for scenario in scenarios:
        try:
            results.append(
                execute_scenario(
                    scenario,
                    args.max_budget_usd,
                    args.keep,
                    seed_source=args.seed_source,
                )
            )
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, json.JSONDecodeError) as error:
            results.append(
                {
                    "id": scenario["id"],
                    "source": scenario["source"],
                    "target": scenario["target"],
                    "passed": False,
                    "score": 0.0,
                    "error": str(error),
                }
            )

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": all(result.get("passed") for result in results),
        "results": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

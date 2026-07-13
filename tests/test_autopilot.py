from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from memoryhub.autopilot import (
    AutopilotStore,
    GoalContract,
    ProviderUsage,
    Route,
    TaskContract,
    classify_goal,
    default_goal,
    default_tasks,
    parallel_batch,
    parse_usage,
    route_task,
    validate_plan,
)
from memoryhub.autopilot_runner import (
    AutopilotRunner,
    is_provider_infrastructure_block,
    validation_command_allowed,
)
from memoryhub.core import MemoryStore


class AutopilotContractTests(unittest.TestCase):
    def test_trivial_goal_stays_one_fast_task(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            cwd = Path(raw)
            goal = default_goal("Cambia il colore del bottone in blu", cwd)
            tasks = default_tasks(goal, cwd)
        self.assertEqual(("xs", "low"), (goal.complexity, goal.risk))
        self.assertEqual(1, len(tasks))
        self.assertEqual("fast", tasks[0].profile)
        validate_plan(goal, tasks)

    def test_provider_task_ids_are_case_normalized(self) -> None:
        task = TaskContract.from_dict(
            {
                "id": "T2",
                "title": "Build",
                "objective": "Build",
                "acceptance_criteria": ["Done"],
                "validations": ["make test"],
                "depends_on": ["T1"],
            }
        )
        self.assertEqual(("t2", ["t1"]), (task.id, task.depends_on))

    def test_over_engineered_and_cyclic_plans_are_rejected(self) -> None:
        goal = GoalContract("Tiny", ["Done"], complexity="xs", risk="low")
        tasks = [
            TaskContract("t1", "One", "One", ["Done"], ["make test"]),
            TaskContract("t2", "Two", "Two", ["Done"], ["make test"]),
        ]
        with self.assertRaisesRegex(ValueError, "over-engineers"):
            validate_plan(goal, tasks)
        medium = GoalContract("Medium", ["Done"], complexity="m", risk="medium")
        cycle = [
            TaskContract("t1", "One", "One", ["Done"], ["make test"], depends_on=["t2"]),
            TaskContract("t2", "Two", "Two", ["Done"], ["make test"], depends_on=["t1"]),
        ]
        with self.assertRaisesRegex(ValueError, "cycle"):
            validate_plan(medium, cycle)

    def test_parallelism_requires_disjoint_explicit_scopes(self) -> None:
        left = TaskContract(
            "t1", "Backend", "Backend", ["Done"], ["make test"],
            allowed_paths=["src/api"], parallel_safe=True,
        )
        right = TaskContract(
            "t2", "Frontend", "Frontend", ["Done"], ["make test"],
            allowed_paths=["web"], parallel_safe=True,
        )
        overlap = TaskContract(
            "t3", "API tests", "Tests", ["Done"], ["make test"],
            allowed_paths=["src"], parallel_safe=True,
        )
        self.assertEqual(["t1", "t2"], [item.id for item in parallel_batch([left, right], 2)])
        self.assertEqual(["t1"], [item.id for item in parallel_batch([left, overlap], 2)])

    def test_routing_obeys_usage_and_alternates_after_failure(self) -> None:
        task = TaskContract("t1", "Build", "Build", ["Done"], ["make test"])
        usage = {
            "codex": ProviderUsage("codex", status="rate_limited"),
            "claude": ProviderUsage("claude", status="available"),
        }
        self.assertEqual("claude", route_task(task, usage).provider)
        usage["codex"] = ProviderUsage("codex", status="available")
        self.assertEqual(
            "claude", route_task(task, usage, previous_provider="codex").provider
        )

    def test_usage_parser_normalizes_limits_and_redacts(self) -> None:
        value = parse_usage(
            "claude", "Usage limit reached. Resets in 2 hours. api_key=secret-value"
        )
        self.assertEqual("rate_limited", value.status)
        self.assertEqual("2 hours", value.reset_at)
        self.assertNotIn("secret-value", value.raw_excerpt)

    def test_validation_allowlist_rejects_destructive_or_arbitrary_commands(self) -> None:
        self.assertTrue(validation_command_allowed(["python3", "-m", "unittest", "discover"]))
        self.assertTrue(validation_command_allowed(["npm", "run", "test"]))
        self.assertFalse(validation_command_allowed(["python3", "-c", "do_damage()"]))
        self.assertFalse(validation_command_allowed(["npm", "publish"]))
        self.assertFalse(validation_command_allowed(["make", "deploy"]))
        self.assertFalse(validation_command_allowed(["git", "push"]))

    def test_sandbox_initialization_block_is_provider_fallback_not_goal_block(self) -> None:
        self.assertTrue(
            is_provider_infrastructure_block(
                {
                    "status": "blocked",
                    "summary": "workspace was not writable",
                    "validations": ["bwrap: failed RTM_NEWADDR"],
                    "blockers": ["sandbox initialization failed"],
                }
            )
        )
        self.assertTrue(
            is_provider_infrastructure_block(
                {
                    "status": "blocked",
                    "summary": "python could not run: command required approval and was not approved",
                    "blockers": ["no approval prompt reaches the worker"],
                }
            )
        )
        self.assertFalse(
            is_provider_infrastructure_block(
                {"status": "blocked", "summary": "missing product decision", "blockers": []}
            )
        )


class AutopilotStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.repo = self.base / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("test\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "init"],
            cwd=self.repo,
            check=True,
        )
        self.db = self.base / "memory.db"
        self.memory = MemoryStore(self.db)
        self.store = AutopilotStore(self.memory)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_schema_one_is_migrated_additively(self) -> None:
        with sqlite3.connect(self.db) as db:
            db.execute("CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO meta VALUES('schema_version', '1')")
        self.store.initialize()
        with sqlite3.connect(self.db) as db:
            self.assertEqual(
                "2", db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
            )
            self.assertIsNotNone(
                db.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='autopilot_jobs'"
                ).fetchone()
            )

    def test_job_plan_leases_and_recovery_are_durable(self) -> None:
        goal = GoalContract("Build feature", ["Tests pass"])
        job_id = self.store.create_job(cwd=self.repo, goal=goal)
        task = TaskContract("t1", "Build", "Build", ["Tests pass"], ["make test"])
        self.store.save_plan(job_id, [task])
        self.assertEqual(["t1"], [item.id for item in self.store.ready_contracts(job_id)])
        self.assertTrue(self.store.claim_task(job_id, "t1", "runner-a", 60))
        self.assertFalse(self.store.claim_task(job_id, "t1", "runner-b", 60))
        self.assertEqual(1, self.store.recover_running_tasks(job_id, reason="crash"))
        self.assertTrue(self.store.claim_task(job_id, "t1", "runner-b", 60))


class AutopilotRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.repo = self.base / "repo"
        self.home.mkdir()
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        (self.repo / "README.md").write_text("autopilot test\n", encoding="utf-8")
        (self.repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md", ".gitignore"], cwd=self.repo, check=True)
        subprocess.run(
            ["git", "-c", "user.name=Test", "-c", "user.email=test@example.test", "commit", "-qm", "init"],
            cwd=self.repo,
            check=True,
        )
        self.fake = self.base / "fake-codex"
        self.fake.write_text(
            """#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
out = pathlib.Path(args[args.index('--output-last-message') + 1])
prompt = args[-1]
if 'stateless technical lead' in prompt:
    if os.environ.get('FAKE_PLAN') == 'parallel':
      value = {
       'goal': {'objective':'Create two files','done_when':['both exist'],
                'constraints':['minimal diff'],'non_goals':['cleanup'],
                'complexity':'m','risk':'low'},
       'tasks': [
        {'id':'t1','title':'Create A','objective':'Create a.txt',
         'acceptance_criteria':['a exists'],'validations':['python3 -m compileall -q .'],
         'allowed_paths':['a.txt'],'constraints':['minimal diff'],'depends_on':[],
         'profile':'builder','risk':'low','parallel_safe':True},
        {'id':'t2','title':'Create B','objective':'Create b.txt',
         'acceptance_criteria':['b exists'],'validations':['python3 -m compileall -q .'],
         'allowed_paths':['b.txt'],'constraints':['minimal diff'],'depends_on':[],
         'profile':'builder','risk':'low','parallel_safe':True}
       ]}
    else:
      value = {
      'goal': {'objective':'Create result file','done_when':['result exists'],
               'constraints':['minimal diff'],'non_goals':['unrelated cleanup'],
               'complexity':'s','risk':'low'},
      'tasks': [{'id':'t1','title':'Create result','objective':'Create result.txt',
                 'acceptance_criteria':['result exists'],
                 'validations':['python3 -m compileall -q .'],
                 'allowed_paths':['result.txt'],'constraints':['minimal diff'],
                 'depends_on':[],'profile':'builder','risk':'low','parallel_safe':False}]
      }
elif 'independent final reviewer' in prompt:
    value = {'passed':True,'summary':'verified','answers':['goal met'],
             'required_fixes':[],'residual_risks':[]}
else:
    if os.environ.get('FAKE_CODEX_WORKER_RATE_LIMIT') == '1':
      print('usage limit reached', file=sys.stderr)
      raise SystemExit(1)
    if os.environ.get('FAKE_ALL_INFRA_BLOCK') == '1':
      value = {'status':'blocked','summary':'sandbox initialization failed',
               'next_action':'retry','files':[],'validations':[],
               'blockers':['bwrap: failed RTM_NEWADDR']}
      out.write_text(json.dumps(value), encoding='utf-8')
      raise SystemExit(0)
    target = 'a.txt' if 'Create a.txt' in prompt else 'result.txt'
    pathlib.Path(target).write_text('completed\\n', encoding='utf-8')
    value = {'status':'done','summary':'created result','next_action':'validate',
             'files':[target],'validations':['compileall pending'],'blockers':[]}
out.write_text(json.dumps(value), encoding='utf-8')
""",
            encoding="utf-8",
        )
        self.fake.chmod(0o755)
        self.fake_claude = self.base / "fake-claude"
        self.fake_claude.write_text(
            """#!/usr/bin/env python3
import json, os, pathlib, sys
prompt = sys.argv[-1]
if 'independent final reviewer' in prompt:
  value = {'passed':True,'summary':'verified','answers':['goal met'],
           'required_fixes':[],'residual_risks':[]}
elif 'stateless technical lead' in prompt:
  value = {'goal': {'objective':'Fallback goal','done_when':['done'],
                    'constraints':[],'non_goals':[],'complexity':'s','risk':'low'},
           'tasks': [{'id':'t1','title':'Fallback','objective':'Create result.txt',
                      'acceptance_criteria':['done'],
                      'validations':['python3 -m compileall -q .'],
                      'allowed_paths':['result.txt'],'constraints':[], 'depends_on':[],
                      'profile':'builder','risk':'low','parallel_safe':False}]}
else:
  target = 'b.txt' if 'Create b.txt' in prompt else 'result.txt'
  if os.environ.get('FAKE_ALL_INFRA_BLOCK') == '1':
    value = {'status':'blocked','summary':'This command requires approval',
             'next_action':'retry','files':[],'validations':[],
             'blockers':['no approval prompt']}
    print(json.dumps({'is_error':False,'structured_output':value}))
    raise SystemExit(0)
  pathlib.Path(target).write_text('completed\\n', encoding='utf-8')
  if os.environ.get('FAKE_CLAUDE_APPROVAL_BLOCK') == '1':
    value = {'status':'blocked','summary':'This command requires approval',
             'next_action':'runner validates','files':[target],
             'validations':[],'blockers':['no approval prompt']}
  else:
    value = {'status':'done','summary':'created by Claude','next_action':'validate',
             'files':[target],'validations':['compileall pending'],'blockers':[]}
print(json.dumps({'is_error':False,'structured_output':value}))
""",
            encoding="utf-8",
        )
        self.fake_claude.chmod(0o755)
        self.env = {
            **os.environ,
            "HOME": str(self.home),
            "MEMORYHUB_HOME": str(self.home / ".local" / "share" / "memoryhub"),
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_end_to_end_job_uses_fresh_agents_validates_and_fast_forwards(self) -> None:
        with patch.dict(os.environ, self.env, clear=False):
            store = AutopilotStore()
            job_id = store.create_job(
                cwd=self.repo,
                goal=GoalContract("Create result file", ["result exists"], complexity="s", risk="low"),
                lead_provider="codex",
            )
            runner = AutopilotRunner(
                store=store,
                codex_binary=str(self.fake),
                claude_binary=str(self.fake_claude),
                provider_timeout=20,
                validation_timeout=20,
                refresh_provider_usage=False,
            )
            result = runner.run_job(job_id, self.repo)
        self.assertEqual("completed", result["status"])
        self.assertEqual("completed\n", (self.repo / "result.txt").read_text(encoding="utf-8"))
        self.assertEqual("completed", result["tasks"][0]["status"])
        self.assertTrue(result["apply_result"]["applied"])
        self.assertEqual("", subprocess.run(
            ["git", "status", "--porcelain"], cwd=self.repo,
            capture_output=True, text=True, check=True,
        ).stdout)

    def test_recovery_reaps_orphan_provider_process_group(self) -> None:
        with patch.dict(os.environ, self.env, clear=False):
            store = AutopilotStore()
            job_id = store.create_job(
                cwd=self.repo,
                goal=GoalContract("Recover", ["No orphan remains"], complexity="s", risk="low"),
            )
            run_id = store.create_run(
                job_id=job_id,
                task_id=None,
                role="worker",
                route=Route("codex", "", "low", "fast", "test"),
            )
            process = subprocess.Popen(["sleep", "60"], start_new_session=True)
            try:
                store.update_run_pid(run_id, process.pid)
                runner = AutopilotRunner(store=store, refresh_provider_usage=False)
                self.assertEqual(1, runner.reap_orphan_provider_runs(job_id))
                process.wait(timeout=3)
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, 9)
                    process.wait(timeout=3)
        self.assertEqual([], store.active_runs(job_id))

    def test_rate_limit_falls_back_from_codex_to_claude(self) -> None:
        with patch.dict(
            os.environ,
            {**self.env, "FAKE_CODEX_WORKER_RATE_LIMIT": "1"},
            clear=False,
        ):
            store = AutopilotStore()
            job_id = store.create_job(
                cwd=self.repo,
                goal=GoalContract("Create result file", ["result exists"], complexity="s", risk="low"),
                lead_provider="codex",
            )
            runner = AutopilotRunner(
                store=store,
                codex_binary=str(self.fake),
                claude_binary=str(self.fake_claude),
                provider_timeout=20,
                validation_timeout=20,
                refresh_provider_usage=False,
            )
            result = runner.run_job(job_id, self.repo)
        self.assertEqual("completed", result["status"])
        self.assertEqual("claude", result["tasks"][0]["provider"])
        self.assertEqual(2, result["tasks"][0]["attempt_count"])
        self.assertEqual("rate_limited", result["usage"]["codex"]["status"])

    def test_runner_recovers_valid_changes_when_worker_cannot_run_tests(self) -> None:
        with patch.dict(
            os.environ, {**self.env, "FAKE_CLAUDE_APPROVAL_BLOCK": "1"}, clear=False
        ):
            store = AutopilotStore()
            job_id = store.create_job(
                cwd=self.repo,
                goal=GoalContract("Create result file", ["result exists"], complexity="s", risk="low"),
                lead_provider="claude",
            )
            result = AutopilotRunner(
                store=store,
                codex_binary=str(self.fake),
                claude_binary=str(self.fake_claude),
                provider_timeout=20,
                validation_timeout=20,
                refresh_provider_usage=False,
            ).run_job(job_id, self.repo)
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, result["tasks"][0]["attempt_count"])
        self.assertTrue(result["tasks"][0]["result"]["recovered_by_runner_validation"])

    def test_retry_gate_stops_at_configured_attempt_count(self) -> None:
        with patch.dict(os.environ, {**self.env, "FAKE_ALL_INFRA_BLOCK": "1"}, clear=False):
            store = AutopilotStore()
            job_id = store.create_job(
                cwd=self.repo,
                goal=GoalContract("Create result file", ["result exists"], complexity="s", risk="low"),
                max_attempts=2,
                lead_provider="codex",
            )
            result = AutopilotRunner(
                store=store,
                codex_binary=str(self.fake),
                claude_binary=str(self.fake_claude),
                provider_timeout=20,
                validation_timeout=20,
                refresh_provider_usage=False,
            ).run_job(job_id, self.repo)
        self.assertEqual("blocked", result["status"])
        self.assertEqual(2, result["tasks"][0]["attempt_count"])

    def test_two_disjoint_tasks_use_two_providers_and_integrate(self) -> None:
        with patch.dict(os.environ, {**self.env, "FAKE_PLAN": "parallel"}, clear=False):
            store = AutopilotStore()
            job_id = store.create_job(
                cwd=self.repo,
                goal=GoalContract("Create two files", ["both exist"], complexity="m", risk="low"),
                max_workers=2,
                lead_provider="codex",
            )
            runner = AutopilotRunner(
                store=store,
                codex_binary=str(self.fake),
                claude_binary=str(self.fake_claude),
                provider_timeout=20,
                validation_timeout=20,
                refresh_provider_usage=False,
            )
            result = runner.run_job(job_id, self.repo)
        self.assertEqual("completed", result["status"])
        self.assertEqual({"codex", "claude"}, {task["provider"] for task in result["tasks"]})
        self.assertTrue((self.repo / "a.txt").is_file())
        self.assertTrue((self.repo / "b.txt").is_file())


if __name__ == "__main__":
    unittest.main()

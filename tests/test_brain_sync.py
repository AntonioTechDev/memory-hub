from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from memoryhub.brain_sync import (
    HOOK_START,
    allowed_path,
    doctor_brains,
    load_config,
    register_brain,
    sync_brain,
)


class FakeWikiHandler(BaseHTTPRequestHandler):
    project_id = "prj-demo"
    project_path = Path("/")
    rescans = 0

    def log_message(self, *_: object) -> None:
        return

    def reply(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def freshness(self) -> str:
        path = self.project_path / "wiki" / "memoryhub-freshness.md"
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/v1/projects":
            self.reply({"projects": [{
                "id": self.project_id, "name": self.project_id,
                "path": str(self.project_path), "current": False,
            }], "currentProject": None})
            return
        prefix = f"/api/v1/projects/{self.project_id}/"
        if parsed.path == prefix + "files/content":
            relative = parse_qs(parsed.query).get("path", [""])[0]
            path = self.project_path / unquote(relative)
            self.reply({"path": relative, "content": path.read_text(encoding="utf-8") if path.exists() else ""})
            return
        if parsed.path == prefix + "graph":
            query = parse_qs(parsed.query).get("q", [""])[0]
            content = self.freshness()
            nodes = [{"id": query, "label": query, "type": "freshness"}] if query and query in content else []
            self.reply({"nodes": nodes, "edges": []})
            return
        self.reply({"error": "not found"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        prefix = f"/api/v1/projects/{self.project_id}/"
        if parsed.path == prefix + "sources/rescan":
            type(self).rescans += 1
            self.reply({"ok": True, "changedTasks": []})
            return
        if parsed.path == prefix + "search":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            query = str(payload.get("query", ""))
            content = self.freshness()
            results = [{"path": "wiki/memoryhub-freshness.md", "title": query, "content": content}] if query and query in content else []
            self.reply({"results": results, "mode": "keyword"})
            return
        self.reply({"error": "not found"}, 404)


class BrainSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home"
        self.repo = self.base / "demo-repo"
        self.brain = self.base / "brain"
        self.config = self.base / "brains.json"
        self.state = self.base / "state.json"
        self.home.mkdir()
        self.repo.mkdir()
        (self.brain / ".llm-wiki").mkdir(parents=True)
        (self.brain / ".llm-wiki" / "project.json").write_text(
            json.dumps({"id": "prj-demo", "name": "Demo"}), encoding="utf-8"
        )
        (self.brain / ".llm-wiki" / "file-change-queue.json").write_text(
            json.dumps({"version": 1, "tasks": []}), encoding="utf-8"
        )
        (self.brain / "wiki").mkdir()
        (self.brain / "wiki" / "overview.md").write_text("# Overview", encoding="utf-8")
        (self.brain / "wiki" / "index.md").write_text("# Index", encoding="utf-8")
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=self.repo, check=True)
        subprocess.run(["git", "config", "user.name", "Memory Hub Test"], cwd=self.repo, check=True)
        (self.repo / "app.py").write_text("def stable():\n    return 'main'\n", encoding="utf-8")
        self.commit("initial")

        FakeWikiHandler.project_path = self.brain
        FakeWikiHandler.project_id = "prj-demo"
        FakeWikiHandler.rescans = 0
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeWikiHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.api_url = f"http://127.0.0.1:{self.server.server_port}"
        self.previous_home = os.environ.get("HOME")
        self.previous_memory_home = os.environ.get("MEMORYHUB_HOME")
        os.environ["HOME"] = str(self.home)
        os.environ["MEMORYHUB_HOME"] = str(self.home / ".local" / "share" / "memoryhub")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        if self.previous_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = self.previous_home
        if self.previous_memory_home is None:
            os.environ.pop("MEMORYHUB_HOME", None)
        else:
            os.environ["MEMORYHUB_HOME"] = self.previous_memory_home
        self.temp.cleanup()

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=self.repo, check=True, capture_output=True, text=True
        ).stdout.strip()

    def commit(self, message: str) -> str:
        subprocess.run(["git", "add", "-A"], cwd=self.repo, check=True)
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=self.repo, check=True)
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.repo, check=True,
            capture_output=True, text=True,
        ).stdout.strip()

    def register(self, threshold: int = 10, *, hook: bool = True, refresh: list[str] | None = None) -> dict:
        return register_brain(
            "prj-demo", self.repo, "main", api_url=self.api_url,
            config_path=self.config, large_merge_threshold=threshold,
            refresh_command=refresh or [], binary="/usr/bin/true", install_hook=hook,
        )

    def sync(self, **kwargs: object) -> dict:
        return sync_brain(
            "prj-demo", config_path=self.config, state_path=self.state,
            api_url=self.api_url, wait_seconds=0, **kwargs,
        )

    def test_register_installs_idempotent_hook_and_private_config(self) -> None:
        first = self.register()
        second = self.register()
        hook = Path(first["hook"])
        self.assertEqual(first["hook"], second["hook"])
        self.assertEqual(1, hook.read_text(encoding="utf-8").count(HOOK_START))
        self.assertIn(str(self.config), hook.read_text(encoding="utf-8"))
        self.assertIn(self.api_url, hook.read_text(encoding="utf-8"))
        self.assertEqual(0o600, self.config.stat().st_mode & 0o777)
        self.assertEqual("main", load_config(self.config)["brains"]["prj-demo"]["canonical_branch"])

    def test_feature_branch_with_hundreds_of_files_never_changes_brain(self) -> None:
        self.register(threshold=5)
        initial = self.sync()
        self.assertEqual("fresh", initial["status"])
        canonical = initial["commit"]
        self.git("checkout", "-q", "-b", "refactor/huge")
        for index in range(150):
            (self.repo / f"module_{index}.py").write_text(f"VALUE = {index}\n", encoding="utf-8")
        self.commit("huge branch refactor")
        result = self.sync()
        self.assertTrue(result["noop"])
        self.assertEqual(canonical, result["commit"])
        self.assertFalse((self.brain / "raw" / "sources" / "demo-repo" / "module_149.py").exists())

    def test_large_main_merge_reconciles_all_files_and_graph_canary(self) -> None:
        self.register(threshold=5)
        self.sync()
        self.git("checkout", "-q", "-b", "refactor/huge")
        for index in range(12):
            (self.repo / f"part_{index}.ts").write_text(f"export const part{index} = {index}\n", encoding="utf-8")
        self.commit("large refactor")
        self.git("checkout", "-q", "main")
        self.git("merge", "--no-ff", "-m", "merge refactor", "refactor/huge")
        result = self.sync()
        self.assertEqual("reconcile", result["mode"])
        self.assertGreaterEqual(result["changed_files"], 12)
        self.assertTrue(result["evidence"]["passed"])
        self.assertTrue((self.brain / "wiki" / "sources" / "demo-repo" / "part_11.ts.md").is_file())
        report = doctor_brains(
            project_id="prj-demo", config_path=self.config, state_path=self.state,
            api_url=self.api_url,
        )
        self.assertTrue(report["ok"])

    def test_small_main_change_is_incremental_and_deletion_is_pruned(self) -> None:
        self.register(threshold=50)
        self.sync()
        (self.repo / "small.ts").write_text("export const small = true\n", encoding="utf-8")
        self.commit("small")
        added = self.sync()
        self.assertEqual("incremental", added["mode"])
        source = self.brain / "raw" / "sources" / "demo-repo" / "small.ts"
        page = self.brain / "wiki" / "sources" / "demo-repo" / "small.ts.md"
        self.assertTrue(source.is_file())
        source.unlink()  # foolish local tampering is repaired by force reconciliation
        stale = doctor_brains(
            project_id="prj-demo", config_path=self.config, state_path=self.state,
            api_url=self.api_url, deep=True,
        )
        self.assertFalse(stale["ok"])
        self.assertEqual(1, stale["brains"][0]["materialization"]["missing"])
        repaired = self.sync(force=True)
        self.assertEqual("reconcile", repaired["mode"])
        self.assertTrue(source.is_file())
        self.assertTrue(doctor_brains(
            project_id="prj-demo", config_path=self.config, state_path=self.state,
            api_url=self.api_url, deep=True,
        )["ok"])
        (self.repo / "small.ts").unlink()
        self.commit("delete small")
        deleted = self.sync()
        self.assertEqual("incremental", deleted["mode"])
        self.assertFalse(source.exists())
        self.assertFalse(page.exists())

    def test_secret_bearing_and_binary_files_are_never_materialized(self) -> None:
        (self.repo / ".env").write_text("API_KEY=do-not-copy\n", encoding="utf-8")
        (self.repo / "fixture.txt").write_text("token=sk-abcdefghijklmnopqrstuv\n", encoding="utf-8")
        (self.repo / "blob.txt").write_bytes(b"abc\x00def")
        self.commit("unsafe fixtures")
        self.register()
        result = self.sync()
        target = self.brain / "raw" / "sources" / "demo-repo"
        self.assertFalse((target / ".env").exists())
        self.assertFalse((target / "fixture.txt").exists())
        self.assertFalse((target / "blob.txt").exists())
        self.assertGreaterEqual(result["materialized"]["secret_skipped"], 2)

    def test_failed_refresh_does_not_claim_fresh_state(self) -> None:
        self.register(refresh=["/bin/sh", "-c", "exit 7"])
        with self.assertRaisesRegex(ValueError, "refresh command failed"):
            self.sync()
        self.assertFalse(self.state.exists())

    def test_concurrent_foolish_sync_requests_serialize_without_corruption(self) -> None:
        self.register()
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: self.sync(), range(8)))
        self.assertTrue(all(item["status"] == "fresh" for item in results))
        self.assertEqual(1, len([item for item in results if not item.get("noop")]))
        json.loads(self.state.read_text(encoding="utf-8"))

    def test_malformed_config_and_invalid_project_id_fail_cleanly(self) -> None:
        self.config.write_text("{broken", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "invalid JSON"):
            load_config(self.config)
        with self.assertRaisesRegex(ValueError, "project id"):
            register_brain("../escape", self.repo, "main", api_url=self.api_url, config_path=self.config)
        self.assertFalse(allowed_path("docs/bad\nname.md"))


if __name__ == "__main__":
    unittest.main()

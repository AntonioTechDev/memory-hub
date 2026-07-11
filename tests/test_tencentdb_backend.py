from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "agent_memory.py"
sys.path.insert(0, str(ROOT / "scripts"))

from tencentdb_backend import BackendError, TencentDBBackend  # noqa: E402


class GatewayHandler(BaseHTTPRequestHandler):
    calls: list[dict] = []

    def log_message(self, *_: object) -> None:
        pass

    def _write(self, status: int, payload: dict) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        self.__class__.calls.append({"method": "GET", "path": self.path, "authorization": self.headers.get("Authorization")})
        self._write(200, {"status": "ok", "version": "test", "stores": {"vectorStore": True, "embeddingService": False}})

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.__class__.calls.append(
            {"method": "POST", "path": self.path, "authorization": self.headers.get("Authorization"), "body": body}
        )
        if self.path == "/recall":
            if "no-l1" in body["query"]:
                self._write(200, {"context": "", "memory_count": 0})
            else:
                self._write(200, {"context": f"semantic::{body['query']}", "memory_count": 1})
        elif self.path == "/capture":
            self._write(200, {"l0_recorded": 2, "scheduler_notified": True})
        elif self.path == "/search/memories":
            self._write(200, {"results": "memory-hit", "total": 1, "strategy": "keyword"})
        elif self.path == "/search/conversations":
            self._write(200, {"results": "conversation-hit", "total": 1})
        elif self.path == "/session/end":
            self._write(200, {"flushed": True})
        else:
            self._write(404, {"error": "not found"})


class TencentDBBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        GatewayHandler.calls = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), GatewayHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / ".agent-memory").mkdir()
        self.cli("init", "--project-id", "phoenix", "--force")

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def env(self, url: str | None = None) -> dict[str, str]:
        return {
            "AGENT_MEMORY_BACKEND": "tencentdb",
            "AGENT_MEMORY_TENCENT_URL": url or self.url,
            "AGENT_MEMORY_TENCENT_API_KEY": "test-bearer",
            "AGENT_MEMORY_USER_ID": "owner-1",
        }

    def cli(self, *args: str, payload: dict | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *args],
            cwd=self.root,
            input=json.dumps(payload) if payload is not None else None,
            capture_output=True,
            text=True,
            check=True,
            env={**os.environ, **(env or {})},
        )

    def test_client_contract_and_bearer_auth(self) -> None:
        backend = TencentDBBackend(self.url, api_key="test-bearer")
        self.assertEqual("ok", backend.health()["status"])
        self.assertIn("semantic::resume", backend.recall("resume", "phoenix", "owner-1"))
        self.assertEqual(2, backend.capture("u", "a", "phoenix", "s1", "owner-1")["l0_recorded"])
        self.assertEqual(1, backend.search_memories("deploy")["total"])
        self.assertEqual(1, backend.search_conversations("deploy", "phoenix")["total"])
        self.assertTrue(backend.end_session("phoenix", "owner-1")["flushed"])
        self.assertTrue(all(call["authorization"] == "Bearer test-bearer" for call in GatewayHandler.calls))

    def test_rejects_insecure_remote_configuration(self) -> None:
        with self.assertRaises(BackendError):
            TencentDBBackend("http://192.0.2.20:8420", api_key="key")
        with self.assertRaises(BackendError):
            TencentDBBackend("https://memory.example.com", api_key=None)

    def test_hooks_recall_and_capture_across_agents(self) -> None:
        self.cli(
            "checkpoint", "--actor", "claude-code", "--status", "in_progress",
            "--objective", "Deploy Phoenix", "--summary", "Unit ready", "--next-action", "Run health check",
        )
        start = self.cli(
            "hook", "--event", "session-start", "--actor", "codex",
            payload={"thread-id": "codex-1"}, env=self.env(),
        )
        self.assertIn("TENCENTDB SEMANTIC RECALL", start.stdout)
        self.assertIn("Deploy Phoenix", start.stdout)

        prompt = self.cli(
            "hook", "--event", "user-prompt", "--actor", "codex",
            payload={"thread-id": "codex-1", "prompt": "Continue the health check"}, env=self.env(),
        )
        self.assertIn("semantic::Continue the health check", prompt.stdout)
        self.cli(
            "hook", "--event", "stop", "--actor", "codex",
            payload={"thread-id": "codex-1", "last-assistant-message": "Health check passed"}, env=self.env(),
        )
        capture = next(call for call in GatewayHandler.calls if call["path"] == "/capture")
        self.assertEqual("Continue the health check", capture["body"]["user_content"])
        self.assertEqual("Health check passed", capture["body"]["assistant_content"])
        self.assertEqual("phoenix", capture["body"]["session_key"])
        self.assertEqual("owner-1", capture["body"]["user_id"])

    def test_backend_outage_is_fail_open_for_agent_hook(self) -> None:
        result = self.cli(
            "hook", "--event", "session-start", "--actor", "claude-code",
            payload={"session_id": "claude-1"}, env=self.env("http://127.0.0.1:1"),
        )
        self.assertIn("SHARED AGENT HANDOFF", result.stdout)
        self.assertIn("TencentDB session-start skipped", result.stderr)
        history = (self.root / ".agent-memory" / "private" / "events.jsonl").read_text()
        self.assertIn('"type":"backend-error"', history)

    def test_hook_falls_back_to_raw_conversation_search_without_l1(self) -> None:
        result = self.cli(
            "hook", "--event", "user-prompt", "--actor", "codex",
            payload={"thread-id": "codex-2", "prompt": "no-l1 canary"}, env=self.env(),
        )
        self.assertIn("conversation-hit", result.stdout)
        self.assertTrue(any(call["path"] == "/search/conversations" for call in GatewayHandler.calls))


if __name__ == "__main__":
    unittest.main()

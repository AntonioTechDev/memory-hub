#!/usr/bin/env python3
"""Stress SQLite concurrency and secret redaction without model calls."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from memoryhub.core import MemoryStore  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=int, default=1000)
    parser.add_argument("--workers", type=int, default=32)
    parser.add_argument(
        "--report", type=Path, default=ROOT / "evals" / "latest-local-stress-report.json"
    )
    args = parser.parse_args()
    if args.events < 1 or args.workers < 1:
        parser.error("--events and --workers must be positive")

    started = time.monotonic()
    write_latencies: list[float] = []
    with tempfile.TemporaryDirectory(prefix="memoryhub-stress-") as temp:
        root = Path(temp)
        store = MemoryStore(root / "memory.db")
        store.initialize()
        workspace = store.ensure_workspace(root)
        task_id = store.create_task(workspace["id"], "stress", "Stress validation")

        def write(index: int) -> None:
            write_started = time.monotonic()
            canary = f"canary-secret-{index:04d}"
            store.append_event(
                event_type="stress",
                actor=f"worker-{index % args.workers}",
                current_session_id=f"stress-{index % args.workers}",
                workspace_id=workspace["id"],
                task_id=task_id,
                content_text=f"api_key={canary}",
                payload={"event_id": f"stress-{index}", "message": f"token={canary}"},
            )
            write_latencies.append(time.monotonic() - write_started)

        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            list(pool.map(write, range(args.events)))

        with sqlite3.connect(store.db_path) as db:
            count = int(db.execute("SELECT COUNT(*) FROM events WHERE type='stress'").fetchone()[0])
            distinct_count = int(
                db.execute("SELECT COUNT(DISTINCT dedupe_key) FROM events WHERE type='stress'").fetchone()[0]
            )
            leaked = int(
                db.execute(
                    """
                    SELECT COUNT(*) FROM events
                    WHERE type='stress'
                      AND (content_text LIKE '%canary-secret-%' OR content_json LIKE '%canary-secret-%')
                    """
                ).fetchone()[0]
            )
            integrity = str(db.execute("PRAGMA integrity_check").fetchone()[0])
        mode = oct(store.db_path.stat().st_mode & 0o777)
        context_latencies: list[float] = []
        for _ in range(200):
            context_started = time.monotonic()
            store.render_context(cwd=root, task_id=task_id)
            context_latencies.append(time.monotonic() - context_started)

    def percentile95(values: list[float]) -> float:
        ordered = sorted(values)
        return ordered[max(0, int(len(ordered) * 0.95) - 1)]

    write_p95 = percentile95(write_latencies)
    context_p95 = percentile95(context_latencies)

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "passed": count == args.events
        and distinct_count == args.events
        and leaked == 0
        and integrity == "ok"
        and mode == "0o600"
        and write_p95 < 0.300
        and context_p95 < 2.0,
        "requested_events": args.events,
        "stored_events": count,
        "distinct_events": distinct_count,
        "raw_secret_leaks": leaked,
        "sqlite_integrity": integrity,
        "database_mode": mode,
        "write_p95_ms": round(write_p95 * 1000, 3),
        "context_p95_ms": round(context_p95 * 1000, 3),
        "write_p95_target_ms": 300,
        "context_p95_target_ms": 2000,
        "duration_seconds": round(time.monotonic() - started, 3),
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Tests for the NDJSON stdio RPC service (rpc.py).

These drive a real ``python -m atom_memory.rpc`` child process the way the
dsh plugin does, so they assert the wire protocol end to end without any Node
dependency. matplotlib-free; uses only stdlib subprocess.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

import pytest

# The RPC host must not try to load a real embedding model in CI; the default
# library path would. We exercise non-embedding methods and the lifecycle here,
# plus add() which enqueues without embedding (the worker would embed, so we
# only assert the enqueue response, not persistence).
ADDR = [sys.executable, "-m", "atom_memory.rpc"]


@pytest.fixture
def proc(tmp_path):
    """Spawn an RPC child process pointing at a temp database."""
    env = {"PYTHONIOENCODING": "utf-8"}
    p = subprocess.Popen(
        ADDR,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        env={**os.environ, **env},
        cwd=str(tmp_path),
    )
    yield p
    # Tear down gracefully: send stop, wait briefly, then kill.
    try:
        p.stdin.write('{"id": "s", "method": "stop"}\n')
        p.stdin.flush()
    except (BrokenPipeError, OSError):
        pass
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def _send(p, rid, method, params=None, flush=True):
    line = json.dumps({"id": rid, "method": method, "params": params or {}}, ensure_ascii=False)
    p.stdin.write(line + "\n")
    if flush:
        p.stdin.flush()


def _recv(p, timeout=15):
    """Read one NDJSON response line from stdout."""
    line = p.stdout.readline()
    deadline = time.time() + timeout
    while not line.strip() and time.time() < deadline:
        if p.poll() is not None:
            break
        line = p.stdout.readline()
    return json.loads(line) if line.strip() else None


def test_lifecycle_and_unknown_method(proc, tmp_path):
    p = proc
    db = str(tmp_path / "rpc.db")
    _send(p, 1, "start", {"db_path": db, "worker_poll_interval_sec": 0.05})
    resp = _recv(p)
    assert resp["id"] == 1 and resp["ok"] is True
    assert resp["result"]["started"] is True

    # health reports started
    _send(p, 2, "health")
    assert _recv(p)["result"]["started"] is True

    # unknown method -> ok false, error
    _send(p, 3, "nope")
    resp = _recv(p)
    assert resp["ok"] is False and "unknown method" in resp["error"]

    # stats works on an idle user
    _send(p, 4, "stats", {"user_id": "u1"})
    resp = _recv(p)
    assert resp["ok"] is True and resp["result"]["facts"] == 0


def test_add_enqueues_and_recall_after_start(proc, tmp_path):
    p = proc
    db = str(tmp_path / "rpc2.db")
    _send(p, 1, "start", {"db_path": db, "worker_poll_interval_sec": 0.05})
    assert _recv(p)["ok"] is True

    _send(p, 2, "add", {"user_id": "u1", "session_id": "s1", "text": "用户喜欢黑咖啡"})
    resp = _recv(p)
    assert resp["ok"] is True
    assert resp["result"]["status"] == "pending"
    assert "candidate_id" in resp["result"]

    # recall returns a structured result (facts may be empty while embedding
    # would run in the worker; we assert the shape, not the contents).
    _send(p, 3, "recall", {"user_id": "u1", "query": "咖啡"})
    resp = _recv(p)
    assert resp["ok"] is True
    for key in ("facts", "summaries", "pending", "token_count", "trace_id"):
        assert key in resp["result"]


def test_bad_arguments_reported(proc, tmp_path):
    p = proc
    _send(p, 1, "start", {"db_path": str(tmp_path / "bad.db")})
    assert _recv(p)["ok"] is True
    # call with a bogus kwarg for a method
    _send(p, 2, "stats", {"user_id": "u1", "bogus": 1})
    resp = _recv(p)
    assert resp["ok"] is False and "bad arguments" in resp["error"]


def test_method_before_start_errors(proc, tmp_path):
    p = proc
    # Without a start, an ordinary method must fail cleanly.
    _send(p, 1, "recall", {"user_id": "u1", "query": "x"})
    resp = _recv(p)
    assert resp["ok"] is False and "start" in resp["error"]


def test_invalid_json_line(proc, tmp_path):
    p = proc
    p.stdin.write("{not json\n")
    p.stdin.flush()
    resp = _recv(p)
    assert resp["ok"] is False and "invalid JSON" in resp["error"]


def test_persist_candidates_persists_type_and_content(proc, tmp_path):
    """LLM-style pre-extracted candidates persist with type + content via RPC."""
    p = proc
    db = str(tmp_path / "rpc3.db")
    _send(p, 1, "start", {"db_path": db, "worker_poll_interval_sec": 0.05})
    assert _recv(p)["ok"] is True

    _send(p, 2, "persist_candidates", {
        "user_id": "u1",
        "session_id": "s1",
        "turn_id": 1,
        "candidates": [
            {
                "subject": "用户",
                "predicate": "教训",
                "object": "先备份再升级",
                "type": "lesson",
                "content": "升级任何生产依赖前先做完整备份",
            },
            {
                "subject": "用户",
                "predicate": "偏好",
                "object": "黑咖啡",
                "type": "semantic",
            },
        ],
    })
    resp = _recv(p)
    assert resp["ok"] is True and resp["result"]["queued"] == 2
    assert "candidate_id" in resp["result"]

    # give the worker a moment to drain, then inspect the persisted facts
    import time as _time
    _time.sleep(1.0)
    _send(p, 3, "stats", {"user_id": "u1"})
    resp = _recv(p)
    assert resp["ok"] is True and resp["result"]["facts"] == 2

    # verify type/content survived in recall's fact dict
    _send(p, 4, "recall", {"user_id": "u1", "query": "升级"})
    resp = _recv(p)
    assert resp["ok"] is True
    lessons = [f for f in resp["result"]["facts"] if f.get("type") == "lesson"]
    assert len(lessons) == 1
    assert lessons[0]["content"] == "升级任何生产依赖前先做完整备份"


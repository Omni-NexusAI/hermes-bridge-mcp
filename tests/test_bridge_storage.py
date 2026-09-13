"""Durability regressions; every process uses test-owned temporary files."""
import multiprocessing
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from agent_bridge_storage import AtomicJsonStore, DurableTaskStore, StorageError, association_key, process_alive, _windows_process_alive
from agent_bridge_universal import UniversalAgentRegistry


def _increment(path, count):
    store = AtomicJsonStore(Path(path))
    for _ in range(count):
        store.mutate(lambda data: data.update(count=data.get("count", 0) + 1))


def test_process_writers_do_not_lose_updates(tmp_path):
    path = tmp_path / "shared.json"
    processes = [multiprocessing.get_context("spawn").Process(target=_increment, args=(str(path), 15)) for _ in range(3)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(20)
        assert process.exitcode == 0
    assert AtomicJsonStore(path).read()["count"] == 45


def test_corruption_does_not_reset_delivery_history(tmp_path):
    path = tmp_path / "shared.json"
    path.write_text("broken", encoding="utf-8")
    with pytest.raises(StorageError):
        AtomicJsonStore(path).mutate(lambda data: data.update(replayed=True))
    assert path.read_text() == "broken"


def test_idempotency_and_interruption_never_redeliver(tmp_path):
    store = DurableTaskStore(tmp_path / "tasks.json")
    first, created = store.reserve("one", request_key="caller:request", request_fingerprint="payload")
    assert created and first["status"] == "queued"
    second, created = store.reserve("two", request_key="caller:request", request_fingerprint="payload")
    assert not created and second["task_id"] == "one"
    with pytest.raises(StorageError):
        store.reserve("three", request_key="caller:request", request_fingerprint="changed")
    assert store.claim("one", "dead-owner")
    assert not store.claim("one", "second-owner")
    assert store.reconcile(lambda owner: False) == ["one"]
    assert not store.claim("one", "new-owner")
    assert store.get("one")["status"] == "interrupted"


def test_complete_reply_survives_restart(tmp_path):
    path = tmp_path / "tasks.json"
    store = DurableTaskStore(path)
    store.reserve("one")
    store.claim("one", "owner")
    reply = "A" * 40000 + "important final context"
    store.complete("one", {"status": "completed", "stdout": reply})
    assert DurableTaskStore(path).get("one")["result"]["stdout"] == reply


def test_unknown_owner_is_not_declared_dead(tmp_path):
    store = DurableTaskStore(tmp_path / "tasks.json")
    store.reserve("one")
    store.claim("one", "owner")
    assert store.reconcile(lambda owner: None) == []
    assert store.get("one")["status"] == "running"


def test_universal_delivery_serializes_session_lookup(tmp_path):
    registry = UniversalAgentRegistry(tmp_path / "state", tmp_path / "fake", command_probe=lambda args: "")
    registry.manifests = lambda: {"fake": {"enabled": True, "available": True, "kind": "cli"}}
    seen = []
    def execute(manifest, prompt, cwd, turns, session_id, timeout, cancel):
        seen.append(session_id)
        time.sleep(0.05)
        return {"status": "completed", "session_id": "same-session", "stdout": "reply"}
    registry._execute_cli = execute
    errors = []
    def call():
        try:
            registry.execute("fake", "hello", None, 1, "caller", "topic", 5, threading.Event())
        except Exception as exc:
            errors.append(exc)
    threads = [threading.Thread(target=call) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not errors
    assert seen == [None, "same-session", "same-session"]


def test_universal_project_context_is_separate_and_legacy_is_reused(tmp_path):
    registry = UniversalAgentRegistry(tmp_path / "state", tmp_path / "fake", command_probe=lambda args: "")
    registry.manifests = lambda: {"fake": {"enabled": True, "available": True, "kind": "cli"}}
    registry._save_session("caller|fake|topic", "old-session", tmp_path / "one")
    seen = []
    def execute(manifest, prompt, cwd, turns, session_id, timeout, cancel):
        seen.append(session_id)
        return {"status": "completed", "session_id": session_id or "new-session", "stdout": "reply"}
    registry._execute_cli = execute
    for project in ["one", "two", "two"]:
        registry.execute("fake", "hello", tmp_path / project, 1, "caller", "topic", 5, threading.Event())
    assert seen == ["old-session", None, "new-session"]


def test_large_cli_reply_is_drained_and_preserved(tmp_path, monkeypatch):
    registry = UniversalAgentRegistry(tmp_path / "state", tmp_path / "fake", command_probe=lambda args: "")
    monkeypatch.setattr(registry, "_sandbox_stub", lambda: False)
    result = registry._execute_cli({"command": [sys.executable, "-c", "print('a' * 200000 + 'FINAL_CONTEXT')"]},
                                   "", None, 1, None, 5, threading.Event())
    assert result["status"] == "completed"
    assert result["stdout"].startswith("a" * 200000)
    assert result["stdout"].rstrip().endswith("FINAL_CONTEXT")


def test_scope_includes_project_and_avoids_delimiter_collision():
    assert association_key("a|b", "c", "d", "e") != association_key("a", "b|c", "d", "e")
    assert association_key("caller", "codex", "repo-one", "topic") != association_key("caller", "codex", "repo-two", "topic")


def test_conversation_lock_can_cancel_wait(tmp_path):
    store = DurableTaskStore(tmp_path / "tasks.json")
    canceled = threading.Event()
    errors = []
    def wait():
        try:
            with store.conversation_lock("topic", cancel_event=canceled):
                pytest.fail("must not acquire while held")
        except StorageError as exc:
            errors.append(exc.code)
    with store.conversation_lock("topic"):
        thread = threading.Thread(target=wait)
        thread.start()
        canceled.set()
        thread.join(3)
        assert not thread.is_alive()
    assert errors == ["canceled"]


@pytest.mark.parametrize("handle,error,query,exit_code,expected", [
    (42, 0, True, 259, True),
    (42, 0, True, 0, False),
    (42, 0, False, 0, None),
    (None, 87, False, 0, False),
    (None, 5, False, 0, None),
    (None, 123, False, 0, None),
])
def test_windows_liveness_is_query_only(handle, error, query, exit_code, expected):
    class API:
        closed = []
        def OpenProcess(self, access, inherit, pid):
            assert access == 0x1000 and inherit is False and pid == 1234
            return handle
        def GetExitCodeProcess(self, given_handle, out):
            assert given_handle == handle
            out._obj.value = exit_code
            return query
        def CloseHandle(self, given_handle):
            self.closed.append(given_handle)
    api = API()
    assert _windows_process_alive(1234, api, lambda: error) is expected
    assert api.closed == ([handle] if handle else [])


@pytest.mark.parametrize("pid", [None, "1234", 0, -1, True, 2 ** 32])
def test_unknown_or_invalid_process_ids_preserve_uncertainty(pid):
    assert process_alive(pid) is None


def test_windows_query_initialization_failure_preserves_uncertainty(monkeypatch):
    import agent_bridge_storage as storage
    def unavailable():
        raise OSError("synthetic access failure")
    monkeypatch.setattr(storage, "_windows_process_api", unavailable)
    assert storage._windows_process_alive(1234) is None

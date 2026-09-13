import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from agent_bridge_codex import CodexConversationRouter, CodexRoutingError, OwnerAppServerRPC, repository_identity


class Owner:
    def __init__(self, threads=None):
        self.threads = {t["id"]: t for t in (threads or [])}
        self.calls = []
        self.counter = 0
        self.lost_ack = False
        self.complete = True
        self.reply = "Actual retained-context reply"
        self.steer_unsupported = False

    def request(self, method, params):
        self.calls.append((method, copy.deepcopy(params)))
        if method == "thread/list":
            return {"data": list(copy.deepcopy(self.threads).values()), "nextCursor": None}
        if method == "thread/read":
            return {"thread": copy.deepcopy(self.threads[params["threadId"]])}
        if method == "thread/start":
            self.counter += 1
            t = thread("created-" + str(self.counter), cwd=params["cwd"])
            self.threads[t["id"]] = t
            return {"thread": copy.deepcopy(t)}
        if method == "thread/resume":
            return {"thread": copy.deepcopy(self.threads[params["threadId"]])}
        if method in {"turn/start", "turn/steer"}:
            t = self.threads[params["threadId"]]
            if method == "turn/steer":
                if self.steer_unsupported:
                    raise CodexRoutingError("unsupported_capability", "Unsupported")
                assert params["expectedTurnId"] == t["turns"][-1]["id"]
                turn_id = params["expectedTurnId"]
                t["turns"].pop()
            else:
                assert not any(x["status"] == "inProgress" for x in t["turns"])
                self.counter += 1
                turn_id = "turn-" + str(self.counter)
            new_turn = {"id": turn_id, "status": "completed" if self.complete else "inProgress",
                        "items": [{"type": "agentMessage", "text": self.reply}]}
            t["turns"].append(new_turn)
            t["status"] = {"type": "idle" if self.complete else "active"}
            if self.lost_ack:
                raise CodexRoutingError("owner_connection_lost", "Acknowledgement lost")
            return {"turnId": turn_id} if method == "turn/steer" else {"turn": new_turn}
        raise AssertionError(method)

    def close(self):
        pass


def thread(identifier, preview="", cwd="D:/host/repo", status="idle"):
    return {"id": identifier, "cwd": cwd, "preview": preview, "name": "unreliable title", "status": {"type": status}, "turns": []}


def router(tmp_path, owner, **owner_options):
    config = {"owner": {"endpoint": "ws://127.0.0.1:19001", "desktop_owner": True, **owner_options},
              "projects": [{"id": "host-repo", "repository": "https://github.com/test/repo.git", "cwd": "D:/host/repo"}],
              "projectless_cwd": "D:/host/scratch"}
    return CodexConversationRouter(tmp_path, config, rpc_factory=lambda config: owner, poll_interval=0.01)


def call(r, request_id="one", **kwargs):
    return r.delegate("peer-a", "codex", request_id, kwargs.pop("prompt", "Please contribute"),
                      project=kwargs.pop("project", {"repository": "git@github.com:test/repo.git"}),
                      topic=kwargs.pop("topic", "storage concurrency"), timeout=kwargs.pop("timeout", 1), **kwargs)


def test_repository_identity_and_cross_machine_paths(tmp_path):
    owner = Owner([thread("existing", "storage concurrency decisions")])
    r = router(tmp_path, owner)
    result = call(r)
    assert result["status"] == "completed"
    assert result["conversation_id"] == "existing"
    assert result["reused"] is True
    assert result["project_id"] == "host-repo"
    assert result["reply"] == owner.reply
    assert repository_identity("git@github.com:TEST/Repo.git") == "github.com/test/repo"
    assert next(p for m, p in owner.calls if m == "thread/list")["cwd"] == "D:/host/repo"


def test_title_alone_does_not_match_and_new_is_host_project(tmp_path):
    existing = thread("wrong", "unrelated renderer issue")
    existing["name"] = "storage concurrency"
    owner = Owner([existing])
    result = call(router(tmp_path, owner))
    assert result["created"]
    assert result["desktop_project_registration"] == "unverified"
    assert next(p for m, p in owner.calls if m == "thread/start") == {"cwd": "D:/host/repo"}


def test_ambiguous_conversations_fail_without_creating(tmp_path):
    owner = Owner([thread("one", "storage concurrency"), thread("two", "storage concurrency")])
    result = call(router(tmp_path, owner))
    assert result["error_code"] == "ambiguous_conversation"
    assert result["candidates"] == ["one", "two"]
    assert not any(m in {"thread/start", "turn/start"} for m, _ in owner.calls)


def test_host_project_ambiguity_and_caller_paths_rejected(tmp_path):
    r = router(tmp_path, Owner())
    r.config["projects"].append({"id": "other", "repository": "github.com/test/repo", "cwd": "E:/other"})
    with pytest.raises(CodexRoutingError, match="Select one") as error:
        call(r)
    assert error.value.code == "ambiguous_project"
    with pytest.raises(CodexRoutingError) as error:
        call(r, project={"cwd": "C:/caller/repo"})
    assert error.value.code == "invalid_project"


def test_followup_reuses_association_with_context_delta(tmp_path):
    owner = Owner()
    r = router(tmp_path, owner)
    first = call(r, context={"decisions": ["Use atomic updates"]})
    second = call(r, "two", context={"delta": ["Concurrent case reproduced"]})
    assert second["conversation_id"] == first["conversation_id"]
    assert second["reused"]
    assert sum(m == "thread/start" for m, _ in owner.calls) == 1
    text = [p["input"][0]["text"] for m, p in owner.calls if m == "turn/start"][-1]
    assert "Concurrent case reproduced" in text
    assert "Use atomic updates" not in text


def test_idempotent_request_and_changed_input_conflict(tmp_path):
    owner = Owner()
    r = router(tmp_path, owner)
    first = call(r)
    assert call(r) == first
    assert sum(m == "turn/start" for m, _ in owner.calls) == 1
    with pytest.raises(CodexRoutingError) as error:
        call(r, prompt="Different request")
    assert error.value.code == "request_conflict"


def test_busy_queue_then_same_request_delivers_once(tmp_path):
    t = thread("busy", "storage concurrency", status="active")
    t["turns"] = [{"id": "existing-turn", "status": "inProgress", "items": []}]
    owner = Owner([t])
    r = router(tmp_path, owner)
    pending = call(r, timeout=0)
    assert pending["status"] == "queued"
    assert pending["pending_retry"]
    assert not any(m == "turn/start" for m, _ in owner.calls)
    t["status"] = {"type": "idle"}
    t["turns"][0]["status"] = "completed"
    assert call(r)["status"] == "completed"
    assert sum(m == "turn/start" for m, _ in owner.calls) == 1


@pytest.mark.parametrize("supported", [False, True])
def test_busy_steer_is_guarded_or_queued(tmp_path, supported):
    t = thread("busy", "storage concurrency", status="active")
    t["turns"] = [{"id": "active-turn", "status": "inProgress", "items": []}]
    owner = Owner([t])
    result = call(router(tmp_path, owner, supports_steer=supported), delivery_mode="steer", timeout=0)
    assert result["status"] == ("completed" if supported else "queued")
    if supported:
        params = next(p for m, p in owner.calls if m == "turn/steer")
        assert params["expectedTurnId"] == "active-turn"
        assert set(params) == {"threadId", "input", "expectedTurnId"}
    else:
        assert "unsupported" in result["delivery_reason"]


def test_owner_rejecting_steer_falls_back_to_queue(tmp_path):
    t = thread("busy", "storage concurrency", status="active")
    t["turns"] = [{"id": "active-turn", "status": "inProgress", "items": []}]
    owner = Owner([t])
    owner.steer_unsupported = True
    result = call(router(tmp_path, owner, supports_steer=True), delivery_mode="steer", timeout=0)
    assert result["status"] == "queued"
    assert result["steering_unsupported"]


def test_separate_requires_reason_and_followup_reuses_new_topic(tmp_path):
    owner = Owner([thread("old", "storage concurrency")])
    r = router(tmp_path, owner)
    with pytest.raises(CodexRoutingError):
        call(r, delivery_mode="separate")
    first = call(r, delivery_mode="separate", new_reason="Independent benchmark contribution")
    assert first["created"]
    assert call(r, "follow-up")["conversation_id"] == first["conversation_id"]


def test_projectless_context_and_association(tmp_path):
    owner = Owner([thread("projectless", "storage concurrency", cwd="D:/host/scratch")])
    r = router(tmp_path, owner)
    first = call(r, project=None)
    second = call(r, "followup", project=None)
    assert first["conversation_id"] == second["conversation_id"] == "projectless"
    assert first["project_id"] == ""


def test_projectless_creation_requires_host_cwd(tmp_path):
    r = router(tmp_path, Owner())
    del r.config["projectless_cwd"]
    result = call(r, project=None)
    assert result["error_code"] == "owner_configuration"


def test_full_reply_not_truncated_and_exact_turn_retrieved_after_restart(tmp_path):
    owner = Owner([thread("existing", "storage concurrency")])
    owner.reply = "long reply " * 4000
    owner.complete = False
    r = router(tmp_path, owner)
    started = call(r, timeout=0)
    assert started["status"] == "running"
    t = owner.threads["existing"]
    t["turns"][-1]["status"] = "completed"
    t["turns"].append({"id": "unrelated", "status": "completed", "items": [{"type": "agentMessage", "text": "Wrong reply"}]})
    restarted = router(tmp_path, owner)
    result = restarted.result("one", "peer-a")
    assert result["reply"] == owner.reply
    assert result["turn_id"] == started["turn_id"]
    assert sum(m == "turn/start" for m, _ in owner.calls) == 1


def test_lost_ack_never_resends_after_restart(tmp_path):
    owner = Owner()
    owner.lost_ack = True
    r = router(tmp_path, owner)
    assert call(r)["status"] == "delivery_uncertain"
    assert call(router(tmp_path, owner))["status"] == "delivery_uncertain"
    assert sum(m == "turn/start" for m, _ in owner.calls) == 1


def test_completed_without_reply_is_not_success(tmp_path):
    owner = Owner()
    owner.reply = ""
    result = call(router(tmp_path, owner))
    assert result["status"] == "result_unavailable"


def test_final_answer_preferred_to_commentary(tmp_path):
    owner = Owner()
    owner.complete = False
    r = router(tmp_path, owner)
    initial = call(r, timeout=0)
    turn = owner.threads[initial["conversation_id"]]["turns"][-1]
    turn.update(status="completed", items=[{"type": "agentMessage", "phase": "commentary", "text": "working"},
                                          {"type": "agentMessage", "phase": "final_answer", "text": "actual final"}])
    assert r.result("one", "peer-a")["reply"] == "actual final"


def test_result_is_caller_scoped(tmp_path):
    r = router(tmp_path, Owner())
    call(r)
    with pytest.raises(CodexRoutingError) as error:
        r.result("one", "other-peer")
    assert error.value.code == "task_not_found"


def test_no_owner_assertion_fails_without_spawning(tmp_path):
    r = CodexConversationRouter(tmp_path, {"owner": {"endpoint": "ws://127.0.0.1:19001"}})
    with pytest.raises(CodexRoutingError) as error:
        r.conversations()
    assert error.value.code == "owner_not_configured"


def test_real_transport_forbidden_by_test_guard():
    with pytest.raises(CodexRoutingError) as error:
        OwnerAppServerRPC({"endpoint": "ws://127.0.0.1:19001"})
    assert error.value.code == "sandbox_violation"


def test_wrong_project_explicit_conversation_fails(tmp_path):
    owner = Owner([thread("wrong", cwd="D:/other")])
    result = call(router(tmp_path, owner), conversation_id="wrong")
    assert result["error_code"] == "project_mismatch"


def test_pagination_includes_appserver_and_all_pages(tmp_path):
    owner = Owner([thread("right", "storage concurrency")])
    original = owner.request
    def request(method, params):
        if method == "thread/list" and "cursor" not in params:
            assert "appServer" in params["sourceKinds"]
            return {"data": [], "nextCursor": "page-two"}
        return original(method, params)
    owner.request = request
    assert call(router(tmp_path, owner))["conversation_id"] == "right"


def test_explicit_separate_overrides_existing_topic_association(tmp_path):
    owner = Owner()
    r = router(tmp_path, owner)
    first = call(r)
    second = call(r, "second", delivery_mode="separate", new_reason="Independent benchmark")
    assert second["created"]
    assert first["conversation_id"] != second["conversation_id"]


@pytest.mark.parametrize("status", [None, "systemError", "unknown"])
def test_missing_or_error_runtime_state_prevents_delivery(tmp_path, status):
    t = thread("existing", "storage concurrency")
    t["status"] = status
    owner = Owner([t])
    result = call(router(tmp_path, owner))
    assert result["status"] == "failed"
    assert not any(m == "turn/start" for m, _ in owner.calls)


def test_read_bounds_context_and_checks_project(tmp_path):
    t = thread("existing")
    t["turns"] = [{"id": "prior", "items": [{"type": "agentMessage", "text": "x" * 17000}]}]
    r = router(tmp_path, Owner([t]))
    result = r.read("existing", {"id": "host-repo"})
    assert len(result["context"]) == 16000
    assert result["context_truncated"]
    with pytest.raises(CodexRoutingError):
        r.read("existing", None)


def test_request_lock_timeout_does_not_change_other_worker_state(tmp_path):
    from agent_bridge_storage import association_key, conversation_lock
    r = router(tmp_path, Owner())
    key = association_key("peer-a", "codex", "request", "one")
    with conversation_lock(tmp_path, "request:" + key):
        result = call(r, timeout=0)
    assert result["status"] == "pending"
    assert result["pending_retry"]
    assert call(r)["status"] == "completed"


def test_parallel_duplicate_requests_create_and_deliver_once(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    owner = Owner()
    r = router(tmp_path, owner)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: call(r), range(4)))
    assert all(x["status"] == "completed" for x in results)
    assert len({x["conversation_id"] for x in results}) == 1
    assert sum(m == "thread/start" for m, _ in owner.calls) == 1
    assert sum(m == "turn/start" for m, _ in owner.calls) == 1


def test_parallel_same_topic_requests_reuse_one_conversation(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    owner = Owner()
    r = router(tmp_path, owner)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda number: call(r, str(number), timeout=3), range(4)))
    assert all(x["status"] == "completed" for x in results)
    assert len({x["conversation_id"] for x in results}) == 1
    assert sum(m == "thread/start" for m, _ in owner.calls) == 1
    assert sum(m == "turn/start" for m, _ in owner.calls) == 4


@pytest.mark.parametrize("already_delivered", [False, True])
def test_waiting_worker_does_not_block_explicit_steering(tmp_path, already_delivered):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    t = thread("existing", "storage concurrency", status="idle" if already_delivered else "active")
    if not already_delivered:
        t["turns"] = [{"id": "desktop-turn", "status": "inProgress", "items": []}]
    owner = Owner([t])
    owner.complete = False
    r = router(tmp_path, owner, supports_steer=True)
    cancel = threading.Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call, r, "waiting", timeout=3, cancel_event=cancel)
        deadline = time.monotonic() + 2
        expected = "running" if already_delivered else "queued"
        while time.monotonic() < deadline:
            try:
                if r.result("waiting", "peer-a")["status"] == expected:
                    break
            except CodexRoutingError:
                pass
            time.sleep(0.01)
        assert not first.done()
        second = call(r, "steering", delivery_mode="steer", timeout=0.5)
        assert second["delivery_mode"] == "steer"
        assert second["status"] == "running"
        assert any(m == "turn/steer" for m, _ in owner.calls)
        cancel.set()
        first.result(timeout=2)


def test_created_conversation_cwd_must_match_before_delivery(tmp_path):
    owner = Owner()
    original = owner.request
    def wrong_cwd(method, params):
        result = original(method, params)
        if method == "thread/start":
            result["thread"]["cwd"] = "D:/wrong-project"
        return result
    owner.request = wrong_cwd
    result = call(router(tmp_path, owner))
    assert result["error_code"] == "project_mismatch"
    assert result["conversation_id"]
    assert result["created"]
    assert not any(m == "turn/start" for m, _ in owner.calls)


def test_queued_conversation_moved_project_before_retry_is_rejected(tmp_path):
    t = thread("busy", "storage concurrency", status="active")
    t["turns"] = [{"id": "existing", "status": "inProgress", "items": []}]
    owner = Owner([t])
    r = router(tmp_path, owner)
    assert call(r, timeout=0)["status"] == "queued"
    t["cwd"] = "D:/other-project"
    t["status"] = {"type": "idle"}
    t["turns"] = []
    assert call(r)["error_code"] == "project_mismatch"
    assert not any(m == "turn/start" for m, _ in owner.calls)


def test_rpc_ignores_notifications_and_never_auto_approves():
    import json
    class Socket:
        def __init__(self):
            self.sent = []
            self.frames = iter([
                {"method": "item/agentMessage/delta", "params": {"delta": "status"}},
                {"id": 88, "method": "item/commandExecution/requestApproval", "params": {}},
                {"id": 1, "result": {"thread": {"id": "expected"}}},
            ])
        def send(self, value):
            self.sent.append(json.loads(value))
        def recv(self, timeout):
            return json.dumps(next(self.frames))
    rpc = OwnerAppServerRPC.__new__(OwnerAppServerRPC)
    rpc.sequence = 0
    rpc.server_requests = []
    rpc.socket = Socket()
    assert rpc.request("thread/read", {"threadId": "expected"})["thread"]["id"] == "expected"
    assert len(rpc.socket.sent) == 1
    assert rpc.server_requests == ["item/commandExecution/requestApproval"]

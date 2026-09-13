import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from agent_bridge_conversations import ConversationRuntime
from agent_bridge_codex import CodexRoutingError
from agent_bridge_storage import association_key
from test_codex_owner_routing import Owner, router, thread


def runtime(tmp_path, owner):
    return ConversationRuntime(tmp_path, router_factory=lambda: router(tmp_path, owner))


def start(runtime, caller="peer-a", request_id="one", **kwargs):
    return runtime.start(caller, request_id, kwargs.pop("prompt", "Private requested contribution"), "storage concurrency",
                         project={"id": "host-repo"}, timeout_seconds=1, **kwargs)


def settled(runtime):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with runtime.lock:
            workers = list(runtime.threads.values())
        if not workers:
            return
        for worker in workers:
            worker.join(timeout=0.1)
    raise AssertionError("Worker did not settle")


def busy_owner():
    t = thread("existing", "storage concurrency", status="active")
    t["turns"] = [{"id": "desktop-turn", "status": "inProgress", "items": []}]
    return Owner([t])


def make_idle(owner):
    t = owner.threads["existing"]
    t["status"] = {"type": "idle"}
    t["turns"][-1]["status"] = "completed"


def test_result_poll_continues_durable_queue_without_duplicate_start(tmp_path):
    owner = busy_owner()
    rt = runtime(tmp_path, owner)
    task = start(rt)
    settled(rt)
    assert rt.jobs.read()[task["task_id"]]["status"] == "queued"
    make_idle(owner)
    rt.result("peer-a", task["task_id"])
    settled(rt)
    assert rt.result("peer-a", task["task_id"])["reply"] == owner.reply
    assert sum(m == "turn/start" for m, _ in owner.calls) == 1


def test_startup_recovers_queue_from_private_payload(tmp_path):
    owner = busy_owner()
    first = runtime(tmp_path, owner)
    task = start(first, context={"delta": "Private delta not to expose"})
    settled(first)
    make_idle(owner)
    restarted = runtime(tmp_path, owner)
    assert restarted.recover_pending()["scheduled"] == [task["task_id"]]
    settled(restarted)
    result = restarted.result("peer-a", task["task_id"])
    assert result["status"] == "completed"
    assert "Private delta not to expose" not in json.dumps(result)
    params = next(p for m, p in owner.calls if m == "turn/start")
    assert "Private delta not to expose" in params["input"][0]["text"]


@pytest.mark.parametrize("router_status", ["sending", "creating_conversation", "delivery_uncertain"])
def test_startup_never_replays_ambiguous_router_record(tmp_path, router_status):
    owner = busy_owner()
    first = runtime(tmp_path, owner)
    task = start(first)
    settled(first)
    r = first.router()
    key = association_key("peer-a", "codex", "request", task["task_id"])
    r.store.mutate(lambda data: data["requests"][key].update(status=router_status))
    make_idle(owner)
    restarted = runtime(tmp_path, owner)
    restarted.recover_pending()
    settled(restarted)
    assert restarted.result("peer-a", task["task_id"])["status"] == "delivery_uncertain"
    assert not any(m == "turn/start" for m, _ in owner.calls)


def test_accepted_result_reconciles_after_wrapper_loses_status(tmp_path):
    owner = Owner()
    first = runtime(tmp_path, owner)
    task = start(first)
    settled(first)
    first.jobs.mutate(lambda data: data[task["task_id"]].update(status="pending"))
    restarted = runtime(tmp_path, owner)
    restarted.recover_pending()
    settled(restarted)
    assert restarted.result("peer-a", task["task_id"])["status"] == "completed"
    assert sum(m == "turn/start" for m, _ in owner.calls) == 1


def test_payload_context_and_caller_are_private_in_every_result(tmp_path):
    rt = runtime(tmp_path, Owner())
    task = start(rt, context={"constraints": "PRIVATE"})
    assert "_payload" not in task
    assert "PRIVATE" not in json.dumps(task)
    settled(rt)
    result = rt.result("peer-a", task["task_id"])
    preview = rt.preview(rt.jobs.read()[task["task_id"]])
    for public in (result, preview):
        assert "_payload" not in public
        assert "caller" not in public
        assert "fingerprint" not in public
        assert "PRIVATE" not in json.dumps(public)


def test_complete_reply_survives_missing_config_and_bounded_preview(tmp_path):
    owner = Owner()
    owner.reply = "Z" * 25000
    rt = runtime(tmp_path, owner)
    task = start(rt)
    settled(rt)
    without_config = ConversationRuntime(tmp_path)
    result = without_config.result("peer-a", task["task_id"])
    assert result["reply"] == owner.reply
    preview = without_config.preview(result)
    assert len(preview["reply"]) == 8000
    assert preview["reply_truncated"]


def test_request_ids_are_peer_scoped_and_conflicts_stable(tmp_path):
    rt = runtime(tmp_path, Owner())
    one = start(rt)
    same = start(rt)
    two = start(rt, caller="peer-b")
    assert one["task_id"] == same["task_id"]
    assert two["task_id"] != one["task_id"]
    with pytest.raises(CodexRoutingError) as error:
        start(rt, prompt="Changed input")
    assert error.value.code == "request_conflict"
    with pytest.raises(CodexRoutingError):
        rt.result("peer-b", one["task_id"])
    settled(rt)


def test_missing_payload_never_invents_input(tmp_path):
    owner = busy_owner()
    first = runtime(tmp_path, owner)
    task = start(first)
    settled(first)
    first.jobs.mutate(lambda data: data[task["task_id"]].pop("_payload"))
    make_idle(owner)
    restarted = runtime(tmp_path, owner)
    restarted.recover_pending()
    settled(restarted)
    saved = restarted.jobs.read()[task["task_id"]]
    assert saved["error_code"] == "recovery_input_missing"
    assert not any(m == "turn/start" for m, _ in owner.calls)


def test_cancel_durable_queue_prevents_future_poll_delivery(tmp_path):
    owner = busy_owner()
    rt = runtime(tmp_path, owner)
    task = start(rt)
    settled(rt)
    assert rt.cancel("peer-a", task["task_id"])["status"] == "canceled"
    make_idle(owner)
    restarted = runtime(tmp_path, owner)
    assert restarted.recover_pending()["scheduled"] == []
    assert restarted.result("peer-a", task["task_id"])["status"] == "canceled"
    assert not any(m == "turn/start" for m, _ in owner.calls)


def test_cancel_accepted_turn_never_interrupts_owner(tmp_path):
    owner = Owner()
    owner.complete = False
    rt = runtime(tmp_path, owner)
    task = start(rt)
    settled(rt)
    canceled = rt.cancel("peer-a", task["task_id"])
    assert canceled["status"] == "running"
    assert "remains intact" in canceled["action"]
    assert not any(m == "turn/interrupt" for m, _ in owner.calls)


def test_two_runtime_instances_recover_same_queue_without_duplicate_delivery(tmp_path):
    owner = busy_owner()
    original = runtime(tmp_path, owner)
    task = start(original)
    settled(original)
    make_idle(owner)
    first, second = runtime(tmp_path, owner), runtime(tmp_path, owner)
    first.recover_pending()
    second.recover_pending()
    settled(first)
    settled(second)
    assert first.result("peer-a", task["task_id"])["status"] == "completed"
    assert sum(m == "turn/start" for m, _ in owner.calls) == 1

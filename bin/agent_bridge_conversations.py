"""Optional conversation routing tools; legacy and universal schemas stay intact."""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Optional

from agent_bridge_codex import CodexConversationRouter, CodexRoutingError
from agent_bridge_storage import AtomicJsonStore, StorageError, conversation_lock

EXTENSION = "conversation_routing_v1"
TOOLS = tuple("bridge_" + side + "_" + name for side in ("agent", "peer")
              for name in ("projects_list", "conversations_list", "conversation_read", "routed_delegate_start", "complete_result"))
RESULT_TOOLS = tuple(name for name in TOOLS if name.endswith("_complete_result"))


def owner_routing_enabled():
    return os.environ.get("AGENT_BRIDGE_ENABLE_CODEX_OWNER_ROUTING",
                          os.environ.get("HERMES_BRIDGE_ENABLE_CODEX_OWNER_ROUTING", "0")) == "1"


def advertised_tools():
    return TOOLS if owner_routing_enabled() else RESULT_TOOLS


class ConversationRuntime:
    def __init__(self, state_dir, router_factory=None):
        self.state_dir = Path(state_dir)
        self.jobs = AtomicJsonStore(self.state_dir / "routed-jobs.json")
        self.threads = {}
        self.lock = threading.Lock()
        self.router_factory = router_factory

    def router(self):
        if self.router_factory is not None:
            return self.router_factory()
        if not owner_routing_enabled():
            raise CodexRoutingError("integration_disabled", "Codex Desktop owner routing is unfinished and disabled. Use native Codex Desktop connections; retained bridge-managed CLI sessions are not Desktop attachment.")
        path = Path(os.environ.get("AGENT_BRIDGE_CODEX_OWNER_CONFIG") or
                    os.environ.get("HERMES_BRIDGE_CODEX_OWNER_CONFIG") or self.state_dir / "codex-owner.json")
        if not path.exists():
            raise CodexRoutingError("owner_configuration", "Configure codex-owner.json with the existing desktop owner's supported endpoint and host-owned project mappings")
        return CodexConversationRouter(self.state_dir, AtomicJsonStore(path).read())

    def capabilities(self):
        if self.router_factory is None and not owner_routing_enabled():
            return {"extension": EXTENSION, "configured": False, "enabled": False,
                    "support_status": "unfinished", "error": "integration_disabled",
                    "message": "Use native Codex Desktop connections for existing Desktop tasks.",
                    "tools": [], "result_tools": list(RESULT_TOOLS)}
        try:
            router = self.router()
            router._owner()
            return {"extension": EXTENSION, "configured": True, "agents": ["codex"],
                    "owner_reachability": "not_probed", "owner_verification": "host_operator_assertion",
                    "desktop_project_registration": "unverified", "tools": list(TOOLS)}
        except (CodexRoutingError, StorageError) as exc:
            return {"extension": EXTENSION, "configured": False, "error": exc.code, "message": str(exc), "tools": list(TOOLS)}

    def _save(self, task_id, value):
        def save(data):
            record = data.setdefault(task_id, {})
            if record.get("status") in {"completed", "canceled"}:
                return dict(record)
            record.update(value)
            if record.get("status") == "completed":
                record.pop("message", None)
                record.pop("cancel_requested", None)
            return dict(record)
        return self.jobs.mutate(save)

    def start(self, caller, request_id, prompt, topic, project=None, context=None,
              conversation_id=None, delivery_mode="queue", new_reason=None, timeout_seconds=300):
        if not isinstance(request_id, str) or not request_id.strip():
            raise CodexRoutingError("invalid_request", "A stable request_id is required; reuse it for retries and use a new id for a follow-up")
        if not isinstance(timeout_seconds, (int, float)) or isinstance(timeout_seconds, bool) or not 1 <= timeout_seconds <= 86400:
            raise CodexRoutingError("invalid_request", "timeout_seconds must be between 1 and 86400")
        router = self.router()
        router._owner()
        task_id = "codex-" + hashlib.sha256(json.dumps([caller, request_id]).encode()).hexdigest()
        payload = dict(caller=caller, agent_id="codex", request_id=task_id, prompt=prompt, topic=topic,
                       project=project, context=context, conversation_id=conversation_id,
                       delivery_mode=delivery_mode, new_reason=new_reason, timeout=timeout_seconds)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        def reserve(data):
            if task_id in data and data[task_id].get("fingerprint") != fingerprint:
                raise CodexRoutingError("request_conflict", "request_id already names different input")
            record = data.setdefault(task_id, {"task_id": task_id, "status": "pending", "caller": caller,
                                               "fingerprint": fingerprint, "_payload": payload})
            # Identical explicit retries can restore pre-upgrade queued records
            # lacking a payload, but cannot alter already accepted requests.
            if record.get("status") in {"pending", "queued"}:
                record.setdefault("_payload", payload)
            return dict(record)
        previous = self.jobs.mutate(reserve)
        if previous["status"] in {"pending", "queued"}:
            self._launch(task_id)
        return self.preview(previous)

    def _launch(self, task_id):
        """Schedule one worker; the process lock arbitrates other bridge hosts."""
        with self.lock:
            if task_id in self.threads and self.threads[task_id].is_alive():
                return False
            def run():
                try:
                    # Never let two bridge processes own the same worker. The
                    # router adds its own durable send/ack guard underneath.
                    with conversation_lock(self.state_dir, "routed-worker:" + task_id, timeout=0):
                        record = self.jobs.read()[task_id]
                        if record.get("status") not in {"pending", "queued"}:
                            return
                        router = self.router()
                        try:
                            actual = router.result(task_id, record["caller"])
                        except CodexRoutingError as exc:
                            if exc.code != "task_not_found":
                                raise
                            actual = {"status": "pending"}
                        if actual.get("status") not in {"pending", "queued"}:
                            self._save(task_id, actual)
                            return
                        if record.get("_cancel_requested"):
                            self._save(task_id, {"status": "canceled", "action": "Canceled before delivery"})
                            return
                        payload = record.get("_payload")
                        if not payload:
                            self._save(task_id, {"error_code": "recovery_input_missing", "action": "Retry the original request with identical input to restore this queued record"})
                            return
                        runtime = self
                        class CancelSignal:
                            def is_set(self):
                                return bool(runtime.jobs.read().get(task_id, {}).get("_cancel_requested"))
                        result = router.delegate(**payload, cancel_event=CancelSignal(),
                                                 on_selected=lambda route: self._save(task_id, route))
                        if result.get("status") in {"pending", "queued"}:
                            result["action"] = "Queued durably; polling the result resumes safe delivery"
                        self._save(task_id, result)
                except (CodexRoutingError, StorageError) as exc:
                    if isinstance(exc, StorageError) and exc.code == "lock_timeout":
                        return
                    # Keep pending work recoverable on temporary owner/config
                    # failure. Router state decides whether any send occurred.
                    self._save(task_id, {"error_code": exc.code, "message": str(exc),
                                         "action": "Restore access to the configured owner, then poll this same task; do not submit duplicate work"})
                except Exception:
                    # Do not leak credentials or owner transport diagnostics.
                    self._save(task_id, {"status": "delivery_uncertain", "error": "routing_error",
                                        "message": "Inspect the owning server and reconcile this request before resending"})
                finally:
                    with self.lock:
                        self.threads.pop(task_id, None)
            thread = threading.Thread(target=run, daemon=True, name="bridge-route-" + task_id[-8:])
            self.threads[task_id] = thread
            thread.start()
        return True

    def recover_pending(self):
        """Startup recovery: only pending/queued jobs are scheduled for inspection.

        Workers reconcile the router record before doing anything. A persisted
        sending/creating/unknown outcome never re-enters delivery.
        """
        if self.router_factory is None and not owner_routing_enabled():
            return {"scheduled": []}
        scheduled = []
        for task_id, record in self.jobs.read().items():
            if record.get("status") in {"pending", "queued"} and self._launch(task_id):
                scheduled.append(task_id)
        return {"scheduled": scheduled}

    @staticmethod
    def public(record):
        return {key: value for key, value in record.items() if key not in {"caller", "fingerprint"} and not key.startswith("_")}

    @staticmethod
    def preview(record):
        public = ConversationRuntime.public(record)
        if "reply" in public:
            public["reply_length"] = len(public["reply"])
            public["reply_truncated"] = len(public["reply"]) > 8000
            public["reply"] = public["reply"][:8000]
        return public

    def result(self, caller, task_id):
        record = self.jobs.read().get(task_id)
        if not record or record.get("caller") != caller:
            raise CodexRoutingError("task_not_found", "No routed task exists for this caller")
        if record.get("status") in {"completed", "canceled"}:
            return self.public(record)
        if self.router_factory is None and not owner_routing_enabled():
            return self.public(dict(record, error_code="integration_disabled",
                                    action="Unfinished Desktop integration is disabled; retained work was not resumed or resent."))
        try:
            result = self.router().result(task_id, caller)
            record = self._save(task_id, result)
        except CodexRoutingError as exc:
            if exc.code != "task_not_found":
                record = self._save(task_id, {"error_code": exc.code, "message": str(exc),
                                              "action": "Restore access to the configured owner and poll this task; do not submit duplicate work"})
        if record.get("status") in {"pending", "queued"}:
            self._launch(task_id)
        return self.public(record)

    def cancel(self, caller, task_id):
        record = self.jobs.read().get(task_id)
        if not record or record.get("caller") != caller:
            raise CodexRoutingError("task_not_found", "No routed task exists for this caller")
        if record.get("status") == "completed":
            return self.preview(record)
        self._save(task_id, {"_cancel_requested": True})
        try:
            with conversation_lock(self.state_dir, "routed-worker:" + task_id, timeout=0):
                try:
                    actual = self.router().result(task_id, caller)
                except CodexRoutingError as exc:
                    if exc.code != "task_not_found":
                        raise
                    actual = {"status": "pending"}
                if actual.get("status") in {"pending", "queued"}:
                    return self.preview(self._save(task_id, {"status": "canceled", "action": "Canceled before delivery"}))
                return self.preview(self._save(task_id, {**actual, "action": "Stopped waiting; accepted or uncertain owner work remains intact"}))
        except StorageError as exc:
            if exc.code != "lock_timeout":
                raise
            return self.preview(self._save(task_id, {"cancel_requested": True,
                                "action": "Cancellation requested; poll for acknowledgement. Any accepted owner turn will remain intact"}))


def add_conversation_tools(mcp, runtime, caller_peer, peer_call, load_task, access_allowed=lambda: True):
    def owner_tool(func):
        return mcp.tool()(func) if owner_routing_enabled() else func

    def output(action):
        try:
            if not access_allowed():
                raise CodexRoutingError("managed_peer_required", "Conversation extensions require a paired peer identity or local host access")
            return json.dumps(action(), ensure_ascii=False)
        except (CodexRoutingError, StorageError) as exc:
            return json.dumps({"error": exc.code, "message": str(exc), **getattr(exc, "details", {})})

    def complete(task_id, offset, limit):
        if offset < 0 or not 1 <= limit <= 200000:
            raise CodexRoutingError("invalid_range", "offset must be nonnegative and limit must be 1..200000 characters")
        if task_id.startswith("codex-"):
            record = runtime.result(caller_peer(), task_id)
            reply = str(record.get("reply") or "")
        else:
            record = load_task(task_id)
            if not record or record.get("caller_peer", "local") != caller_peer():
                raise CodexRoutingError("task_not_found", "No task exists for this caller")
            reply = str(record.get("stdout") or "")
        public = {k: v for k, v in record.items() if k not in {"reply", "stdout", "complete_result", "prompt", "args", "cwd_path", "caller_peer", "owner_token"}}
        end = min(len(reply), offset + limit)
        public.update(reply=reply[offset:end], offset=offset, total_chars=len(reply),
                      next_offset=end if end < len(reply) else None, complete=end >= len(reply),
                      result_complete=record.get("status") == "completed")
        return public

    @owner_tool
    def bridge_agent_projects_list() -> str:
        """List host-owned project mappings for optional Codex conversation routing."""
        return output(lambda: {"extension": EXTENSION, "projects": runtime.router().projects()})

    @owner_tool
    def bridge_agent_conversations_list(project: Optional[dict] = None) -> str:
        """Discover existing owner conversations under a repository identity or mapped project id."""
        return output(lambda: {"conversations": runtime.router().conversations(project)})

    @owner_tool
    def bridge_agent_conversation_read(conversation_id: str, project: Optional[dict] = None) -> str:
        """Read relevant owner conversation context, verifying the host project association."""
        return output(lambda: runtime.router().read(conversation_id, project))

    @owner_tool
    def bridge_agent_routed_delegate_start(request_id: str, prompt: str, topic: str,
            project: Optional[dict] = None, context: Optional[dict] = None,
            conversation_id: Optional[str] = None, delivery_mode: str = "queue",
            new_reason: Optional[str] = None, timeout_seconds: int = 300) -> str:
        """Route to a relevant Codex owner conversation. Reuse request_id only for identical retries; send selected context or deltas."""
        return output(lambda: runtime.start(caller_peer(), request_id, prompt, topic, project, context,
                                           conversation_id, delivery_mode, new_reason, timeout_seconds))

    @mcp.tool()
    def bridge_agent_complete_result(task_id: str, offset: int = 0, limit: int = 64000) -> str:
        """Retrieve full task replies in lossless pages; retired owner work is retained without resuming delivery."""
        return output(lambda: complete(task_id, offset, limit))

    @owner_tool
    def bridge_peer_projects_list(peer_id: str) -> str:
        """List project mappings on a paired, authenticated peer."""
        return output(lambda: peer_call(peer_id, "bridge_agent_projects_list", {}))

    @owner_tool
    def bridge_peer_conversations_list(peer_id: str, project: Optional[dict] = None) -> str:
        """Discover relevant existing conversations on a paired peer."""
        return output(lambda: peer_call(peer_id, "bridge_agent_conversations_list", {"project": project}))

    @owner_tool
    def bridge_peer_conversation_read(peer_id: str, conversation_id: str, project: Optional[dict] = None) -> str:
        """Read selected conversation context from a paired peer's owning server."""
        return output(lambda: peer_call(peer_id, "bridge_agent_conversation_read", {"conversation_id": conversation_id, "project": project}))

    @owner_tool
    def bridge_peer_routed_delegate_start(peer_id: str, request_id: str, prompt: str, topic: str,
            project: Optional[dict] = None, context: Optional[dict] = None,
            conversation_id: Optional[str] = None, delivery_mode: str = "queue",
            new_reason: Optional[str] = None, timeout_seconds: int = 300) -> str:
        """Route useful context or follow-up deltas to a paired peer's existing or justified new Codex conversation."""
        arguments = dict(request_id=request_id, prompt=prompt, topic=topic, project=project, context=context,
                         conversation_id=conversation_id, delivery_mode=delivery_mode, new_reason=new_reason,
                         timeout_seconds=timeout_seconds)
        return output(lambda: peer_call(peer_id, "bridge_agent_routed_delegate_start", arguments))

    @mcp.tool()
    def bridge_peer_complete_result(peer_id: str, task_id: str, offset: int = 0, limit: int = 64000) -> str:
        """Retrieve full task replies and route evidence from a paired peer without truncation loss."""
        return output(lambda: peer_call(peer_id, "bridge_agent_complete_result", {"task_id": task_id, "offset": offset, "limit": limit}))

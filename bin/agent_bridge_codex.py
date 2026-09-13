"""Codex conversation routing through an explicitly configured owning app-server.

This module never starts Codex, edits its stores, or changes execution policy.
The owner assertion and project mappings belong to the host operator. They do
not prove Desktop attachment: that requires live acceptance on that host.
Protocol reference: https://learn.chatgpt.com/docs/app-server
"""
from __future__ import annotations

import contextlib
import hashlib
import ipaddress
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

from agent_bridge_storage import AtomicJsonStore, StorageError, association_key, conversation_lock


class CodexRoutingError(RuntimeError):
    def __init__(self, code, message, **details):
        super().__init__(message)
        self.code = code
        self.details = details


def repository_identity(value):
    """Normalize transport spelling, never accept remote filesystem paths."""
    value = str(value or "").strip().rstrip("/")
    if not value:
        return ""
    if "://" not in value and re.match(r"^[^/@:]+@[^/:]+:.+", value):
        host, path = value.split("@", 1)[1].split(":", 1)
    else:
        parsed = urlparse(value if "://" in value else "https://" + value)
        host, path = parsed.hostname or "", parsed.path.lstrip("/")
        if parsed.port and parsed.port not in {22, 80, 443, 9418}:
            host += ":" + str(parsed.port)
        if parsed.query or parsed.fragment or parsed.scheme not in {"http", "https", "ssh", "git"}:
            raise CodexRoutingError("invalid_project", "Supply a repository identity without query or fragment")
    if path.endswith(".git"):
        path = path[:-4]
    if not host or not path or "\\" in path or any(part in {".", ".."} for part in path.split("/")):
        raise CodexRoutingError("invalid_project", "Supply a repository URL, not a local path")
    host = host.lower()
    if host in {"github.com", "gitlab.com"}:
        path = path.lower()
    return host + "/" + path


def _path(value):
    value = str(value or "").replace("\\", "/").rstrip("/")
    return value.casefold() if re.match(r"^[A-Za-z]:/", value) else value


def _tokens(value):
    stop = {"the", "and", "for", "with", "this", "that", "from", "please", "have", "work", "task", "project"}
    return set(re.findall(r"[\w.-]{3,}", str(value).lower())) - stop


def _active(thread):
    state = thread.get("status")
    if isinstance(state, str):
        state = {"type": state}
    if not isinstance(state, dict) or state.get("type") not in {"active", "idle", "notLoaded", "systemError"}:
        raise CodexRoutingError("owner_protocol", "Owner omitted a supported runtime status; delivery cannot safely determine whether the conversation is busy")
    if state.get("type") == "systemError":
        raise CodexRoutingError("owner_unavailable", "The selected owner conversation is in a system error state")
    turns = thread.get("turns", [])
    active = next((t for t in reversed(turns) if t.get("status") == "inProgress"), None)
    return state.get("type") == "active" or active is not None, active, state.get("activeFlags", [])


def _reply(turn):
    messages = [i for i in turn.get("items", []) if i.get("type") == "agentMessage"]
    final = [i for i in messages if i.get("phase") == "final_answer"]
    return "\n".join(str(i.get("text", "")) for i in (final or messages))


class OwnerAppServerRPC:
    """One initialized connection to a host-configured *existing* owner.

    Remote peer callers cannot select this endpoint or provide its credentials.
    Server requests are left to the owning client; we never approve them.
    """
    def __init__(self, owner):
        endpoint = owner.get("endpoint", "")
        parsed = urlparse(endpoint)
        if parsed.scheme not in {"ws", "wss", "unix"} or parsed.username or parsed.password:
            raise CodexRoutingError("owner_configuration", "Configure an existing owner ws/wss loopback or Unix endpoint")
        if parsed.scheme != "unix":
            try:
                loopback = parsed.hostname == "localhost" or ipaddress.ip_address(parsed.hostname or "").is_loopback
            except ValueError:
                loopback = False
            if not loopback or not parsed.port:
                raise CodexRoutingError("owner_configuration", "The owner endpoint must be local to this bridge host")
        if os.environ.get("AGENT_BRIDGE_TEST_SANDBOX", os.environ.get("HERMES_BRIDGE_TEST_SANDBOX")) == "1":
            raise CodexRoutingError("sandbox_violation", "Tests must inject a synthetic owner RPC client")
        try:
            from websockets.sync.client import connect, unix_connect
        except ImportError as exc:
            raise CodexRoutingError("missing_dependency", "Install the bridge's websockets dependency") from exc
        headers = None
        if owner.get("token_env"):
            token = os.environ.get(owner["token_env"])
            if not token:
                raise CodexRoutingError("owner_authentication", "The configured owner token environment variable is unset")
            headers = {"Authorization": "Bearer " + token}
        try:
            options = dict(additional_headers=headers, open_timeout=10, close_timeout=2, max_size=None)
            self.socket = unix_connect(path=parsed.path, **options) if parsed.scheme == "unix" else connect(endpoint, proxy=None, **options)
        except Exception as exc:
            raise CodexRoutingError("owner_unavailable", "Cannot connect to the configured owning app-server; verify its listener and authentication") from exc
        self.sequence = 0
        self.server_requests = []
        try:
            self.request("initialize", {"clientInfo": {"name": "agent_bridge", "title": "Agent Bridge", "version": "1"}})
            self.socket.send(json.dumps({"method": "initialized", "params": {}}))
        except Exception:
            self.close()
            raise

    def request(self, method, params, timeout=30):
        self.sequence += 1
        identifier = self.sequence
        deadline = time.monotonic() + timeout
        try:
            self.socket.send(json.dumps({"id": identifier, "method": method, "params": params}))
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                frame = json.loads(self.socket.recv(timeout=remaining))
                if frame.get("method"):
                    if "id" in frame:
                        self.server_requests.append(frame["method"])
                        self.server_requests = self.server_requests[-64:]
                    continue
                if frame.get("id") != identifier:
                    continue
                if "error" in frame:
                    error = frame["error"]
                    code = "unsupported_capability" if error.get("code") == -32601 else "owner_request_failed"
                    raw = str(error.get("message", "")).lower()
                    if "auth" in raw or "login" in raw:
                        code = "owner_authentication"
                    elif "approval" in raw:
                        code = "approval_required"
                    raise CodexRoutingError(code, f"Owner rejected {method}; inspect the owner for configuration, authorization, or capability requirements", rpc_code=error.get("code"), rejected=True)
                return frame.get("result", {})
        except CodexRoutingError:
            raise
        except Exception as exc:
            raise CodexRoutingError("owner_connection_lost", f"Owner connection ended during {method}; delivery acknowledgement may be uncertain") from exc

    def close(self):
        self.socket.close()


class CodexConversationRouter:
    def __init__(self, state_dir, host_config, rpc_factory=None, poll_interval=0.5):
        self.state_dir = Path(state_dir)
        self.config = host_config
        self.rpc_factory = rpc_factory or OwnerAppServerRPC
        self.poll_interval = max(0.01, poll_interval)
        self.store = AtomicJsonStore(self.state_dir / "codex-routing.json", lambda: {"associations": {}, "requests": {}})

    def _owner(self):
        owner = self.config.get("owner", {})
        if owner.get("desktop_owner") is not True or not owner.get("endpoint"):
            raise CodexRoutingError("owner_not_configured", "Configure the app-server that owns the Desktop conversations; a new app-server cannot stand in for the Desktop owner")
        return owner

    @contextlib.contextmanager
    def _rpc(self):
        rpc = self.rpc_factory(self._owner())
        try:
            yield rpc
        finally:
            close = getattr(rpc, "close", None)
            if close:
                close()

    def projects(self):
        return [{"id": p["id"], "repository": repository_identity(p.get("repository")), "association_source": "host_mapping"}
                for p in self.config.get("projects", [])]

    def _project(self, requested):
        if not requested:
            return {"id": "", "cwd": self.config.get("projectless_cwd"), "repository": ""}
        if not isinstance(requested, dict) or set(requested) - {"id", "repository"}:
            raise CodexRoutingError("invalid_project", "Select a host project id or repository identity; caller paths are not accepted")
        identity = repository_identity(requested.get("repository"))
        matches = [p for p in self.config.get("projects", [])
                   if (not requested.get("id") or p.get("id") == requested["id"])
                   and (not identity or repository_identity(p.get("repository")) == identity)]
        if len(matches) != 1:
            raise CodexRoutingError("ambiguous_project" if matches else "project_not_found", "Select one existing host-mapped project", candidates=[p["id"] for p in matches])
        project = dict(matches[0])
        if not project.get("cwd"):
            raise CodexRoutingError("owner_configuration", "The host project mapping needs its local cwd")
        return project

    def _matches_project(self, thread, project):
        paths = [_path(project.get("cwd")), *[_path(p) for p in project.get("cwd_aliases", [])]]
        return _path(thread.get("cwd")) in paths

    def _list(self, rpc, project):
        rows, seen = [], set()
        params = {"limit": 100, "sortKey": "updated_at", "sourceKinds": ["cli", "vscode", "exec", "appServer", "unknown"], "archived": False}
        if project.get("cwd"):
            paths = [project["cwd"], *project.get("cwd_aliases", [])]
            params["cwd"] = paths[0] if len(paths) == 1 else paths
        while True:
            page = rpc.request("thread/list", params)
            rows.extend(t for t in page.get("data", []) if self._matches_project(t, project))
            cursor = page.get("nextCursor")
            if not cursor:
                return rows
            if cursor in seen or len(rows) > 10000:
                raise CodexRoutingError("discovery_incomplete", "Owner pagination did not complete; no new conversation was created")
            seen.add(cursor)
            params["cursor"] = cursor

    def conversations(self, project=None):
        selected = self._project(project)
        with self._rpc() as rpc:
            return [{"conversation_id": t["id"], "title": t.get("name"), "context": t.get("preview", ""), "status": t.get("status"), "project_id": selected["id"]} for t in self._list(rpc, selected)]

    def read(self, conversation_id, project=None):
        """Bounded context for routing, separate from complete reply retrieval."""
        selected = self._project(project)
        with self._rpc() as rpc:
            thread = self._read(rpc, conversation_id)
        if not self._matches_project(thread, selected):
            raise CodexRoutingError("project_mismatch", "The conversation is outside the selected host project mapping")
        messages = []
        for turn in thread.get("turns", [])[-4:]:
            for item in turn.get("items", []):
                if item.get("type") == "agentMessage":
                    messages.append(str(item.get("text", "")))
                elif item.get("type") == "userMessage":
                    messages.extend(str(c.get("text", "")) for c in item.get("content", []) if c.get("type") == "text")
        context = "\n".join(messages)
        return {"conversation_id": conversation_id, "title": thread.get("name"), "project_id": selected["id"],
                "status": thread.get("status"), "context": context[-16000:], "context_truncated": len(context) > 16000,
                "preview": str(thread.get("preview", ""))[:4000]}

    @contextlib.contextmanager
    def _request_lock(self, key, timeout, cancel_event):
        lock = conversation_lock(self.state_dir, "request:" + key, timeout=timeout, cancel_event=cancel_event)
        try:
            lock.__enter__()
        except StorageError as exc:
            if exc.code not in {"lock_timeout", "canceled"}:
                raise
            yield False
            return
        try:
            yield True
        finally:
            lock.__exit__(None, None, None)

    def _read(self, rpc, conversation_id):
        thread = rpc.request("thread/read", {"threadId": conversation_id, "includeTurns": True}).get("thread")
        if not isinstance(thread, dict) or thread.get("id") != conversation_id:
            raise CodexRoutingError("owner_protocol", "Owner returned an inconsistent conversation")
        return thread

    def _update(self, key, **changes):
        def change(state):
            state["requests"][key].update(changes)
            return dict(state["requests"][key])
        return self.store.mutate(change)

    @staticmethod
    def _public(record):
        return {k: v for k, v in record.items() if k not in {"fingerprint", "prompt", "context", "association", "caller", "before_turn_ids"}}

    def _choose(self, rpc, project, topic, context, association, conversation_id, mode, new_reason, key):
        saved = self.store.read().get("associations", {}).get(association)
        if conversation_id:
            thread = self._read(rpc, conversation_id)
            reason = "caller selected an existing conversation"
        elif saved and mode != "separate":
            thread = self._read(rpc, saved["conversation_id"])
            reason = "reused the caller, agent, project, and topic association"
        elif mode != "separate":
            requested_tokens = _tokens(topic + " " + json.dumps(context, ensure_ascii=False))
            ranked = []
            for candidate in self._list(rpc, project):
                detail = self._read(rpc, candidate["id"])
                # Titles are deliberately excluded from relevance. Read actual context.
                evidence = str(detail.get("preview", "")) + " " + json.dumps(detail.get("turns", [])[-4:], ensure_ascii=False)
                overlap = requested_tokens & _tokens(evidence)
                score = len(overlap) / max(1, len(requested_tokens))
                if len(overlap) >= 2 and score >= 0.2:
                    ranked.append((score, detail))
            ranked.sort(key=lambda pair: pair[0], reverse=True)
            if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.1:
                raise CodexRoutingError("ambiguous_conversation", "Several conversations match the supplied context; select one conversation id", candidates=[t["id"] for _, t in ranked[:5]])
            thread = ranked[0][1] if ranked else None
            reason = "reused relevant conversation context" if thread else "no relevant existing conversation matched the supplied context"
        else:
            thread = None
            reason = "separate contribution: " + new_reason
        if thread:
            if not self._matches_project(thread, project):
                raise CodexRoutingError("project_mismatch", "The chosen conversation no longer belongs to the host-mapped project; reconcile the mapping explicitly")
            created = False
        else:
            if not project.get("cwd"):
                raise CodexRoutingError("owner_configuration", "Configure a host-owned projectless_cwd before creating a projectless conversation")
            self._update(key, status="creating_conversation", selection_reason=reason)
            thread = rpc.request("thread/start", {"cwd": project["cwd"]}).get("thread", {})
            if not thread.get("id"):
                raise CodexRoutingError("owner_protocol", "Owner did not acknowledge the created conversation")
            self._update(key, status="conversation_created", conversation_id=thread["id"], created=True, reused=False, project_id=project["id"])
            if not self._matches_project(thread, project):
                raise CodexRoutingError("project_mismatch", "Owner created a conversation outside the requested host project mapping; no contribution was sent")
            created = True
        metadata = {"conversation_id": thread["id"], "session_id": thread.get("sessionId"), "created": created, "reused": not created,
                    "project_id": project["id"], "project_association_source": "host_mapping", "desktop_project_registration": "unverified",
                    "owner_verification": "host_operator_assertion", "selection_reason": reason,
                    "requested_delivery_mode": mode, "delivery_mode": mode}
        def save(state):
            state.setdefault("associations", {})[association] = {"conversation_id": thread["id"]}
            state["requests"][key].update(metadata, status="queued")
        self.store.mutate(save)
        return metadata

    def delegate(self, caller, agent_id, request_id, prompt, context=None, project=None, topic="", conversation_id=None,
                 delivery_mode="queue", new_reason=None, timeout=300, on_selected=None, cancel_event=None):
        if delivery_mode not in {"queue", "steer", "separate"}:
            raise CodexRoutingError("invalid_delivery", "Choose queue, steer, or separate")
        if delivery_mode == "separate" and (not isinstance(new_reason, str) or not new_reason.strip()):
            raise CodexRoutingError("new_reason_required", "A separate conversation requires the calling agent's reason")
        if delivery_mode == "separate" and conversation_id:
            raise CodexRoutingError("invalid_delivery", "Separate delivery cannot select an existing conversation id")
        if not all(isinstance(v, str) and v.strip() for v in (caller, agent_id, request_id, prompt, topic)):
            raise CodexRoutingError("invalid_request", "Caller, agent, request id, topic, and prompt must be nonempty strings")
        if context is not None and (not isinstance(context, dict) or set(context) - {"objectives", "decisions", "constraints", "progress", "requested_contribution", "delta"}):
            raise CodexRoutingError("invalid_context", "Context may contain objectives, decisions, constraints, progress, requested_contribution, and delta")
        self._owner()
        target = self._project(project)
        key = association_key(caller, agent_id, "request", request_id)
        association = association_key(caller, agent_id, target["id"], topic)
        fingerprint = hashlib.sha256(json.dumps([prompt, context, project, topic, conversation_id, delivery_mode, new_reason], sort_keys=True).encode()).hexdigest()
        def reserve(state):
            requests = state.setdefault("requests", {})
            previous = requests.get(key)
            if previous and previous["fingerprint"] != fingerprint:
                raise CodexRoutingError("request_conflict", "This request id already names different input; use a new id for a follow-up")
            if previous:
                return dict(previous)
            record = {"task_id": request_id, "request_id": request_id, "caller": caller, "agent_id": agent_id, "fingerprint": fingerprint,
                      "association": association, "prompt": prompt, "context": context or {}, "status": "pending", "reply": ""}
            requests[key] = record
            return dict(record)
        record = self.store.mutate(reserve)
        deadline = time.monotonic() + max(0, timeout)
        # Request locks plus per-association selection locks prevent duplicate
        # delivery and duplicate creation across local/peer bridge processes.
        with self._request_lock(key, max(0, timeout), cancel_event) as acquired:
            if not acquired:
                current = self._public(self.store.read()["requests"][key])
                return dict(current, pending_retry=True, action="Another worker owns this request or the wait was canceled; retrieve its result or retry the same request id")
            record = self.store.read()["requests"][key]
            if record["status"] not in {"pending", "queued"}:
                return self.result(request_id, caller, agent_id)
            with self._rpc() as rpc:
                try:
                    if not record.get("conversation_id"):
                        with conversation_lock(self.state_dir, "route:" + association, timeout=max(0, deadline - time.monotonic()), cancel_event=cancel_event):
                            metadata = self._choose(rpc, target, topic, context or {}, association, conversation_id, delivery_mode, new_reason, key)
                    else:
                        metadata = self._public(record)
                    if on_selected:
                        on_selected(metadata)
                    return self._deliver(rpc, key, deadline, cancel_event)
                except CodexRoutingError as exc:
                    current = self.store.read()["requests"][key]
                    uncertain = current["status"] in {"creating_conversation", "sending"} and not exc.details.get("rejected")
                    status = "delivery_uncertain" if uncertain else current["status"] if current.get("turn_id") else "failed"
                    return self._public(self._update(key, status=status, error_code=exc.code, error=str(exc), **exc.details,
                                                     action="Inspect the owner and reconcile this request; do not automatically resend" if uncertain else "Inspect the owner; use a new request id for corrected input after resolving the issue"))
                except StorageError as exc:
                    if exc.code not in {"lock_timeout", "canceled"}:
                        raise
                    return self._public(self._update(key, status="canceled" if exc.code == "canceled" else "queued",
                                                     action="Canceled before delivery" if exc.code == "canceled" else "Retry the same request id to continue the queue", pending_retry=exc.code == "lock_timeout"))

    def _deliver(self, rpc, key, deadline, cancel_event):
        record = self.store.read()["requests"][key]
        cid = record["conversation_id"]
        while True:
            if cancel_event and cancel_event.is_set():
                return self._public(self._update(key, status="canceled", action="Canceled before delivery"))
            # Lock only observe/send/ack. A queued worker must not prevent a
            # later explicit steer from reaching the owner's active turn.
            with conversation_lock(self.state_dir, "codex:" + cid, timeout=max(0, deadline - time.monotonic()), cancel_event=cancel_event):
                sent = self._try_send(rpc, key)
            if sent:
                break
            if time.monotonic() >= deadline:
                return self._public(self._update(key, pending_retry=True, action="Retry the same request id to continue the queue"))
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))
        while True:
            result = self._reconcile(rpc, key)
            if result["status"] != "running" or time.monotonic() >= deadline:
                return result
            if cancel_event and cancel_event.is_set():
                return self._public(self._update(key, action="Caller stopped waiting; accepted owner work was left intact"))
            time.sleep(min(self.poll_interval, max(0, deadline - time.monotonic())))

    def _try_send(self, rpc, key):
        record = self.store.read()["requests"][key]
        cid, requested_mode = record["conversation_id"], record["delivery_mode"]
        thread = self._read(rpc, cid)
        project = self._project({"id": record["project_id"]} if record["project_id"] else None)
        if not self._matches_project(thread, project):
            raise CodexRoutingError("project_mismatch", "The queued conversation moved outside its host project mapping; no contribution was sent")
        busy, active_turn, flags = _active(thread)
        can_steer = self.config["owner"].get("supports_steer") is True
        if busy and requested_mode == "steer" and can_steer and active_turn:
            method, turn_id = "turn/steer", active_turn["id"]
            reason = "calling agent selected steering of the observed active turn"
        elif not busy:
            method, turn_id = "turn/start", None
            reason = "conversation is idle; start the queued contribution"
        else:
            reason = "conversation is busy; ordinary work is queued"
            if requested_mode == "steer":
                reason = "steering is unsupported or active turn id unavailable; queued without interrupting"
            self._update(key, status="queued", delivery_mode="queue", delivery_reason=reason,
                         action="Waiting for host approval" if "waitingOnApproval" in flags else "Waiting for the active turn")
            return False
        state = thread.get("status", {})
        if not busy and (state == "notLoaded" or isinstance(state, dict) and state.get("type") == "notLoaded"):
            rpc.request("thread/resume", {"threadId": cid})
        context = record.get("context") or {}
        text = record["prompt"]
        if context:
            text += "\n\nShared project context (selected by the calling agent):\n" + json.dumps(context, ensure_ascii=False)
        params = {"threadId": cid, "input": [{"type": "text", "text": text}]}
        if turn_id:
            params["expectedTurnId"] = turn_id
        self._update(key, status="sending", before_turn_ids=[t.get("id") for t in thread.get("turns", [])],
                     delivery_mode="steer" if turn_id else "separate" if requested_mode == "separate" else "queue",
                     delivery_reason=reason, pending_retry=False, action="Waiting for delivery acknowledgement")
        try:
            response = rpc.request(method, params)
        except CodexRoutingError as exc:
            if method == "turn/steer" and exc.code == "unsupported_capability":
                self._update(key, status="queued", delivery_mode="queue", delivery_reason="Owner rejected steering as unsupported; queued without interrupting", steering_unsupported=True)
                return False
            raise
        accepted = response.get("turnId") if turn_id else response.get("turn", {}).get("id")
        if not accepted:
            raise CodexRoutingError("owner_protocol", "Owner did not acknowledge a turn id; inspect the owner before retrying")
        self._update(key, turn_id=accepted, status="running", action="Retrieve the result for the accepted turn")
        return True

    def _reconcile(self, rpc, key):
        record = self.store.read()["requests"][key]
        if not record.get("turn_id"):
            return self._public(record)
        thread = self._read(rpc, record["conversation_id"])
        project = self._project({"id": record["project_id"]} if record["project_id"] else None)
        if not self._matches_project(thread, project):
            raise CodexRoutingError("project_mismatch", "The accepted conversation moved outside its host project mapping; reconcile the host mapping before retrieval")
        turn = next((t for t in thread.get("turns", []) if t.get("id") == record["turn_id"]), None)
        if turn is None:
            return self._public(self._update(key, status="delivery_uncertain", error_code="accepted_turn_missing", action="Owner omitted the accepted turn; inspect its history before any resend"))
        status = turn.get("status")
        if status == "completed":
            reply = _reply(turn)
            if not reply:
                return self._public(self._update(key, status="result_unavailable", error_code="empty_reply", action="The owner completed without a retrievable agent reply; inspect the accepted turn"))
            return self._public(self._update(key, status="completed", reply=reply, error_code=None, error=None,
                                             approval_required=False, pending_retry=False, action="Complete reply retrieved from the owning app-server"))
        if status in {"failed", "interrupted"}:
            return self._public(self._update(key, status="failed" if status == "failed" else "canceled", reply=_reply(turn), error_code="owner_turn_" + status, action="Inspect the owner turn; a new request is required to perform more work"))
        _, _, flags = _active(thread)
        return self._public(self._update(key, status="running", action="Host approval required" if "waitingOnApproval" in flags else "Waiting for the accepted owner turn", approval_required="waitingOnApproval" in flags))

    def result(self, request_id, caller, agent_id="codex"):
        key = association_key(caller, agent_id, "request", request_id)
        record = self.store.read().get("requests", {}).get(key)
        if not record:
            raise CodexRoutingError("task_not_found", "No routed request exists for this caller and agent")
        if record.get("turn_id") and record["status"] in {"running", "result_unavailable", "delivery_uncertain"}:
            with self._rpc() as rpc:
                return self._reconcile(rpc, key)
        if record["status"] in {"creating_conversation", "sending"}:
            return self._public(dict(record, status="delivery_uncertain", action="Delivery may have happened; reconcile on the owner before resending"))
        return self._public(record)

"""Transactional bridge-owned state, shared by local and peer processes.

Never infer that an external delivery failed from a bridge restart. A claimed
task with a positively dead owner becomes interrupted and requires reconciliation.
"""
from __future__ import annotations

import errno
import hashlib
import json
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable


class StorageError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _windows_process_api():
    import ctypes
    from ctypes import wintypes
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    api.OpenProcess.restype = wintypes.HANDLE
    api.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    api.GetExitCodeProcess.restype = wintypes.BOOL
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api, ctypes.get_last_error


def _windows_process_alive(pid, api=None, last_error=None):
    """Query only: os.kill(pid, 0) can terminate processes on Windows."""
    import ctypes
    from ctypes import wintypes
    try:
        if api is None:
            api, last_error = _windows_process_api()
        handle = api.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            # A valid but nonexistent process id returns ERROR_INVALID_PARAMETER.
            # Access denied or any other failure is ambiguous, never evidence of death.
            return False if last_error() == 87 else None
        try:
            code = wintypes.DWORD()
            if not api.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == 259  # STILL_ACTIVE
        finally:
            api.CloseHandle(handle)
    except (OSError, AttributeError, TypeError):
        return None


def process_alive(pid) -> bool | None:
    """Return death only on positive OS evidence; uncertainty preserves delivery.

    A live recycled PID is intentionally treated as possibly alive. Callers must
    never infer permission to rerun a delivery solely from a missing worker map.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0 or pid > 0xFFFFFFFF:
        return None
    if os.name == "nt":
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return None


class InterProcessFileLock:
    """OS lock released on process exit; nonblocking probes allow cancellation.

    Separate handles serialize threads as well as processes. Instances are not
    reentrant: acquire a different file for nested transactions.
    """
    def __init__(self, path: Path, timeout=None, cancel_event=None):
        self.path = Path(path)
        self.timeout = timeout
        self.cancel_event = cancel_event
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = open(self.path, "a+b")
        deadline = None if self.timeout is None else time.monotonic() + self.timeout
        try:
            if os.name == "nt":
                self.handle.seek(0, os.SEEK_END)
                if self.handle.tell() == 0:
                    self.handle.write(b"0")
                    self.handle.flush()
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise StorageError("canceled", "delivery canceled while waiting for conversation")
                try:
                    if os.name == "nt":
                        import msvcrt
                        self.handle.seek(0)
                        msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return self
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                if deadline is not None and time.monotonic() >= deadline:
                    raise StorageError("lock_timeout", "conversation or state is busy; retry without changing request id")
                time.sleep(0.02)
        except BaseException:
            self.handle.close()
            self.handle = None
            raise

    def __exit__(self, exc_type, exc, tb):
        if self.handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self.handle.seek(0)
                msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.handle.fileno(), fcntl.LOCK_UN)
        finally:
            self.handle.close()
            self.handle = None


def atomic_json_write(path: Path, value: dict[str, Any]) -> None:
    """Write completely before rename; caller owns the read/modify/write lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, ensure_ascii=False, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class AtomicJsonStore:
    def __init__(self, path: Path, default_factory: Callable = dict):
        self.path = Path(path)
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self.default_factory = default_factory

    def _read_unlocked(self):
        try:
            value = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return self.default_factory()
        except (OSError, ValueError) as exc:
            raise StorageError("state_unreadable", "bridge state cannot be read; preserve it and repair before delivery") from exc
        if not isinstance(value, dict):
            raise StorageError("state_unreadable", "bridge state must contain a JSON object")
        return value

    def read(self):
        with InterProcessFileLock(self.lock_path):
            return self._read_unlocked()

    def mutate(self, callback: Callable):
        with InterProcessFileLock(self.lock_path):
            value = self._read_unlocked()
            result = callback(value)
            atomic_json_write(self.path, value)
            return result


def association_key(caller: str, agent: str, project: str, topic: str) -> str:
    return json.dumps([str(caller or "local"), str(agent), str(project or ""), str(topic or "default")], separators=(",", ":"))


def conversation_lock(state_dir: Path, scope: str, timeout=None, cancel_event=None):
    name = hashlib.sha256(str(scope).encode("utf-8")).hexdigest()
    return InterProcessFileLock(Path(state_dir) / "conversation-locks" / (name + ".lock"), timeout, cancel_event)


class DurableTaskStore:
    """Atomic reservations are durable before an adapter can receive work."""
    def __init__(self, path: Path):
        self.store = AtomicJsonStore(path, lambda: {"schema_version": 1, "tasks": {}, "requests": {}})

    def reserve(self, task_id: str, request_key=None, request_fingerprint=None, **record):
        def reserve(data):
            tasks, requests = data.setdefault("tasks", {}), data.setdefault("requests", {})
            existing_id = requests.get(request_key) if request_key else None
            if existing_id is not None:
                previous = tasks.get(existing_id)
                if previous is None:
                    raise StorageError("state_unreadable", "request references missing task; reconcile before retry")
                if previous.get("request_fingerprint") != request_fingerprint:
                    raise StorageError("idempotency_conflict", "request id already belongs to a different payload")
                return dict(previous), False
            if task_id in tasks:
                return dict(tasks[task_id]), False
            value = {**record, "task_id": task_id, "status": "queued", "created_at": time.time(),
                     "request_key": request_key, "request_fingerprint": request_fingerprint}
            tasks[task_id] = value
            if request_key:
                requests[request_key] = task_id
            return dict(value), True
        return self.store.mutate(reserve)

    def claim(self, task_id: str, owner_id: str, owner_pid=None):
        def claim(data):
            value = data["tasks"][task_id]
            if value["status"] != "queued":
                return False
            value.update(status="running", owner_id=owner_id,
                         owner_pid=os.getpid() if owner_pid is None else owner_pid, started_at=time.time())
            return True
        return self.store.mutate(claim)

    def update(self, task_id: str, **changes):
        def update(data):
            value = data["tasks"][task_id]
            value.update(changes)
            value["updated_at"] = time.time()
            return dict(value)
        return self.store.mutate(update)

    def complete(self, task_id: str, result: dict):
        return self.update(task_id, status=result.get("status", "completed"), result=result, finished_at=time.time())

    def get(self, task_id: str):
        return self.store.read().get("tasks", {}).get(task_id)

    def list(self):
        return list(self.store.read().get("tasks", {}).values())

    def reconcile(self, owner_alive: Callable[[dict], bool | None]):
        """Only positive evidence of owner death interrupts; unknown stays put."""
        def reconcile(data):
            interrupted = []
            for task_id, value in data.get("tasks", {}).items():
                if value.get("status") == "running" and owner_alive(dict(value)) is False:
                    value.update(status="interrupted", updated_at=time.time(),
                                 error_code="delivery_outcome_unknown",
                                 error="Owner exited after claiming delivery. Retrieve the conversation result before deciding whether to retry.")
                    interrupted.append(task_id)
            return interrupted
        return self.store.mutate(reconcile)

    def conversation_lock(self, scope: str, timeout=None, cancel_event=None):
        return conversation_lock(self.store.path.parent, scope, timeout, cancel_event)

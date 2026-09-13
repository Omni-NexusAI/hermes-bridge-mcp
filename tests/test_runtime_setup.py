"""Setup regressions use only synthetic payloads and temporary host state."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import types
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))
from agent_bridge_runtime import atomic_json, probe, state_dir


def load_file(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def payload(tmp_path, monkeypatch):
    module = load_file("bootstrap_test", "bootstrap.py")
    source = tmp_path / "source"
    source.mkdir()
    for name in module.PAYLOAD:
        path = source / name
        if "." in name:
            path.write_text("synthetic payload", encoding="utf-8")
        else:
            path.mkdir()
            (path / "fixture.txt").write_text("first", encoding="utf-8")
    monkeypatch.setenv("AGENT_BRIDGE_INSTALL_ROOT", str(tmp_path / "runtime"))
    return module, source


def test_dirty_payload_gets_distinct_release_and_rollback_keeps_environment(tmp_path, monkeypatch):
    module, source = payload(tmp_path, monkeypatch)
    environments = []
    class Builder:
        def __init__(self, **kwargs): pass
        def create(self, directory):
            environments.append(directory)
            executable = module.python_in(directory)
            executable.parent.mkdir(parents=True)
            executable.write_text("synthetic python")
    monkeypatch.setattr(module.venv, "EnvBuilder", Builder)
    monkeypatch.setattr(module.subprocess, "check_call", lambda *args, **kwargs: None)
    first = module.install(source, False, True)
    first_marker = json.loads((module.bridge_root() / "current.json").read_text())
    (source / "bin/fixture.txt").write_text("changed without a commit")
    second = module.install(source, False, True)
    second_marker = json.loads((module.bridge_root() / "current.json").read_text())
    assert first["release"] != second["release"]
    assert len(environments) == 2 and environments[0] != environments[1]
    assert second_marker["previous_python"] == first_marker["python"]
    module.rollback()
    restored = json.loads((module.bridge_root() / "current.json").read_text())
    assert restored["release"] == first["release"]
    assert restored["python"] == first_marker["python"]


def test_failed_dependencies_do_not_activate_candidate(tmp_path, monkeypatch):
    module, source = payload(tmp_path, monkeypatch)
    module.install(source, False, False)
    marker = (module.bridge_root() / "current.json").read_bytes()
    (source / "bin/fixture.txt").write_text("second")
    monkeypatch.setattr(module.venv.EnvBuilder, "create", lambda *args: None)
    def fail(*args, **kwargs): raise RuntimeError("synthetic dependency failure")
    monkeypatch.setattr(module.subprocess, "check_call", fail)
    with pytest.raises(RuntimeError, match="synthetic"):
        module.install(source, False, True)
    assert (module.bridge_root() / "current.json").read_bytes() == marker


def test_atomic_marker_failure_leaves_old_document(tmp_path, monkeypatch):
    import agent_bridge_runtime
    path = tmp_path / "current.json"
    atomic_json(path, {"release": "old"})
    def fail(*args): raise OSError("synthetic replace failure")
    monkeypatch.setattr(agent_bridge_runtime.os, "replace", fail)
    with pytest.raises(OSError): atomic_json(path, {"release": "new"})
    assert json.loads(path.read_text()) == {"release": "old"}
    assert not list(tmp_path.glob("*.tmp"))


def test_installer_requests_guarded_convergence_for_every_listener():
    """A selected payload is not a successful update until both launcher roles agree."""
    installer = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "-File $bridge -Restart" in installer
    assert '"start-agent-bridge-peer.ps1") -Restart' in installer
    assert "did not converge to the selected release" in installer


def test_a0_disabled_customization_and_headers_survive():
    module = load_file("a0_setup_test", "scripts/configure-a0-mcp.py")
    original = {"mcp_servers": {"mcpServers": {"agent-bridge": {
        "disabled": True, "url": "old", "tool_timeout": 1234,
        "description": "my description", "custom": [1], "headers": {"X-Host": "keep"}}}}}
    updated, changed = module.configure(original, "agent-bridge", "new", "synthetic-token")
    entry = json.loads(updated["mcp_servers"])["mcpServers"]["agent-bridge"]
    assert changed and entry["disabled"] is True
    assert entry["tool_timeout"] == 1234 and entry["description"] == "my description"
    assert entry["custom"] == [1] and entry["headers"]["X-Host"] == "keep"
    assert original["mcp_servers"]["mcpServers"]["agent-bridge"]["url"] == "old"
    again, changed = module.configure(updated, "agent-bridge", "new", "synthetic-token")
    assert not changed


def test_state_resolution_uses_canonical_explicit_location(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_BRIDGE_HOME", str(tmp_path / "canonical"))
    monkeypatch.setenv("HERMES_BRIDGE_HOME", str(tmp_path / "legacy"))
    monkeypatch.delenv("HERMES_BRIDGE_STATE_DIR")
    assert state_dir() == tmp_path / "canonical/bridge-state"
    monkeypatch.setenv("AGENT_BRIDGE_STATE_DIR", str(tmp_path / "custom"))
    assert state_dir() == tmp_path / "custom"


def test_doctor_accepts_codex_only_and_reports_config_error(tmp_path, monkeypatch):
    module, source = payload(tmp_path, monkeypatch)
    module.install(source, False, False)
    executable = module.python_in(module.bridge_root() / "venv")
    executable.parent.mkdir(parents=True)
    executable.write_text("synthetic interpreter")
    class Registry:
        def __init__(self, *args): pass
        def public_agents(self):
            return [{"agent": "hermes", "available": False, "enabled": True},
                    {"agent": "codex", "available": True, "enabled": True}]
    monkeypatch.setitem(sys.modules, "agent_bridge_universal", types.SimpleNamespace(UniversalAgentRegistry=Registry))
    monkeypatch.setattr(module.shutil, "which", lambda name: None)
    result = module.doctor()
    assert result["action"].startswith("configuration_ready")
    assert result["authentication"] == "not_probed" and result["listener_readiness"] == "not_probed"
    def malformed(self):
        error = ValueError("not echoed")
        error.code = "invalid_manifest"
        raise error
    monkeypatch.setattr(Registry, "public_agents", malformed)
    assert module.doctor()["adapter_error"] == "invalid_manifest"


def test_readiness_rejects_wrong_release_pid_and_status():
    data = {"status": "ready", "bridge_version": "test", "build_revision": "candidate", "process_id": 123}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps(data).encode())
        def log_message(self, *args): pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        port = server.server_port
        assert port not in {18082, 18083, 18084, 18443}
        assert probe(port, expected_revision="candidate", expected_pid=123)["ready"]
        assert not probe(port, expected_revision="old", expected_pid=123)["ready"]
        assert not probe(port, expected_revision="candidate", expected_pid=999)["ready"]
        data["status"] = "not_ready"
        assert not probe(port)["ready"]
    finally:
        server.shutdown(); server.server_close(); worker.join()


def test_powershell_launchers_parse_and_ownership_is_exact(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell: pytest.skip("PowerShell unavailable")
    script = tmp_path / "check.ps1"
    # Only parse repository code and call a pure predicate with synthetic processes.
    script.write_text('''$ErrorActionPreference = 'Stop'
Get-ChildItem -LiteralPath $args[0] -Filter '*.ps1' | ForEach-Object {
    $errors = $null; $tokens = $null
    [Management.Automation.Language.Parser]::ParseFile($_.FullName, [ref]$tokens, [ref]$errors) | Out-Null
    if ($errors) { throw ($errors | Out-String) }
}
. (Join-Path $args[0] 'agent-bridge-launcher-common.ps1')
$p = [pscustomobject]@{ ExecutablePath = 'C:\\test\\python.exe'; CommandLine = '"C:\\test\\python.exe" "C:\\test\\bridge.py" --port 49123' }
if (-not (Test-BridgeProcessOwnership $p 'C:\\test\\bridge.py' 'C:\\test\\python.exe')) { throw 'exact ownership failed' }
if (Test-BridgeProcessOwnership $p 'C:\\test\\bridge.py' 'C:\\other\\python.exe') { throw 'wrong executable accepted' }
$p.CommandLine = '"C:\\test\\python.exe" "C:\\test\\bridge.py.backup"'
if (Test-BridgeProcessOwnership $p 'C:\\test\\bridge.py' 'C:\\test\\python.exe') { throw 'substring script accepted' }
''')
    result = subprocess.run([shell, "-NoProfile", "-File", str(script), str(ROOT / "bin")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_launcher_busy_and_wrong_owner_never_stopped_and_local_start_is_http(tmp_path):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell: pytest.skip("PowerShell unavailable")
    script = tmp_path / "lifecycle.ps1"
    script.write_text('''$ErrorActionPreference = 'Stop'
. (Join-Path $args[0] 'agent-bridge-launcher-common.ps1')
$BridgeHome = $args[1]; $LogDir = Join-Path $BridgeHome 'logs'
$RuntimeRoot = Join-Path $BridgeHome 'runtime'; $Release = Join-Path $BridgeHome 'release'
$LegacyHome = Join-Path $BridgeHome 'legacy'; $PythonExe = Join-Path $BridgeHome 'python.exe'
$BridgeScript = Join-Path $Release 'bin/agent-bridge-mcp.py'; $BuildRevision = 'candidate'
New-Item -ItemType Directory -Force $BridgeHome | Out-Null
$script:stopped = $false; $script:started = $false; $script:wrongOwner = $false
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($Filter -like 'ParentProcessId*') { return @() }
    return [pscustomobject]@{ ProcessId = $(if ($script:started) {222} else {123}); ExecutablePath = $(if ($script:wrongOwner) { 'C:\\unrelated.exe' } else { $PythonExe }); CommandLine = ('"' + $PythonExe + '" "' + $BridgeScript + '"'); CreationDate = 42 }
}
function Get-NetTCPConnection { if ($script:started) { return [pscustomobject]@{OwningProcess = 222} }; return @() }
function Stop-Process { $script:stopped = $true; throw 'must not stop' }
function Test-BridgeReady { return $script:started }
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, $RedirectStandardOutput, $RedirectStandardError, [switch]$PassThru)
    if ($ArgumentList -contains '--secure-network') { throw 'local launcher selected TLS' }
    if ($ArgumentList -notcontains '--stateless-http') { throw 'stateless contract lost' }
    if ($WindowStyle -ne 'Hidden') { throw 'visible startup' }
    $script:started = $true
    return [pscustomobject]@{ Id = 222; HasExited = $false }
}
'123' | Set-Content (Join-Path $BridgeHome 'test.pid')
try { Start-ManagedBridge 'test' 49123 '127.0.0.1'; throw 'busy process accepted' }
catch { if ($_.Exception.Message -notmatch 'Checkpoint active tasks') { throw } }
if ($script:stopped -or $script:started) { throw 'busy process was interrupted' }
$script:wrongOwner = $true
try { Start-ManagedBridge 'test' 49123 '127.0.0.1' -Restart; throw 'wrong owner accepted' }
catch { if ($_.Exception.Message -notmatch 'verified bridge executable') { throw } }
if ($script:stopped -or $script:started) { throw 'wrong owner was interrupted' }
Remove-Item -LiteralPath (Join-Path $BridgeHome 'test.pid')
$script:wrongOwner = $false
$env:AGENT_BRIDGE_SECURE_NETWORK = '1'; $env:HERMES_BRIDGE_SECURE_NETWORK = '1'
Start-ManagedBridge 'test' 49123 '127.0.0.1'
if ($env:AGENT_BRIDGE_SECURE_NETWORK -ne '0' -or $env:HERMES_BRIDGE_SECURE_NETWORK -ne '0') { throw 'local TLS environment not cleared' }
if (-not $script:started) { throw 'new process was not started' }
''')
    result = subprocess.run([shell, "-NoProfile", "-File", str(script), str(ROOT / "bin"), str(tmp_path / "synthetic-home")], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

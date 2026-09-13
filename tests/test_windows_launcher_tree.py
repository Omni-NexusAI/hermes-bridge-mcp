"""Windows redirector ownership tests use synthetic processes or a disposable server."""
import os
import shutil
import socket
import subprocess
import textwrap
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("scenario", [
    "ready_child", "ready_busy", "inspection_denied", "listener_inventory_denied",
    "restart", "delayed_restart", "historical_release", "historical_shared_runtime", "autoexit", "unknown_child", "unknown_descendant",
    "wrong_base", "wrong_script", "older_child", "reused_child", "reused_root", "foreign_listener",
])
def test_redirector_tree_ownership_and_restart(tmp_path, scenario):
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell or os.name != "nt":
        pytest.skip("Windows PowerShell required")
    script = tmp_path / "tree.ps1"
    script.write_text(r'''$ErrorActionPreference = 'Stop'
. (Join-Path $args[0] 'agent-bridge-launcher-common.ps1')
$BridgeHome = $args[1]; $LogDir = Join-Path $BridgeHome 'logs'
$RuntimeRoot = Join-Path $BridgeHome 'runtime'; $Release = Join-Path $BridgeHome 'release'
$LegacyHome = Join-Path $BridgeHome 'legacy'; $PythonExe = Join-Path $BridgeHome 'venv\Scripts\python.exe'
$BridgeScript = Join-Path $Release 'bin\agent-bridge-mcp.py'; $BuildRevision = 'candidate'
$base = Join-Path $BridgeHome 'base\python.exe'; $scenario = $args[2]
New-Item -ItemType Directory -Force (Split-Path $PythonExe -Parent) | Out-Null
New-Item -ItemType File -Force $PythonExe | Out-Null
('base-executable = ' + $base) | Set-Content (Join-Path $BridgeHome 'venv\pyvenv.cfg')
function Make-Process($number, $parent, $exe, $birth, $command) {
    [pscustomobject]@{ProcessId=$number;ParentProcessId=$parent;ExecutablePath=$exe;CreationDate=$birth;CommandLine=$command}
}
$command = '"' + $PythonExe + '" "' + $BridgeScript + '" --port 49123'
$script:rows = @(
    (Make-Process 200 100 $PythonExe 10 $command),
    (Make-Process 201 200 $base 11 $command),
    (Make-Process 202 200 (Join-Path $env:SystemRoot 'System32\conhost.exe') 11 'conhost.exe')
)
$script:stopped = @(); $script:probed = @(); $script:listener = 201; $script:drain = 0
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($scenario -eq 'inspection_denied') { throw [UnauthorizedAccessException]::new('synthetic CIM denied') }
    if ($Filter -match '^ProcessId = (\d+)$') { return @($script:rows | Where-Object ProcessId -eq ([int]$Matches[1])) }
    if ($Filter -match '^ParentProcessId = (\d+)$') { return @($script:rows | Where-Object ParentProcessId -eq ([int]$Matches[1])) }
    throw 'unexpected process inventory'
}
function Get-NetTCPConnection {
    if ($scenario -eq 'listener_inventory_denied') { throw [UnauthorizedAccessException]::new('synthetic listener denied') }
    if ($script:listener) { [pscustomobject]@{OwningProcess=$script:listener} }
}
function Test-BridgeReady {
    param($Port,$ProcessId,$Secure)
    $script:probed += $ProcessId
    return $ProcessId -eq 201
}
function Stop-Process {
    param($Id)
    $script:stopped += $Id
    if ($scenario -eq 'delayed_restart' -and $Id -eq 201) { $script:drain = 3; return }
    $script:rows = @($script:rows | Where-Object ProcessId -ne $Id)
    if ($Id -eq $script:listener) { $script:listener = $null }
    if ($scenario -eq 'autoexit' -and $Id -eq 201) { $script:rows = @() }
}
function Start-Sleep {
    if ($script:drain -gt 0) {
        $script:drain--
        if ($script:drain -eq 0) {
            $script:rows = @($script:rows | Where-Object ProcessId -ne 201)
            $script:listener = $null
        }
    }
}
function Wait-Process {}
function Get-Process { param($Id); return @($script:rows | Where-Object ProcessId -eq $Id) }
function Start-Process { $script:started = $true; throw 'unexpected new launch' }
if ($scenario -in @('inspection_denied','listener_inventory_denied')) {
    $rejected = $false
    try { Start-ManagedBridge 'test' 49123 '127.0.0.1' } catch { $rejected = $true }
    if (-not $rejected -or $script:stopped.Count -or $script:started) { throw 'inspection failure caused a process mutation' }
    exit 0
}
if ($scenario -in @('historical_release','historical_shared_runtime')) {
    $oldRelease = Join-Path $RuntimeRoot 'releases\old-payload'; $oldPython = Join-Path $RuntimeRoot 'environments\old-payload\Scripts\python.exe'
    New-Item -ItemType Directory -Force (Join-Path $oldRelease 'bin'), (Split-Path $oldPython -Parent) | Out-Null
    New-Item -ItemType File -Force (Join-Path $oldRelease 'bin\agent-bridge-mcp.py'), $oldPython | Out-Null
    ('base-executable = ' + $base) | Set-Content (Join-Path $RuntimeRoot 'environments\old-payload\pyvenv.cfg')
    $sharedPython = Join-Path $RuntimeRoot 'venv\Scripts\python.exe'
    if ($scenario -eq 'historical_shared_runtime') {
        New-Item -ItemType Directory -Force (Split-Path $sharedPython -Parent) | Out-Null
        New-Item -ItemType File -Force $sharedPython | Out-Null
        ('base-executable = ' + $base) | Set-Content (Join-Path $RuntimeRoot 'venv\pyvenv.cfg')
    }
    $oldInterpreter = if ($scenario -eq 'historical_shared_runtime') { $sharedPython } else { $oldPython }
    $oldCommand = '"' + $oldInterpreter + '" "' + (Join-Path $oldRelease 'bin\agent-bridge-mcp.py') + '" --port 49123'
    $script:rows[0] = Make-Process 200 100 $oldInterpreter 10 $oldCommand
    $script:rows[1] = Make-Process 201 200 $base 11 $oldCommand
}
if ($scenario -in @('ready_child','ready_busy')) {
    if ($scenario -eq 'ready_busy') { $script:rows += Make-Process 203 201 'C:\agent.exe' 12 'active agent' }
    '200' | Set-Content (Join-Path $BridgeHome 'test.pid')
    Start-ManagedBridge 'test' 49123 '127.0.0.1'
    if ($script:probed.Count -ne 1 -or $script:probed[0] -ne 201) { throw 'readiness did not use listener child' }
    if ($script:stopped.Count) { throw 'healthy server stopped' }
    exit 0
}
$tree = Get-OwnedBridgeTree 200
switch ($scenario) {
    'unknown_child' { $script:rows += Make-Process 203 200 'C:\unknown.exe' 12 'unknown' }
    'unknown_descendant' { $script:rows += Make-Process 203 201 'C:\unknown.exe' 12 'unknown' }
    'wrong_base' { $script:rows[1].ExecutablePath = 'C:\other\python.exe' }
    'wrong_script' { $script:rows[1].CommandLine = $command.Replace('agent-bridge-mcp.py', 'agent-bridge-mcp.py.backup') }
    'older_child' { $script:rows[1].CreationDate = 9 }
    'reused_child' { $script:rows[1] = Make-Process 201 200 $base 20 $command }
    'reused_root' { $script:rows[0] = Make-Process 200 100 $PythonExe 20 $command }
    'foreign_listener' { $script:listener = 999 }
}
if ($scenario -in @('restart','delayed_restart','historical_release','historical_shared_runtime','autoexit')) {
    Stop-OwnedBridgeTree $tree 49123
    $expected = if ($scenario -eq 'autoexit') {'201'} else {'201,200'}
    if (($script:stopped -join ',') -ne $expected) { throw ('wrong termination order: ' + ($script:stopped -join ',')) }
    if ($script:listener) { throw 'orphan listener' }
} else {
    $rejected = $false
    try { Stop-OwnedBridgeTree $tree 49123 } catch { $rejected = $true }
    if (-not $rejected -or $script:stopped.Count) { throw 'uncertain ownership was not rejected before stopping' }
}
''', encoding="utf-8")
    result = subprocess.run([shell, "-NoProfile", "-File", str(script), str(ROOT / "bin"),
                             str(tmp_path / "home"), scenario], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_real_windows_venv_listener_restart(tmp_path):
    """Launch only a temporary stdlib server; verify actual Windows redirector PIDs."""
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell or os.name != "nt":
        pytest.skip("Windows PowerShell required")
    visibility = subprocess.run([shell, "-NoProfile", "-Command",
        "Get-CimInstance Win32_Process -Filter \"ProcessId = $PID\" -ErrorAction Stop | Out-Null"],
        capture_output=True, text=True, timeout=20)
    if visibility.returncode:
        pytest.skip("CIM process inspection unavailable; do not launch an uninspectable test process")
    environment = tmp_path / "venv"
    venv.EnvBuilder(with_pip=False).create(environment)
    release = tmp_path / "release"
    (release / "bin").mkdir(parents=True)
    (release / "bin/agent-bridge-mcp.py").write_text(textwrap.dedent('''
        import json, os, sys
        from http.server import BaseHTTPRequestHandler, HTTPServer
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200); self.end_headers()
                self.wfile.write(json.dumps({"status":"ready", "process_id":os.getpid(),
                    "build_revision":os.environ["AGENT_BRIDGE_BUILD_REVISION"]}).encode())
            def log_message(self, *args): pass
        HTTPServer(("127.0.0.1", int(sys.argv[sys.argv.index("--port")+1])), Handler).serve_forever()
    '''), encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert port not in {18082, 18083, 18084, 18443}
    script = tmp_path / "smoke.ps1"
    script.write_text(r'''$ErrorActionPreference = 'Stop'
. (Join-Path $args[0] 'agent-bridge-launcher-common.ps1')
$BridgeHome = $args[1]; $LogDir = Join-Path $BridgeHome 'logs'
$RuntimeRoot = Join-Path $BridgeHome 'runtime'; $Release = Join-Path $BridgeHome 'release'
$LegacyHome = Join-Path $BridgeHome 'legacy'; $PythonExe = Join-Path $BridgeHome 'venv\Scripts\python.exe'
$BridgeScript = Join-Path $Release 'bin\agent-bridge-mcp.py'; $BuildRevision = 'first'
$env:AGENT_BRIDGE_BUILD_REVISION = $BuildRevision
$port = [int]$args[2]
try {
    Start-ManagedBridge 'smoke' $port '127.0.0.1'
    $rootId = [int](Get-Content (Join-Path $BridgeHome 'smoke.pid'))
    $tree = Get-OwnedBridgeTree $rootId
    $serverId = Get-BridgeListenerProcess $tree $port
    if ($serverId -eq $rootId -or $tree.Servers.Count -ne 2) { throw 'Windows venv redirector not exercised' }
    Start-ManagedBridge 'smoke' $port '127.0.0.1'
    $BuildRevision = 'second'; $env:AGENT_BRIDGE_BUILD_REVISION = $BuildRevision
    Start-ManagedBridge 'smoke' $port '127.0.0.1' -Restart
    $newRoot = [int](Get-Content (Join-Path $BridgeHome 'smoke.pid'))
    if ($newRoot -eq $rootId -or (Get-Process -Id $serverId -ErrorAction SilentlyContinue)) { throw 'old interpreter survived restart' }
    $tree = Get-OwnedBridgeTree $newRoot
    if (-not (Test-BridgeReady $port (Get-BridgeListenerProcess $tree $port) $false)) { throw 'replacement is not ready' }
} finally {
    if (Test-Path (Join-Path $BridgeHome 'smoke.pid')) {
        $cleanupId = [int](Get-Content (Join-Path $BridgeHome 'smoke.pid'))
        $cleanupTree = Get-OwnedBridgeTree $cleanupId
        if ($cleanupTree) { Stop-OwnedBridgeTree $cleanupTree $port }
    }
}
''', encoding="utf-8")
    # File capture avoids waiting for inherited pipe handles in Windows console
    # infrastructure if a failed cleanup leaves diagnostics to inspect.
    output = tmp_path / "smoke-output.txt"
    with output.open("w", encoding="utf-8") as log:
        result = subprocess.run([shell, "-NoProfile", "-File", str(script), str(ROOT / "bin"),
                                 str(tmp_path), str(port)], stdout=log, stderr=log, timeout=120)
    assert result.returncode == 0, output.read_text(encoding="utf-8")

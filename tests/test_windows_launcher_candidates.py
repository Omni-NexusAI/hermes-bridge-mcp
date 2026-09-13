"""Exercise candidate deduplication with real shells and synthetic ownership only."""
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("shell_name", ["windows-powershell-5.1", "pwsh"])
def test_distinct_release_interpreters_remain_owned(tmp_path, shell_name):
    if os.name != "nt":
        pytest.skip("Windows required")
    if shell_name == "windows-powershell-5.1":
        # Never let PATH (or the caller's exec shell) substitute PowerShell 7.
        shell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        if not shell.is_file():
            pytest.skip("Windows PowerShell 5.1 unavailable")
    else:
        shell = shutil.which("pwsh")
        if not shell:
            pytest.skip("PowerShell 7 unavailable")
    script = tmp_path / "candidates.ps1"
    script.write_text(r'''$ErrorActionPreference = 'Stop'
if ($args[2] -eq 'windows-powershell-5.1' -and $PSVersionTable.PSVersion.ToString() -notlike '5.1*') {
    throw 'Expected the actual Windows PowerShell 5.1 engine'
}
. $args[0]
$BridgeHome = $args[1]
$RuntimeRoot = Join-Path $BridgeHome 'runtime'
$LegacyHome = Join-Path $BridgeHome 'legacy'
$Release = Join-Path $RuntimeRoot 'releases\current'
$PythonExe = Join-Path $RuntimeRoot 'environments\current\Scripts\python.exe'
$oldRelease = Join-Path $RuntimeRoot 'releases\retained'
$oldPython = Join-Path $RuntimeRoot 'environments\retained\Scripts\python.exe'
$sharedPython = Join-Path $RuntimeRoot 'venv\Scripts\python.exe'
foreach ($path in @($PythonExe, $oldPython, $sharedPython,
    (Join-Path $Release 'bin\agent-bridge-mcp.py'),
    (Join-Path $oldRelease 'bin\agent-bridge-mcp.py'))) {
    New-Item -ItemType Directory -Force (Split-Path $path -Parent) | Out-Null
    New-Item -ItemType File -Force $path | Out-Null
}
# Selected and previous pairs are deliberately duplicated by retained discovery.
@{previous=$oldRelease; previous_python=$oldPython} | ConvertTo-Json |
    Set-Content (Join-Path $RuntimeRoot 'current.json')
$expected = @(
    [pscustomobject]@{release=$Release; python=$PythonExe},
    [pscustomobject]@{release=$Release; python=$sharedPython},
    [pscustomobject]@{release=$oldRelease; python=$oldPython},
    [pscustomobject]@{release=$oldRelease; python=$sharedPython},
    [pscustomobject]@{release=$BridgeHome; python=(Join-Path $LegacyHome 'hermes-agent\venv\Scripts\python.exe')}
)
$candidates = @(Get-BridgeProcessCandidates)
if ($candidates.Count -ne $expected.Count) { throw ('candidate count: ' + $candidates.Count) }
function Get-CimInstance { param($ClassName, $Filter); return $script:mockProcess }
# These fail closed if ownership ever starts using real process/network actions.
function Stop-Process { throw 'unexpected process mutation' }
function Start-Process { throw 'unexpected process mutation' }
function Get-NetTCPConnection { throw 'unexpected listener inspection' }
foreach ($pair in $expected) {
    $matches = @($candidates | Where-Object { $_.release -eq $pair.release -and $_.python -eq $pair.python })
    if ($matches.Count -ne 1) { throw 'missing pair or duplicate pair' }
    foreach ($name in @('agent-bridge-mcp.py', 'windows-hermes-proxy-mcp.py')) {
        $targetScript = Join-Path $pair.release ('bin\' + $name)
        $script:mockProcess = [pscustomobject]@{
            ProcessId=200; ExecutablePath=$pair.python
            CommandLine=('"' + $pair.python + '" "' + $targetScript + '"')
        }
        if ((Get-OwnedBridgeProcess 200).ProcessId -ne 200) { throw 'valid ownership rejected' }
        foreach ($fault in @('executable', 'script')) {
            $script:mockProcess.ExecutablePath = $pair.python
            $script:mockProcess.CommandLine = '"' + $pair.python + '" "' + $targetScript + '"'
            if ($fault -eq 'executable') { $script:mockProcess.ExecutablePath = Join-Path $BridgeHome 'foreign\python.exe' }
            else { $script:mockProcess.CommandLine = '"' + $pair.python + '" "' + $targetScript + '.backup"' }
            $rejected = $false
            try { Get-OwnedBridgeProcess 200 | Out-Null } catch { $rejected = $true }
            if (-not $rejected) { throw ('unsafe ownership accepted: ' + $fault) }
        }
    }
}
Write-Output ('verified engine ' + $PSVersionTable.PSVersion + ': five distinct pairs, duplicates removed, ownership guarded')
''', encoding="utf-8")
    result = subprocess.run(
        [str(shell), "-NoProfile", "-File", str(script),
         str(ROOT / "bin/agent-bridge-launcher-common.ps1"), str(tmp_path / "home"), shell_name],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr

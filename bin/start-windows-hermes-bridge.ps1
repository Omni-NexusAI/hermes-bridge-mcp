$ErrorActionPreference = "Stop"

$LegacyHome = Join-Path $env:LOCALAPPDATA "hermes"
$CanonicalHome = Join-Path $env:LOCALAPPDATA "agent-bridge"
$BridgeHome = if ($env:AGENT_BRIDGE_HOME) {
    $env:AGENT_BRIDGE_HOME
} elseif ($env:HERMES_BRIDGE_HOME) {
    $env:HERMES_BRIDGE_HOME
} elseif (Test-Path (Join-Path $LegacyHome "bridge-state")) {
    $LegacyHome
} else {
    $CanonicalHome
}
$RuntimeRoot = Join-Path $BridgeHome "bridge-runtime"
$Release = $null
$Marker = Join-Path $RuntimeRoot "current.json"
if (Test-Path $Marker) { $Release = (Get-Content $Marker -Raw | ConvertFrom-Json).release }
if (-not $Release -or -not (Test-Path $Release)) { $Release = $BridgeHome }
$PythonExe = Join-Path $RuntimeRoot "venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) { $PythonExe = Join-Path $LegacyHome "hermes-agent\venv\Scripts\python.exe" }
$BridgeScript = Join-Path $Release "bin\agent-bridge-mcp.py"
if (-not (Test-Path $BridgeScript)) { $BridgeScript = Join-Path $Release "bin\windows-hermes-proxy-mcp.py" }
$StateDir = if ($env:AGENT_BRIDGE_STATE_DIR) { $env:AGENT_BRIDGE_STATE_DIR } else { Join-Path $BridgeHome "bridge-state" }
$TokenPath = Join-Path $StateDir "local-mcp-token"
$LogDir = Join-Path $BridgeHome "logs"
$LogPath = Join-Path $LogDir "agent-bridge.log"
$ErrPath = Join-Path $LogDir "agent-bridge.err.log"
$PidPath = Join-Path $BridgeHome "agent-bridge.pid"
$Port = 18082

New-Item -ItemType Directory -Force -Path $LogDir, $StateDir | Out-Null
if (-not (Test-Path $TokenPath)) {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_') | Set-Content $TokenPath -NoNewline
}
$env:AGENT_BRIDGE_AUTH_TOKEN = (Get-Content $TokenPath -Raw).Trim()
$env:HERMES_BRIDGE_AUTH_TOKEN = $env:AGENT_BRIDGE_AUTH_TOKEN
# This is the local A0/Codex endpoint. Discovery may be enabled globally, but
# only the dedicated peer listener on 18443 is TLS-enabled.
$env:HERMES_BRIDGE_SECURE_NETWORK = "0"
$env:AGENT_BRIDGE_SECURE_NETWORK = "0"

if (Test-Path $PidPath) {
    $oldPid = Get-Content $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPid -and (Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue)) {
        try {
            $ready = Invoke-RestMethod "http://127.0.0.1:$Port/readyz" -TimeoutSec 2
            if ($ready.status -eq "ready") { Write-Output "OK: native bridge already ready as PID $oldPid"; return }
        } catch {}
        Stop-Process -Id ([int]$oldPid) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
}

$arguments = @($BridgeScript, "--transport", "streamable-http", "--host", "0.0.0.0", "--port", "$Port", "--stateless-http", "--json-response")
$process = Start-Process -FilePath $PythonExe -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput $LogPath -RedirectStandardError $ErrPath -PassThru
$process.Id | Set-Content $PidPath -NoNewline
Start-Sleep -Seconds 2
try {
    $ready = Invoke-RestMethod "http://127.0.0.1:$Port/readyz" -TimeoutSec 3
    if ($ready.status -ne "ready") { throw "unexpected readiness response" }
    Write-Output "OK: native bridge PID=$($process.Id) ready on port $Port"
} catch {
    Write-Output "WARN: native bridge PID=$($process.Id) did not become ready"
    Get-Content $ErrPath -Tail 40 -ErrorAction SilentlyContinue
}

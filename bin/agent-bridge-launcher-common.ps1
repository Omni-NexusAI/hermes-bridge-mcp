# Shared by canonical and legacy launchers. Dot-sourcing performs no mutation.
function Get-BridgeSetting([string]$Name, $Default) {
    $value = [Environment]::GetEnvironmentVariable("AGENT_BRIDGE_$Name")
    if (-not $value) { $value = [Environment]::GetEnvironmentVariable("HERMES_BRIDGE_$Name") }
    if ($value) { return $value }; return $Default
}

function Initialize-BridgeRuntime {
    $script:LegacyHome = Join-Path $env:LOCALAPPDATA "hermes"
    $defaultHome = if (Test-Path (Join-Path $LegacyHome "bridge-state")) { $LegacyHome } else { Join-Path $env:LOCALAPPDATA "agent-bridge" }
    $script:BridgeHome = Get-BridgeSetting "HOME" $defaultHome
    $script:RuntimeRoot = Get-BridgeSetting "INSTALL_ROOT" (Join-Path $BridgeHome "bridge-runtime")
    $script:StateDir = Get-BridgeSetting "STATE_DIR" (Join-Path $BridgeHome "bridge-state")
    $script:LogDir = Join-Path $BridgeHome "logs"
    $markerPath = Join-Path $RuntimeRoot "current.json"
    $marker = if (Test-Path $markerPath) { Get-Content $markerPath -Raw | ConvertFrom-Json } else { $null }
    $script:Release = if ($marker) { $marker.release } else { $BridgeHome }
    if (-not (Test-Path -LiteralPath $Release)) { throw "Selected bridge release is missing. Run bootstrap.py doctor or rollback." }
    $script:PythonExe = if ($marker.python) { $marker.python } else { Join-Path $RuntimeRoot "venv\Scripts\python.exe" }
    if (-not (Test-Path -LiteralPath $PythonExe) -and -not $marker.python) { $script:PythonExe = Join-Path $LegacyHome "hermes-agent\venv\Scripts\python.exe" }
    if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Bridge Python is missing. Run bootstrap.py install with Python 3.10+; Hermes is optional." }
    $script:BridgeScript = Join-Path $Release "bin\agent-bridge-mcp.py"
    if (-not (Test-Path -LiteralPath $BridgeScript)) { $script:BridgeScript = Join-Path $Release "bin\windows-hermes-proxy-mcp.py" }
    if (-not (Test-Path -LiteralPath $BridgeScript)) { throw "Selected bridge payload is incomplete." }
    $script:BuildRevision = if ($marker) { Split-Path $Release -Leaf } else { "source" }
    $env:AGENT_BRIDGE_BUILD_REVISION = $BuildRevision
    $env:AGENT_BRIDGE_HOME = $BridgeHome
    $env:AGENT_BRIDGE_STATE_DIR = $StateDir
}

function Test-BridgeProcessOwnership($Process, [string]$ExpectedScript, [string]$ExpectedPython) {
    if (-not $Process -or -not $Process.ExecutablePath -or -not $Process.CommandLine) { return $false }
    if (-not [string]::Equals([IO.Path]::GetFullPath($Process.ExecutablePath), [IO.Path]::GetFullPath($ExpectedPython), [StringComparison]::OrdinalIgnoreCase)) { return $false }
    # Exact argument boundaries, not a substring match (e.g. foo.py.backup).
    $scriptPattern = '(?:^|\s)"?' + [regex]::Escape($ExpectedScript) + '"?(?=\s|$)'
    return $Process.CommandLine -match $scriptPattern
}

function Get-OwnedBridgeProcess([int]$ProcessId) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
    if (-not $process) { return $null }
    # Accept the selected release, its explicit rollback release, and installed
    # compatibility wrappers. Never infer ownership from process name or PID alone.
    $candidates = @(@{ release = $Release; python = $PythonExe })
    $markerPath = Join-Path $RuntimeRoot "current.json"
    if (Test-Path $markerPath) {
        $marker = Get-Content $markerPath -Raw | ConvertFrom-Json
        if ($marker.previous) { $candidates += @{ release = $marker.previous; python = $(if ($marker.previous_python) { $marker.previous_python } else { Join-Path $RuntimeRoot "venv\Scripts\python.exe" }) } }
    }
    $candidates += @{ release = $BridgeHome; python = (Join-Path $LegacyHome "hermes-agent\venv\Scripts\python.exe") }
    foreach ($candidate in $candidates) {
        foreach ($name in @("agent-bridge-mcp.py", "windows-hermes-proxy-mcp.py")) {
            if (Test-BridgeProcessOwnership $process (Join-Path $candidate.release "bin\$name") $candidate.python) { return $process }
        }
    }
    throw "PID $ProcessId does not have verified bridge executable and script ownership; leaving it running."
}

function Test-BridgeReady([int]$Port, [int]$ProcessId, [bool]$Secure) {
    $probe = Join-Path $PSScriptRoot "agent_bridge_runtime.py"
    $probeArgs = @($probe, "--port", "$Port", "--pid", "$ProcessId", "--revision", $BuildRevision)
    if ($Secure) { $probeArgs += "--secure" }
    $result = & $PythonExe @probeArgs | ConvertFrom-Json
    return ($LASTEXITCODE -eq 0 -and $result.ready)
}

function Start-ManagedBridge([string]$Name, [int]$Port, [string]$BindAddress, [bool]$Secure = $false, [switch]$Restart) {
    New-Item -ItemType Directory -Force -Path $BridgeHome, $LogDir | Out-Null
    $launchLock = $null
    try { $launchLock = [IO.File]::Open((Join-Path $BridgeHome "launcher.lock"), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw "Another bridge launcher owns startup. Retry after it finishes." }
    try {
        $pidPath = Join-Path $BridgeHome "$Name.pid"
        $oldId = 0
        if (Test-Path $pidPath) {
            if (-not [int]::TryParse((Get-Content $pidPath -Raw).Trim(), [ref]$oldId)) { throw "Invalid bridge PID record; inspect it before restarting." }
            $oldProcess = Get-OwnedBridgeProcess $oldId
            if ($oldProcess) {
                if (Test-BridgeReady $Port $oldId $Secure) { Write-Output "OK: $Name PID=$oldId ready at release $BuildRevision"; return }
                if (-not $Restart) { throw "Bridge PID $oldId has another release or is not ready. Checkpoint active tasks, then explicitly invoke this launcher with -Restart." }
                # Recheck ownership immediately before acting on a reused PID.
                $confirmed = Get-OwnedBridgeProcess $oldId
                if ($confirmed -and $confirmed.CreationDate -eq $oldProcess.CreationDate) {
                    Stop-Process -Id $oldId -ErrorAction Stop
                    Wait-Process -Id $oldId -Timeout 10 -ErrorAction SilentlyContinue
                    if (Get-Process -Id $oldId -ErrorAction SilentlyContinue) { throw "Owned bridge did not exit; no second listener was started." }
                }
            }
        }
        $listeners = @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
        if ($listeners.Count) { throw "Port $Port already has a listener without a verified matching PID record; no process was stopped." }
        $env:AGENT_BRIDGE_SECURE_NETWORK = if ($Secure) { "1" } else { "0" }
        $env:HERMES_BRIDGE_SECURE_NETWORK = $env:AGENT_BRIDGE_SECURE_NETWORK
        $arguments = @(('"' + $BridgeScript + '"'), "--transport", "streamable-http", "--host", $BindAddress, "--port", "$Port", "--stateless-http", "--json-response")
        if ($Secure) { $arguments += "--secure-network" }
        $process = Start-Process -FilePath $PythonExe -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "$Name.log") -RedirectStandardError (Join-Path $LogDir "$Name.err.log") -PassThru
        $process.Id | Set-Content $pidPath -NoNewline
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            if ($process.HasExited) { throw "$Name exited before readiness; inspect its local error log." }
            if (Test-BridgeReady $Port $process.Id $Secure) { Write-Output "OK: $Name PID=$($process.Id) ready on port $Port at release $BuildRevision"; return }
            Start-Sleep -Milliseconds 500
            $process.Refresh()
        }
        throw "$Name did not report matching process, release and readiness; PID record retained for inspection."
    } finally { $launchLock.Dispose() }
}

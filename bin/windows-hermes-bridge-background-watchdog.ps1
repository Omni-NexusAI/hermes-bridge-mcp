$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "agent-bridge-launcher-common.ps1")
Initialize-BridgeRuntime
New-Item -ItemType Directory -Force -Path $BridgeHome, $LogDir | Out-Null
try { $owner = [IO.File]::Open((Join-Path $BridgeHome "watchdog.lock"), 'OpenOrCreate', 'ReadWrite', 'None') }
catch { Write-Output "Another canonical or compatibility watchdog already owns this bridge home."; return }
try {
    while ($true) {
        # Launchers verify readiness, runtime revision and process ownership.
        # They never interrupt live work merely because a probe failed.
        foreach ($launcher in @("start-agent-bridge.ps1", "start-agent-bridge-peer.ps1")) {
            try { & (Join-Path $PSScriptRoot $launcher) | Out-Null }
            catch { Add-Content (Join-Path $LogDir "bridge-background-watchdog.log") "[$(Get-Date -Format o)] $launcher requires inspection: $($_.Exception.Message)" }
        }
        Start-Sleep -Seconds 60
    }
} finally { $owner.Dispose() }

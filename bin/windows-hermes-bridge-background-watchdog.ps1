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
$LocalStart = Join-Path $BridgeHome "bin\start-agent-bridge.ps1"
if (-not (Test-Path -LiteralPath $LocalStart)) {
    $LocalStart = Join-Path $BridgeHome "bin\start-windows-hermes-bridge.ps1"
}
$PeerStart = Join-Path $BridgeHome "bin\start-agent-bridge-peer.ps1"
if (-not (Test-Path -LiteralPath $PeerStart)) {
    $PeerStart = Join-Path $BridgeHome "bin\start-windows-hermes-peer-bridge.ps1"
}
$LogDir = Join-Path $BridgeHome "logs"
$LogPath = Join-Path $LogDir "bridge-background-watchdog.log"
New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

function Test-Ready([string]$Url) {
    try { $response = Invoke-RestMethod $Url -TimeoutSec 3; return $response.status -in @("ready", "ok") } catch { return $false }
}

function Test-Listener([int]$Port) {
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $connected = $client.ConnectAsync("127.0.0.1", $Port).Wait(2000)
        $client.Dispose()
        return $connected
    } catch { return $false }
}

while ($true) {
    if (-not (Test-Ready "http://127.0.0.1:18082/readyz")) {
        Add-Content $LogPath "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Local bridge unhealthy; restarting"
        & $LocalStart | Out-Null
    }
    $DiscoveryEnabled = if ($null -ne $env:AGENT_BRIDGE_AUTO_DISCOVERY) {
        $env:AGENT_BRIDGE_AUTO_DISCOVERY -ne "0"
    } else {
        $env:HERMES_BRIDGE_AUTO_DISCOVERY -ne "0"
    }
    if ($DiscoveryEnabled -and -not (Test-Listener 18443)) {
        Add-Content $LogPath "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Secure peer bridge unhealthy; restarting"
        & $PeerStart | Out-Null
    }
    Start-Sleep -Seconds 60
}

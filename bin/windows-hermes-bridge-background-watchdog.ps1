$HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
$LocalStart = Join-Path $HermesHome "bin\start-windows-hermes-bridge.ps1"
$PeerStart = Join-Path $HermesHome "bin\start-windows-hermes-peer-bridge.ps1"
$LogDir = Join-Path $HermesHome "logs"
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
    if ($env:HERMES_BRIDGE_AUTO_DISCOVERY -ne "0" -and -not (Test-Listener 18443)) {
        Add-Content $LogPath "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Secure peer bridge unhealthy; restarting"
        & $PeerStart | Out-Null
    }
    Start-Sleep -Seconds 60
}

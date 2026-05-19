$HermesHome = "$env:LOCALAPPDATA\hermes"
$PidPath = Join-Path $HermesHome "windows-bridge-supergateway.pid"
$Port = 18082
$StartScript = Join-Path $HermesHome "bin\start-windows-hermes-bridge.ps1"
$LogDir = Join-Path $HermesHome "logs"
$LogPath = Join-Path $LogDir "bridge-background-watchdog.log"

function Test-PortOpen {
    param([int]$PortToCheck)
    try {
        $client = [System.Net.Sockets.TcpClient]::new()
        $task = $client.ConnectAsync('127.0.0.1', $PortToCheck)
        if (-not $task.Wait(2000)) { $client.Dispose(); return $false }
        $client.Dispose()
        return $true
    } catch {
        return $false
    }
}

function Is-BridgeAlive {
    if (-not (Test-PortOpen -PortToCheck $Port)) { return $false }
    if (-not (Test-Path $PidPath)) { return $true }
    $pidValue = Get-Content $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pidValue) { return $true }
    return [bool](Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue)
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

while ($true) {
    if (-not (Is-BridgeAlive)) {
        $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $LogPath -Value "[$timestamp] Bridge down, restarting..."
        & $StartScript | Out-Null
    }
    Start-Sleep -Seconds 60
}


$HermesHome = "$env:LOCALAPPDATA\hermes"
$PidPath = Join-Path $HermesHome "windows-hermes-gateway.pid"
$StartScript = Join-Path $HermesHome "bin\start-windows-hermes-gateway.ps1"
$LogDir = Join-Path $HermesHome "logs"
$LogPath = Join-Path $LogDir "gateway-background-watchdog.log"

function Is-GatewayAlive {
    if (-not (Test-Path $PidPath)) { return $false }
    $pidValue = Get-Content $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $pidValue) { return $false }
    return [bool](Get-Process -Id ([int]$pidValue) -ErrorAction SilentlyContinue)
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

while ($true) {
    if (-not (Is-GatewayAlive)) {
        $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
        Add-Content -Path $LogPath -Value "[$timestamp] Gateway down, restarting..."
        & $StartScript | Out-Null
    }
    Start-Sleep -Seconds 60
}


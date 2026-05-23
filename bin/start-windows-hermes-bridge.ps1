param(
    [switch]$Restart
)

$ErrorActionPreference = "Stop"

$HermesExe = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\hermes.exe"
$HermesHome = "$env:LOCALAPPDATA\hermes"
$BridgeCmd = Join-Path $HermesHome "bin\windows-hermes-mcp-serve.cmd"
$LogDir = Join-Path $HermesHome "logs"
$LogPath = Join-Path $LogDir "windows-bridge-supergateway.log"
$ErrPath = Join-Path $LogDir "windows-bridge-supergateway.err.log"
$PidPath = Join-Path $HermesHome "windows-bridge-supergateway.pid"
$Port = 18082

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

function Get-ListenerPid {
    param([int]$PortToCheck)
    $lines = cmd /c "netstat -ano | findstr :$PortToCheck" 2>$null
    foreach ($line in $lines) {
        if ($line -match "LISTENING\s+(\d+)\s*$") { return [int]$Matches[1] }
    }
    return $null
}

function Stop-ProcessTree {
    param([int]$ProcessId)
    if ($ProcessId -le 0) { return }
    if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) { return }
    & "$env:WINDIR\System32\taskkill.exe" /PID $ProcessId /T /F *>$null
}

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

$listenerPid = Get-ListenerPid -PortToCheck $Port
$oldPid = $null
if (Test-Path $PidPath) {
    $oldPidRaw = Get-Content $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPidRaw) { $oldPid = [int]$oldPidRaw }
}

if ($listenerPid -and -not $Restart) {
    if ($oldPid -ne $listenerPid) {
        $listenerPid | Set-Content -Path $PidPath -NoNewline
    }
    Write-Output "OK: existing bridge PID=$listenerPid is already listening on port $Port"
    return
}

if ($oldPid) { Stop-ProcessTree -ProcessId $oldPid }
if ($listenerPid -and ($listenerPid -ne $oldPid)) { Stop-ProcessTree -ProcessId $listenerPid }
Remove-Item $PidPath -Force -ErrorAction SilentlyContinue

Start-Sleep -Seconds 1

$supergateway = (Get-Command supergateway.cmd -ErrorAction SilentlyContinue).Source
if (-not $supergateway) {
    $supergateway = (Get-Command supergateway -ErrorAction Stop).Source
}

if (Test-Path $LogPath) { Remove-Item $LogPath -Force -ErrorAction SilentlyContinue }
if (Test-Path $ErrPath) { Remove-Item $ErrPath -Force -ErrorAction SilentlyContinue }

$arguments = @(
    "--stdio", "`"$BridgeCmd`"",
    "--port", "$Port",
    "--baseUrl", "http://localhost:$Port",
    "--outputTransport", "streamableHttp",
    "--streamableHttpPath", "/mcp",
    "--stateful",
    "--sessionTimeout", "600000",
    "--healthEndpoint", "/healthz",
    "--logLevel", "info"
)

$process = Start-Process -FilePath $supergateway -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput $LogPath -RedirectStandardError $ErrPath -PassThru

Start-Sleep -Seconds 3

if (Test-PortOpen -PortToCheck $Port) {
    $newListenerPid = Get-ListenerPid -PortToCheck $Port
    if ($newListenerPid) { $newListenerPid | Set-Content -Path $PidPath -NoNewline }
    Write-Output "OK: supergateway PID=$newListenerPid listening on port $Port"
} else {
    Write-Output "WARN: supergateway PID=$($process.Id) started but port $Port is not reachable"
    Write-Output "--- stderr (last 20 lines) ---"
    Get-Content $ErrPath -Tail 20 -ErrorAction SilentlyContinue
}


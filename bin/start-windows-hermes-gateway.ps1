$ErrorActionPreference = "Stop"

$HermesExe = "$env:LOCALAPPDATA\hermes\hermes-agent\venv\Scripts\hermes.exe"
$HermesHome = "$env:LOCALAPPDATA\hermes"
$LogDir = Join-Path $HermesHome "logs"
$LogPath = Join-Path $LogDir "windows-hermes-gateway.log"
$ErrPath = Join-Path $LogDir "windows-hermes-gateway.err.log"
$PidPath = Join-Path $HermesHome "windows-hermes-gateway.pid"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null

if (Test-Path $PidPath) {
    $oldPid = Get-Content $PidPath -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($oldPid) {
        $proc = Get-Process -Id ([int]$oldPid) -ErrorAction SilentlyContinue
        if ($proc) {
            Write-Output "OK: Windows Hermes gateway already running as PID $oldPid"
            return
        }
    }
    Remove-Item $PidPath -Force -ErrorAction SilentlyContinue
}

if (Test-Path $LogPath) { Remove-Item $LogPath -Force -ErrorAction SilentlyContinue }
if (Test-Path $ErrPath) { Remove-Item $ErrPath -Force -ErrorAction SilentlyContinue }

$process = Start-Process -FilePath $HermesExe -ArgumentList @("gateway", "--accept-hooks", "run") -WindowStyle Hidden -RedirectStandardOutput $LogPath -RedirectStandardError $ErrPath -PassThru
$process.Id | Set-Content -Path $PidPath -NoNewline
Start-Sleep -Seconds 5

if (Get-Process -Id $process.Id -ErrorAction SilentlyContinue) {
    Write-Output "OK: Windows Hermes gateway started as PID $($process.Id)"
} else {
    Write-Output "WARN: Windows Hermes gateway process exited after start"
    Write-Output "--- stderr (last 40 lines) ---"
    Get-Content $ErrPath -Tail 40 -ErrorAction SilentlyContinue
}

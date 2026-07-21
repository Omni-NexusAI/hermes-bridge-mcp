$ErrorActionPreference = "Stop"
$env:HERMES_BRIDGE_AUTH_TOKEN = if ($env:HERMES_BRIDGE_STAGING_TOKEN) { $env:HERMES_BRIDGE_STAGING_TOKEN } else { "staging-local-only" }
$HermesHome = Join-Path $env:LOCALAPPDATA "hermes"
$RuntimeRoot = Join-Path $HermesHome "bridge-runtime"
$Marker = Join-Path $RuntimeRoot "current.json"
$Release = if (Test-Path $Marker) { (Get-Content $Marker -Raw | ConvertFrom-Json).release } else { $HermesHome }
$PythonExe = Join-Path $RuntimeRoot "venv\Scripts\python.exe"
if (-not (Test-Path $PythonExe)) { $PythonExe = Join-Path $HermesHome "hermes-agent\venv\Scripts\python.exe" }
$BridgeScript = Join-Path $Release "bin\windows-hermes-proxy-mcp.py"
if (-not (Test-Path $BridgeScript)) { $BridgeScript = Join-Path $HermesHome "bin\windows-hermes-proxy-mcp.py" }
$LogDir = Join-Path $HermesHome "logs"
New-Item -ItemType Directory -Force $LogDir | Out-Null
$arguments = @($BridgeScript, "--transport", "streamable-http", "--host", "127.0.0.1", "--port", "18083", "--stateless-http", "--json-response")
$process = Start-Process -FilePath $PythonExe -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "windows-bridge-staging.log") -RedirectStandardError (Join-Path $LogDir "windows-bridge-staging.err.log") -PassThru
$process.Id | Set-Content (Join-Path $HermesHome "windows-bridge-staging.pid") -NoNewline
Start-Sleep -Seconds 2
try { Invoke-RestMethod "http://127.0.0.1:18083/readyz" -TimeoutSec 3 | Out-Null; Write-Output "OK: native staging bridge PID=$($process.Id) ready on port 18083" } catch { Write-Output "WARN: staging bridge did not become ready" }

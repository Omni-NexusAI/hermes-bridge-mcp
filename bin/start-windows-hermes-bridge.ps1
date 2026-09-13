param([switch]$Restart)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "agent-bridge-launcher-common.ps1")
Initialize-BridgeRuntime
$TokenPath = Join-Path $StateDir "local-mcp-token"
New-Item -ItemType Directory -Force -Path $StateDir | Out-Null
if (-not (Test-Path $TokenPath)) {
    $bytes = New-Object byte[] 32
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $rng.GetBytes($bytes)
    $rng.Dispose()
    $token = [Convert]::ToBase64String($bytes).TrimEnd('=').Replace('+','-').Replace('/','_')
    # CreateNew ensures concurrent launchers cannot replace the other's token.
    try { $file = [IO.File]::Open($TokenPath, 'CreateNew', 'Write', 'None') }
    catch { if (-not (Test-Path $TokenPath)) { throw } }
    if ($file) { try { $data = [Text.Encoding]::UTF8.GetBytes($token); $file.Write($data, 0, $data.Length) } finally { $file.Dispose() } }
}
$env:AGENT_BRIDGE_AUTH_TOKEN = (Get-Content $TokenPath -Raw).Trim()
if (-not $env:AGENT_BRIDGE_AUTH_TOKEN) { throw "Bridge token is empty; inspect token initialization before starting." }
$env:HERMES_BRIDGE_AUTH_TOKEN = $env:AGENT_BRIDGE_AUTH_TOKEN
$Port = [int](Get-BridgeSetting "LOCAL_PORT" 18082)
Start-ManagedBridge -Name "agent-bridge" -Port $Port -BindAddress "0.0.0.0" -Restart:$Restart

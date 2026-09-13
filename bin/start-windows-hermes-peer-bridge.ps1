param([switch]$Restart)
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "agent-bridge-launcher-common.ps1")
Initialize-BridgeRuntime
$HostAddress = Get-BridgeSetting "HOST" "0.0.0.0"
$LegacyPort = [int](Get-BridgeSetting "PORT" 18084)
$SecurePort = [int](Get-BridgeSetting "SECURE_PORT" 18443)
$DiscoveryEnabled = (Get-BridgeSetting "AUTO_DISCOVERY" "1") -ne "0"
$PairKeyConfigured = (Get-BridgeSetting "PAIR_KEY" "") -or (Get-BridgeSetting "AUTH_TOKEN" "")
if ($PairKeyConfigured) {
    Start-ManagedBridge -Name "agent-peer-bridge" -Port $LegacyPort -BindAddress $HostAddress -Restart:$Restart
} elseif (-not $DiscoveryEnabled) {
    throw "Set AGENT_BRIDGE_PAIR_KEY for legacy peers or AGENT_BRIDGE_AUTO_DISCOVERY=1 for automatic pairing."
} else {
    Write-Output "INFO: legacy HTTP peer listener skipped because no shared pair key is configured"
}
if ($DiscoveryEnabled) {
    Start-ManagedBridge -Name "agent-secure-peer-bridge" -Port $SecurePort -BindAddress $HostAddress -Secure $true -Restart:$Restart
} else {
    Write-Output "INFO: secure discovery listener disabled by AGENT_BRIDGE_AUTO_DISCOVERY=0"
}

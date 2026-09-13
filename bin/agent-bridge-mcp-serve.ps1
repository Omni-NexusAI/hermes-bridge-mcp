$ErrorActionPreference = "Stop"
$env:PYTHONPATH = ""
$env:PYTHONHOME = ""
. (Join-Path $PSScriptRoot "agent-bridge-launcher-common.ps1")
Initialize-BridgeRuntime
& $PythonExe $BridgeScript @args
exit $LASTEXITCODE

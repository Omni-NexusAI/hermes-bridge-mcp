@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0agent-bridge-mcp-serve.ps1" %*

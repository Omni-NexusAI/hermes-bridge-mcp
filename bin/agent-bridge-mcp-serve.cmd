@echo off
set PYTHONPATH=
set PYTHONHOME=
set "BRIDGE_HOME=%AGENT_BRIDGE_HOME%"
if "%BRIDGE_HOME%"=="" if not "%HERMES_BRIDGE_HOME%"=="" set "BRIDGE_HOME=%HERMES_BRIDGE_HOME%"
if "%BRIDGE_HOME%"=="" if exist "%LOCALAPPDATA%\hermes\bridge-state" set "BRIDGE_HOME=%LOCALAPPDATA%\hermes"
if "%BRIDGE_HOME%"=="" set "BRIDGE_HOME=%LOCALAPPDATA%\agent-bridge"
set "BRIDGE_PYTHON=%BRIDGE_HOME%\bridge-runtime\venv\Scripts\python.exe"
if not exist "%BRIDGE_PYTHON%" set "BRIDGE_PYTHON=%LOCALAPPDATA%\hermes\hermes-agent\venv\Scripts\python.exe"
"%BRIDGE_PYTHON%" "%~dp0agent-bridge-mcp.py"

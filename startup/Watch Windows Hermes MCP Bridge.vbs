Set shell = CreateObject("WScript.Shell")
scriptPath = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\hermes\bin\windows-hermes-bridge-background-watchdog.ps1")
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & scriptPath & """", 0, False

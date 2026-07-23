Set shell = CreateObject("WScript.Shell")
canonical = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\agent-bridge\bin\agent-bridge-background-watchdog.ps1")
legacy = shell.ExpandEnvironmentStrings("%LOCALAPPDATA%\hermes\bin\agent-bridge-background-watchdog.ps1")
If CreateObject("Scripting.FileSystemObject").FileExists(canonical) Then
  scriptPath = canonical
Else
  scriptPath = legacy
End If
shell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """ & scriptPath & """", 0, False

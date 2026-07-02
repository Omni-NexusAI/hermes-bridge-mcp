# install-skills.ps1 — Cross-platform skill deployment for Hermes Bridge MCP.
#
# Windows PowerShell version. Copies all skills from the repo's skills/
# directory into the Hermes agent's skills directory.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/install-skills.ps1

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot = Split-Path -Parent $ScriptDir
$SkillsSrc = Join-Path $RepoRoot "skills"

# Detect HERMES_HOME
if ($env:HERMES_HOME) {
    $SkillsDest = Join-Path $env:HERMES_HOME "skills"
} elseif ($env:LOCALAPPDATA -and (Test-Path (Join-Path $env:LOCALAPPDATA "hermes"))) {
    $SkillsDest = Join-Path $env:LOCALAPPDATA "hermes\skills"
} elseif (Test-Path "$env:USERPROFILE\.hermes") {
    $SkillsDest = Join-Path $env:USERPROFILE ".hermes\skills"
} else {
    $SkillsDest = Join-Path $env:LOCALAPPDATA "hermes\skills"
}

Write-Host "Source:   $SkillsSrc"
Write-Host "Target:   $SkillsDest"

if (-not (Test-Path $SkillsSrc)) {
    Write-Error "No skills/ directory found in repo root ($RepoRoot)"
    exit 1
}

New-Item -ItemType Directory -Force -Path $SkillsDest | Out-Null

# Copy each skill directory
$skillCount = 0
Get-ChildItem -Path $SkillsSrc -Directory | ForEach-Object {
    $skillName = $_.Name
    $destDir = Join-Path $SkillsDest $skillName
    New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    Copy-Item -Path (Join-Path $_.FullName "*") -Destination $destDir -Recurse -Force
    $skillCount++
    Write-Host "  Installed: $skillName"
}

if ($skillCount -eq 0) {
    Write-Warning "No skill directories found in $SkillsSrc"
} else {
    Write-Host "Installed $skillCount skill(s) to $SkillsDest"
    Write-Host ""
    Write-Host "Reload skills in your agent with:"
    Write-Host "  hermes skills reload    (Hermes)"
    Write-Host "  /reload-skills          (in-session)"
}

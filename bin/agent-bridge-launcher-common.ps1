# Shared by canonical and legacy launchers. Dot-sourcing performs no mutation.
function Get-BridgeSetting([string]$Name, $Default) {
    $value = [Environment]::GetEnvironmentVariable("AGENT_BRIDGE_$Name")
    if (-not $value) { $value = [Environment]::GetEnvironmentVariable("HERMES_BRIDGE_$Name") }
    if ($value) { return $value }; return $Default
}

function Initialize-BridgeRuntime {
    $script:LegacyHome = Join-Path $env:LOCALAPPDATA "hermes"
    $defaultHome = if (Test-Path (Join-Path $LegacyHome "bridge-state")) { $LegacyHome } else { Join-Path $env:LOCALAPPDATA "agent-bridge" }
    $script:BridgeHome = Get-BridgeSetting "HOME" $defaultHome
    $script:RuntimeRoot = Get-BridgeSetting "INSTALL_ROOT" (Join-Path $BridgeHome "bridge-runtime")
    $script:StateDir = Get-BridgeSetting "STATE_DIR" (Join-Path $BridgeHome "bridge-state")
    $script:LogDir = Join-Path $BridgeHome "logs"
    $markerPath = Join-Path $RuntimeRoot "current.json"
    $marker = if (Test-Path $markerPath) { Get-Content $markerPath -Raw | ConvertFrom-Json } else { $null }
    $script:Release = if ($marker) { $marker.release } else { $BridgeHome }
    if (-not (Test-Path -LiteralPath $Release)) { throw "Selected bridge release is missing. Run bootstrap.py doctor or rollback." }
    $script:PythonExe = if ($marker.python) { $marker.python } else { Join-Path $RuntimeRoot "venv\Scripts\python.exe" }
    if (-not (Test-Path -LiteralPath $PythonExe) -and -not $marker.python) { $script:PythonExe = Join-Path $LegacyHome "hermes-agent\venv\Scripts\python.exe" }
    if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Bridge Python is missing. Run bootstrap.py install with Python 3.10+; Hermes is optional." }
    $script:BridgeScript = Join-Path $Release "bin\agent-bridge-mcp.py"
    if (-not (Test-Path -LiteralPath $BridgeScript)) { $script:BridgeScript = Join-Path $Release "bin\windows-hermes-proxy-mcp.py" }
    if (-not (Test-Path -LiteralPath $BridgeScript)) { throw "Selected bridge payload is incomplete." }
    $script:BuildRevision = if ($marker) { Split-Path $Release -Leaf } else { "source" }
    $env:AGENT_BRIDGE_BUILD_REVISION = $BuildRevision
    $env:AGENT_BRIDGE_HOME = $BridgeHome
    $env:AGENT_BRIDGE_STATE_DIR = $StateDir
}

function Test-BridgeProcessOwnership($Process, [string]$ExpectedScript, [string]$ExpectedPython) {
    if (-not $Process -or -not $Process.ExecutablePath -or -not $Process.CommandLine) { return $false }
    if (-not [string]::Equals([IO.Path]::GetFullPath($Process.ExecutablePath), [IO.Path]::GetFullPath($ExpectedPython), [StringComparison]::OrdinalIgnoreCase)) { return $false }
    # Exact argument boundaries, not a substring match (e.g. foo.py.backup).
    $scriptPattern = '(?:^|\s)"?' + [regex]::Escape($ExpectedScript) + '"?(?=\s|$)'
    return $Process.CommandLine -match $scriptPattern
}

function Get-BridgeProcessCandidates {
    # Accept installed content-addressed releases as well as the selected release
    # and compatibility wrappers. An update may advance current.json twice while a
    # verified peer listener is still draining an older retained payload; limiting
    # ownership to one rollback generation strands that listener on its old venv.
    # Every retained candidate must still match its exact script and interpreter;
    # never infer ownership from a process name or PID alone.
    $candidates = @(@{ release = $Release; python = $PythonExe })
    $markerPath = Join-Path $RuntimeRoot "current.json"
    if (Test-Path $markerPath) {
        $marker = Get-Content $markerPath -Raw | ConvertFrom-Json
        if ($marker.previous) { $candidates += @{ release = $marker.previous; python = $(if ($marker.previous_python) { $marker.previous_python } else { Join-Path $RuntimeRoot "venv\Scripts\python.exe" }) } }
    }
    $releaseRoot = Join-Path $RuntimeRoot 'releases'
    $environmentRoot = Join-Path $RuntimeRoot 'environments'
    if (Test-Path -LiteralPath $releaseRoot) {
        foreach ($entry in @(Get-ChildItem -LiteralPath $releaseRoot -Directory -Force -ErrorAction Stop)) {
            # Do not traverse a junction supplied outside the managed payload root.
            if ($entry.Attributes -band [IO.FileAttributes]::ReparsePoint) { continue }
            $candidateRelease = $entry.FullName
            $candidatePython = Join-Path $environmentRoot (Join-Path $entry.Name 'Scripts\python.exe')
            $sharedRuntimePython = Join-Path $RuntimeRoot 'venv\Scripts\python.exe'
            $candidateScript = Join-Path $candidateRelease 'bin\agent-bridge-mcp.py'
            $legacyScript = Join-Path $candidateRelease 'bin\windows-hermes-proxy-mcp.py'
            if ((Test-Path -LiteralPath $candidateScript) -or (Test-Path -LiteralPath $legacyScript)) {
                # Pre-content-addressed installations retained one managed shared
                # venv. Keep it eligible only for a script inside this managed
                # release root, so a later update can converge that listener too.
                foreach ($interpreter in @($candidatePython, $sharedRuntimePython)) {
                    if (Test-Path -LiteralPath $interpreter) { $candidates += @{ release = $candidateRelease; python = $interpreter } }
                }
            }
        }
    }
    $candidates += @{ release = $BridgeHome; python = (Join-Path $LegacyHome "hermes-agent\venv\Scripts\python.exe") }
    # Windows PowerShell 5.1 does not reliably sort hashtable keys as properties.
    # Materialize objects before deduplication so distinct interpreter pairs survive.
    return @($candidates | ForEach-Object { [pscustomobject]$_ } | Sort-Object release, python -Unique)
}

function Get-OwnedBridgeProcess([int]$ProcessId) {
    $process = Get-CimInstance Win32_Process -Filter "ProcessId = $ProcessId" -ErrorAction Stop
    if (-not $process) { return $null }
    foreach ($candidate in (Get-BridgeProcessCandidates)) {
        foreach ($name in @("agent-bridge-mcp.py", "windows-hermes-proxy-mcp.py")) {
            if (Test-BridgeProcessOwnership $process (Join-Path $candidate.release "bin\$name") $candidate.python) { return $process }
        }
    }
    throw "PID $ProcessId does not have verified bridge executable and script ownership; leaving it running."
}

function Get-BridgeBaseExecutables([string]$VenvPython) {
    # Windows venv python.exe redirects to this explicitly configured interpreter.
    # Do not accept arbitrary python.exe processes, even with matching arguments.
    $configPath = Join-Path (Split-Path (Split-Path $VenvPython -Parent) -Parent) 'pyvenv.cfg'
    if (-not (Test-Path -LiteralPath $configPath)) { return @() }
    $executables = @()
    foreach ($line in (Get-Content -LiteralPath $configPath)) {
        if ($line -match '^\s*(executable|base-executable|home)\s*=\s*(.+?)\s*$') {
            $key = $Matches[1]; $value = $Matches[2].Trim('"')
            if (-not [IO.Path]::IsPathRooted($value)) { continue }
            if ($key -eq 'home') { $value = Join-Path $value 'python.exe' }
            $executables += [IO.Path]::GetFullPath($value)
        }
    }
    return @($executables | Select-Object -Unique)
}

function Get-OwnedBridgeTree([int]$ProcessId, [switch]$ForRestart) {
    $root = Get-OwnedBridgeProcess $ProcessId
    if (-not $root) { return $null }
    $scripts = @()
    foreach ($candidate in (Get-BridgeProcessCandidates)) {
        foreach ($name in @('agent-bridge-mcp.py', 'windows-hermes-proxy-mcp.py')) {
            $path = Join-Path $candidate.release "bin\$name"
            if (Test-BridgeProcessOwnership $root $path $candidate.python) {
                foreach ($base in (Get-BridgeBaseExecutables $candidate.python)) {
                    $scripts += @{ script = $path; python = $base }
                }
            }
        }
    }
    $servers = @($root); $consoleHosts = @()
    foreach ($child in @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $ProcessId" -ErrorAction Stop)) {
        if ($child.ParentProcessId -ne $ProcessId -or -not $child.CreationDate -or $child.CreationDate -lt $root.CreationDate) {
            throw 'Bridge child ancestry is uncertain; no process was stopped.'
        }
        # Never terminate a delegated agent or a newly adopted descendant.
        if ($ForRestart) {
            $grandchildren = @(Get-CimInstance Win32_Process -Filter "ParentProcessId = $($child.ProcessId)" -ErrorAction Stop)
            if ($grandchildren.Count) { throw 'Bridge has active descendants; checkpoint them before restarting.' }
        }
        $owned = $false
        foreach ($expected in $scripts) {
            if (Test-BridgeProcessOwnership $child $expected.script $expected.python) { $owned = $true; break }
        }
        if ($owned) { $servers += $child; continue }
        $consolePath = Join-Path $env:SystemRoot 'System32\conhost.exe'
        if ($child.ExecutablePath -and [string]::Equals([IO.Path]::GetFullPath($child.ExecutablePath), $consolePath, [StringComparison]::OrdinalIgnoreCase)) {
            # Windows owns console cleanup; this process is never a stop target.
            $consoleHosts += $child; continue
        }
        if ($ForRestart) { throw "Bridge has an unverified child PID $($child.ProcessId); no process was stopped." }
    }
    return [pscustomobject]@{ Root = $root; Servers = $servers; ConsoleHosts = $consoleHosts }
}

function Get-BridgeListeners([int]$Port) {
    try { return @(Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop) }
    catch {
        if ($_.CategoryInfo.Category -eq 'ObjectNotFound') { return @() }
        throw
    }
}

function Get-BridgeListenerProcess($Tree, [int]$Port) {
    $owners = @(Get-BridgeListeners $Port | Select-Object -ExpandProperty OwningProcess -Unique)
    if (-not $owners.Count) { return $null }
    if ($owners.Count -ne 1 -or -not $Tree -or $owners[0] -notin @($Tree.Servers.ProcessId)) {
        throw "Port $Port has a listener outside the verified bridge process tree; no process was stopped."
    }
    return $owners[0]
}

function Stop-OwnedBridgeTree($Tree, [int]$Port) {
    # Preflight the whole tree before stopping anything; then repeat immediately
    # before each stop to reject PID reuse and new/unknown descendants.
    $targets = @($Tree.Servers | Sort-Object @{ Expression = { $_.ProcessId -eq $Tree.Root.ProcessId } })
    foreach ($target in $targets) {
        $current = Get-OwnedBridgeTree $Tree.Root.ProcessId -ForRestart
        if (-not $current) {
            foreach ($remaining in $targets) {
                if (Get-CimInstance Win32_Process -Filter "ProcessId = $($remaining.ProcessId)" -ErrorAction Stop) {
                    throw 'Bridge launcher disappeared while a recorded process remains; inspect ownership before restarting.'
                }
            }
            return
        }
        foreach ($member in @($current.Servers) + @($current.ConsoleHosts)) {
            $prior = @(@($Tree.Servers) + @($Tree.ConsoleHosts) | Where-Object { $_.ProcessId -eq $member.ProcessId })
            if ($prior.Count -ne 1 -or $prior[0].CreationDate -ne $member.CreationDate -or $prior[0].ExecutablePath -ne $member.ExecutablePath -or $prior[0].CommandLine -ne $member.CommandLine) {
                throw 'Bridge process identity changed during restart; no further process was stopped.'
            }
        }
        Get-BridgeListenerProcess $current $Port | Out-Null
        $confirmed = @($current.Servers | Where-Object { $_.ProcessId -eq $target.ProcessId })
        if (-not $confirmed.Count) { continue }
        Stop-Process -Id $target.ProcessId -Force -ErrorAction Stop
        # A Windows venv redirector can outlive its child briefly after a force
        # stop.  Do not interpret that short drain period as permission to launch
        # a second listener, and do not leave an update half-applied after the
        # former listener eventually exits.  Re-read CIM on every pass so a PID
        # reuse or a changed command line still fails closed.
        $deadline = (Get-Date).AddSeconds(30)
        while ($true) {
            $remaining = @(Get-CimInstance Win32_Process -Filter "ProcessId = $($target.ProcessId)" -ErrorAction Stop)
            if (-not $remaining.Count) { break }
            if ($remaining.Count -ne 1 -or $remaining[0].CreationDate -ne $target.CreationDate -or $remaining[0].ExecutablePath -ne $target.ExecutablePath -or $remaining[0].CommandLine -ne $target.CommandLine) {
                throw 'Bridge process identity changed while waiting for shutdown; no second listener was started.'
            }
            if ((Get-Date) -ge $deadline) { throw 'Owned bridge shutdown is still pending after 30 seconds; no second listener was started.' }
            Start-Sleep -Milliseconds 250
        }
    }
}

function Test-BridgeReady([int]$Port, [int]$ProcessId, [bool]$Secure) {
    $probe = Join-Path $PSScriptRoot "agent_bridge_runtime.py"
    $probeArgs = @($probe, "--port", "$Port", "--pid", "$ProcessId", "--revision", $BuildRevision)
    if ($Secure) { $probeArgs += "--secure" }
    $result = & $PythonExe @probeArgs | ConvertFrom-Json
    return ($LASTEXITCODE -eq 0 -and $result.ready)
}

function Start-ManagedBridge([string]$Name, [int]$Port, [string]$BindAddress, [bool]$Secure = $false, [switch]$Restart) {
    New-Item -ItemType Directory -Force -Path $BridgeHome, $LogDir | Out-Null
    $launchLock = $null
    try { $launchLock = [IO.File]::Open((Join-Path $BridgeHome "launcher.lock"), 'OpenOrCreate', 'ReadWrite', 'None') }
    catch { throw "Another bridge launcher owns startup. Retry after it finishes." }
    try {
        # Verify inspection rights before creating a process we could not verify
        # or clean up. Access-denied inventories must never mean "no listener".
        Get-CimInstance Win32_Process -Filter "ProcessId = $PID" -ErrorAction Stop | Out-Null
        $pidPath = Join-Path $BridgeHome "$Name.pid"
        $oldId = 0
        if (Test-Path $pidPath) {
            if (-not [int]::TryParse((Get-Content $pidPath -Raw).Trim(), [ref]$oldId)) { throw "Invalid bridge PID record; inspect it before restarting." }
            $oldTree = Get-OwnedBridgeTree $oldId
            if ($oldTree) {
                $listenerId = Get-BridgeListenerProcess $oldTree $Port
                if ($listenerId -and (Test-BridgeReady $Port $listenerId $Secure)) { Write-Output "OK: $Name launcher=$oldId server=$listenerId ready at release $BuildRevision"; return }
                if (-not $Restart) { throw "Bridge PID $oldId has another release or is not ready. Checkpoint active tasks, then explicitly invoke this launcher with -Restart." }
                Stop-OwnedBridgeTree $oldTree $Port
            }
        }
        $listeners = @(Get-BridgeListeners $Port)
        if ($listeners.Count) { throw "Port $Port already has a listener without a verified matching PID record; no process was stopped." }
        $env:AGENT_BRIDGE_SECURE_NETWORK = if ($Secure) { "1" } else { "0" }
        $env:HERMES_BRIDGE_SECURE_NETWORK = $env:AGENT_BRIDGE_SECURE_NETWORK
        $arguments = @(('"' + $BridgeScript + '"'), "--transport", "streamable-http", "--host", $BindAddress, "--port", "$Port", "--stateless-http", "--json-response")
        if ($Secure) { $arguments += "--secure-network" }
        $process = Start-Process -FilePath $PythonExe -ArgumentList $arguments -WindowStyle Hidden -RedirectStandardOutput (Join-Path $LogDir "$Name.log") -RedirectStandardError (Join-Path $LogDir "$Name.err.log") -PassThru
        $process.Id | Set-Content $pidPath -NoNewline
        for ($attempt = 0; $attempt -lt 20; $attempt++) {
            if ($process.HasExited) { throw "$Name exited before readiness; inspect its local error log." }
            $tree = Get-OwnedBridgeTree $process.Id
            $listenerId = Get-BridgeListenerProcess $tree $Port
            if ($listenerId -and (Test-BridgeReady $Port $listenerId $Secure)) { Write-Output "OK: $Name launcher=$($process.Id) server=$listenerId ready on port $Port at release $BuildRevision"; return }
            Start-Sleep -Milliseconds 500
            $process.Refresh()
        }
        throw "$Name did not report matching process, release and readiness; PID record retained for inspection."
    } finally { $launchLock.Dispose() }
}

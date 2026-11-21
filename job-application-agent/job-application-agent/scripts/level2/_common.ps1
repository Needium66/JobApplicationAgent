# Shared helpers for the Level 2 PowerShell scripts (dot-sourced). Works in Windows PowerShell 5.1 and 7+.

# Always call the native gcloud (gcloud.cmd on Windows), never the gcloud.ps1 wrapper.
# PowerShell prefers gcloud.ps1, which inherits this script's error settings: a harmless
# internal check in it (e.g. "Test-Path : Access is denied") would otherwise abort the run.
function Find-GcloudExe {
    # 1) native gcloud.cmd / gcloud on PATH
    foreach ($name in @("gcloud.cmd", "gcloud")) {
        $c = Get-Command $name -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($c) { return $c.Source }
    }
    # 2) only the gcloud.ps1 wrapper is visible: use the gcloud.cmd that sits next to it
    $ps1 = Get-Command gcloud -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($ps1 -and $ps1.Source) {
        $sibling = Join-Path (Split-Path $ps1.Source -Parent) "gcloud.cmd"
        if (Test-Path $sibling -ErrorAction SilentlyContinue) { return $sibling }
    }
    # 3) standard Windows install folders (PATH not refreshed after an install/update)
    $roots = @($env:LOCALAPPDATA, $env:ProgramFiles, ${env:ProgramFiles(x86)}) | Where-Object { $_ }
    foreach ($r in $roots) {
        $candidate = Join-Path $r "Google\Cloud SDK\google-cloud-sdk\bin\gcloud.cmd"
        if (Test-Path $candidate -ErrorAction SilentlyContinue) { return $candidate }
    }
    # 4) last resort: the wrapper itself (helpers below neutralise its error handling)
    if ($ps1 -and $ps1.Source) { return $ps1.Source }
    return $null
}
$GcloudExe = Find-GcloudExe
if (-not $GcloudExe) {
    throw ("gcloud not found on PATH or in the standard install folders. Close VS Code completely, reopen it, " +
           "and run 'gcloud --version'. If that fails too, reinstall the Google Cloud CLI.")
}
$LastGcloudError = ""

function Invoke-Gcloud {
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 0   # a .ps1 wrapper doesn't set it; native gcloud.cmd does
    try { & $GcloudExe @args } finally { $ErrorActionPreference = $prev }
    if ($LASTEXITCODE -ne 0) { throw "gcloud $($args -join ' ') failed (exit $LASTEXITCODE)" }
}

function Test-Gcloud {
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 0
    try { & $GcloudExe @args *> $null } catch { }
    $ok = ($LASTEXITCODE -eq 0); $ErrorActionPreference = $prev
    return $ok
}

# Returns the first line of output, or "" on failure (the error text is kept in $LastGcloudError).
function Get-GcloudValue {
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    $global:LASTEXITCODE = 0
    try { $all = @(& $GcloudExe @args 2>&1) } catch { $all = @($_) }
    $ErrorActionPreference = $prev
    $errs = @($all | Where-Object { $_ -is [System.Management.Automation.ErrorRecord] })
    $outs = @($all | Where-Object { $_ -isnot [System.Management.Automation.ErrorRecord] })
    if ($LASTEXITCODE -ne 0 -or $outs.Count -eq 0) {
        $script:LastGcloudError = (($errs | ForEach-Object { $_.ToString() }) -join " ").Trim()
        return ""
    }
    return ([string]$outs[0]).Trim()
}

# True if this gcloud version supports a flag on a command (older versions only have some flags under "beta").
function Test-GcloudFlag([string]$Command, [string]$Flag) {
    $prev = $ErrorActionPreference; $ErrorActionPreference = "Continue"
    try { $help = (& $GcloudExe @($Command -split " ") --help 2>$null) -join "`n" } catch { $help = "" }
    $ErrorActionPreference = $prev
    return ($help -match ("--(\[no-\])?" + [regex]::Escape($Flag) + "\b"))
}

function Get-ProjectNumber([string]$ProjectId) {
    $n = Get-GcloudValue projects describe $ProjectId "--format=value(projectNumber)"
    if (-not $n) {
        throw ("Cannot read project '$ProjectId'. gcloud said: $LastGcloudError`n" +
               "Check: gcloud auth login; gcloud config set project $ProjectId; billing enabled; the ID (not the name) is correct.")
    }
    return $n
}

# Flags with embedded double quotes (e.g. Pub/Sub filters) go through a YAML flags file,
# because Windows PowerShell 5.1 strips embedded quotes from native-command arguments.
function New-FlagsFile([hashtable]$Flags) {
    $path = Join-Path ([System.IO.Path]::GetTempPath()) ("jp-flags-" + [guid]::NewGuid().ToString("N") + ".yaml")
    $lines = foreach ($k in $Flags.Keys) { "--{0}: '{1}'" -f $k, ($Flags[$k] -replace "'", "''") }
    Set-Content -Path $path -Value ($lines -join "`n") -Encoding ascii
    return $path
}

function New-EnvFile([System.Collections.IDictionary]$Vars) {
    $path = Join-Path ([System.IO.Path]::GetTempPath()) ("jp-env-" + [guid]::NewGuid().ToString("N") + ".yaml")
    $lines = foreach ($k in $Vars.Keys) { "{0}: '{1}'" -f $k, ([string]$Vars[$k] -replace "'", "''") }
    Set-Content -Path $path -Value ($lines -join "`n") -Encoding ascii
    return $path
}

$JpAgents = @("ingestion", "evaluation", "cover_letter", "application")

function Get-ServiceName([string]$Role) { return "jp-" + ($Role -replace "_", "-") }

function Get-SaEmail([string]$Short, [string]$ProjectId) { return "jp-$Short@$ProjectId.iam.gserviceaccount.com" }

# Service account short names (<= 30 chars incl. "jp-")
$JpServiceAccounts = [ordered]@{
    edge         = "edge"
    ingestion    = "ingestion"
    evaluation   = "evaluation"
    cover_letter = "coverletter"
    application  = "application"
    healer       = "healer"
    push         = "push"
    scheduler    = "scheduler"
}

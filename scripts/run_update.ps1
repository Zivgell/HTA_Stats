<#
.SYNOPSIS
    One full update cycle for the Hapoel Tel Aviv tracker.

.DESCRIPTION
    Fetch -> aggregate -> build -> verify -> decide next wake-up -> re-arm -> notify.

    The run ALWAYS ends by re-reading the schedule and rewriting its own trigger, so
    there is never a stale timetable. Ingestion is driven by the results feed rather
    than the fixture list, so a match that was never in the fixture list (a cup tie
    drawn and played between runs) is still picked up.

    Designed to run unattended under Task Scheduler: Python is resolved explicitly
    rather than via PATH, every step is logged, and a failure in any one step still
    lets the scheduler re-arm so the chain never dies.

.PARAMETER SkipReArm
    Run the pipeline but do not touch the scheduled task. Useful for manual runs.
#>
[CmdletBinding()]
param(
    [switch]$SkipReArm,
    [switch]$Force
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$logFile = Join-Path $root 'logs\update.log'
New-Item -ItemType Directory -Force -Path (Join-Path $root 'logs') | Out-Null

function Write-Step {
    param([string]$Message)
    $line = "{0} | RUN     | {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Message
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding utf8
}

$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# --- resolve Python explicitly -------------------------------------------------
# A Task Scheduler run has no interactive profile, which is exactly where a bare
# "python" goes missing. Config wins; PATH is only the fallback.
$config = Get-Content (Join-Path $root 'resources\config.json') -Raw -Encoding utf8 | ConvertFrom-Json
$python = $config.python_path
if (-not $python -or -not (Test-Path $python)) {
    $found = (Get-Command python -ErrorAction SilentlyContinue).Source
    if ($found) {
        $python = $found
    } else {
        Write-Step "FATAL: python not found (config python_path='$($config.python_path)', not on PATH either)"
        exit 2
    }
}

Write-Step "update cycle started (python: $python)"

$failed = $false
$stepsRun = @()

function Invoke-Step {
    param([string]$Script, [string[]]$Arguments = @(), [switch]$Optional)
    $name = Split-Path $Script -Leaf
    try {
        # Exit code is the only signal that a step failed. Anything the process writes
        # to stderr must not terminate the run: under ErrorActionPreference 'Stop',
        # PowerShell 5.1 promotes a native command's stderr writes to terminating
        # errors, which would make ordinary log output look like a failure.
        $previous = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        try { & $python $Script @Arguments } finally { $ErrorActionPreference = $previous }
        if ($LASTEXITCODE -ne 0) { throw "$name exited $LASTEXITCODE" }
        $script:stepsRun += $name
        return $true
    } catch {
        if ($Optional) {
            Write-Step "step skipped ($name): $_"
            return $true
        }
        Write-Step "STEP FAILED ($name): $_"
        $script:failed = $true
        return $false
    }
}

# 1. Ingest. A failure here still lets the scheduler re-arm below, so a transient
#    outage never breaks the chain and leaves the tracker asleep forever.
$fetchArgs = @()
if ($Force) { $fetchArgs += '--force' }
$ok = Invoke-Step -Script 'scripts\hta_fetch.py' -Arguments $fetchArgs

# 2. Aggregate and build only if there is data to work with.
if ($ok) {
    if (Invoke-Step -Script 'scripts\hta_aggregate.py') {
        Invoke-Step -Script 'scripts\hta_build.py' | Out-Null
    }
    # Advisory only: a Transfermarkt mismatch is logged, never fatal.
    Invoke-Step -Script 'scripts\hta_verify.py' -Arguments @('--quiet') -Optional | Out-Null
}

# 3. Always decide the next wake-up, even after a failure.
Invoke-Step -Script 'scripts\hta_schedule.py' -Optional | Out-Null

# --- heartbeat -----------------------------------------------------------------
# Written every run so a stale dashboard is obvious rather than merely quiet.
$statusPath = Join-Path $root 'data\fetch_status.json'
$ingested = @()
$source = 'unknown'
if (Test-Path $statusPath) {
    $status = Get-Content $statusPath -Raw -Encoding utf8 | ConvertFrom-Json
    if ($status.newly_ingested) { $ingested = @($status.newly_ingested) }
    if ($status.source) { $source = $status.source }
}

@{
    ran_at        = (Get-Date).ToString('yyyy-MM-dd HH:mm:ss')
    ok            = (-not $failed)
    source        = $source
    new_matches   = $ingested.Count
    steps         = $stepsRun
    triggered_by  = if ($env:USERNAME) { $env:USERNAME } else { 'scheduler' }
} | ConvertTo-Json | Set-Content -Path (Join-Path $root 'data\last_run.json') -Encoding utf8

# 4. Re-arm.
if (-not $SkipReArm) {
    try {
        & (Join-Path $PSScriptRoot 'hta_schedule.ps1')
    } catch {
        Write-Step "RE-ARM FAILED: $_"
    }
}

# 5. Notify only when a new match actually landed. The Hebrew text is composed in
#    hta_aggregate.py and read from JSON - this file stays pure ASCII, because
#    PowerShell 5.1 reads a BOM-less .ps1 as ANSI and would corrupt Hebrew literals.
$notifyPath = Join-Path $root 'data\notification.json'
if ($config.notify -and $ingested.Count -gt 0 -and -not $failed -and (Test-Path $notifyPath)) {
    try {
        $n = Get-Content $notifyPath -Raw -Encoding utf8 | ConvertFrom-Json
        if ($n.show) {
            & (Join-Path $PSScriptRoot 'notify.ps1') -Title $n.title -Body $n.body
            Write-Step "notified"
        }
    } catch {
        Write-Step "notification skipped: $_"
    }
}

# 6. Optionally push new match records so the cloud routine stays current.
if ($config.git_push -and $ingested.Count -gt 0 -and -not $failed) {
    try {
        $git = (Get-Command git -ErrorAction SilentlyContinue).Source
        if ($git) {
            & $git add -- 'data/matches'
            & $git -c user.name='HTA Stats' -c user.email='noreply@localhost' commit -m "data: $($ingested.Count) new match(es)" --quiet
            & $git push --quiet
            Write-Step "pushed $($ingested.Count) new match record(s)"
        } else {
            Write-Step 'git push requested but git is not installed'
        }
    } catch {
        Write-Step "git push failed: $_"
    }
}

Write-Step ("update cycle finished" + $(if ($failed) { " WITH ERRORS" } else { "" }))
if ($failed) { exit 1 }

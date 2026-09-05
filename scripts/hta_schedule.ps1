<#
.SYNOPSIS
    Register or re-arm the tracker's scheduled tasks from the computed decision.

.DESCRIPTION
    Reads data\schedule_status.json (written by hta_schedule.py) and points the
    HTA_Stats_Update task at the next wake-up time.

    Uses the ScheduledTasks module rather than schtasks.exe specifically so that
    -StartWhenAvailable can be set: a one-time trigger is otherwise SKIPPED outright if
    the PC is asleep or switched off at the time - very likely after a 20:00 kick-off.
    With it, a missed run fires at the next boot instead of vanishing.

.PARAMETER Install
    Also create the weekly guard task, which repairs the chain if the main task was
    ever deleted or failed to re-arm.

.PARAMETER WhatIf
    Print what would be scheduled without registering anything.
#>
[CmdletBinding(SupportsShouldProcess)]
param(
    [switch]$Install
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$statusPath = Join-Path $root 'data\schedule_status.json'
$runScript = Join-Path $PSScriptRoot 'run_update.ps1'

$TASK_MAIN = 'HTA_Stats_Update'
$TASK_GUARD = 'HTA_Stats_Guard'

if (-not (Test-Path $statusPath)) {
    throw "no schedule decision found at $statusPath - run: python scripts\hta_schedule.py"
}

$status = Get-Content $statusPath -Raw -Encoding utf8 | ConvertFrom-Json
$nextRun = [datetime]::Parse($status.next_run_utc).ToLocalTime()

Write-Host "state       : $($status.state)"
Write-Host "next run    : $($nextRun.ToString('yyyy-MM-dd HH:mm')) (local)"
if ($status.flag_he) {
    Write-Host "FLAG        : $($status.flag_he)  [$($status.severity)]" -ForegroundColor Yellow
}
if ($status.next_match) {
    Write-Host "next match  : $($status.next_match.home) vs $($status.next_match.away) @ $($status.next_match.kickoff_local)"
}

# A trigger in the past would never fire; nudge it just ahead of now.
if ($nextRun -le (Get-Date)) {
    $nextRun = (Get-Date).AddMinutes(2)
    Write-Host "adjusted    : trigger was in the past, moved to $($nextRun.ToString('HH:mm'))"
}

$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$runScript`"" `
    -WorkingDirectory $root

# S4U ("service for user") would run the task even when nobody is logged on, but
# registering it requires the "log on as a batch job" right, which a standard user does
# not have - it fails with Access Denied (0x80070005) and, worse, leaves the previous
# task definition silently in place. So S4U is attempted first and Interactive is the
# fallback. Interactive still runs while the workstation is LOCKED (locking does not log
# you off); only a full log-off stops it, and a PC that is off is covered by
# StartWhenAvailable running the missed job at next boot.
$principalS4U = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Limited
$principalInteractive = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType Interactive -RunLevel Limited

function Register-TaskWithFallback {
    param([string]$Name, $Trigger, $Settings, $Action, [string]$Description)
    try {
        Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger `
            -Settings $Settings -Principal $principalS4U -Description $Description `
            -Force -ErrorAction Stop | Out-Null
    } catch {
        Write-Host "note        : S4U refused for $Name ($($_.Exception.Message.Trim())); using Interactive."
        Register-ScheduledTask -TaskName $Name -Action $Action -Trigger $Trigger `
            -Settings $Settings -Principal $principalInteractive -Description $Description `
            -Force -ErrorAction Stop | Out-Null
    }
}

# StartWhenAvailable covers a PC that was off at trigger time (runs at next boot).
# WakeToRun covers a PC asleep at trigger time - this machine supports S0 Modern
# Standby and has wake timers enabled on AC.
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -WakeToRun `
    -RunOnlyIfNetworkAvailable `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
    -MultipleInstances IgnoreNew

function Assert-TaskSettings {
    <#
        A previous registration silently came back with WakeToRun = False despite it
        being requested, so the settings are read back and checked rather than trusted.
    #>
    param([string]$Name)
    $task = Get-ScheduledTask -TaskName $Name -ErrorAction Stop
    $logon = $task.Principal.LogonType
    $wake = $task.Settings.WakeToRun
    $avail = $task.Settings.StartWhenAvailable
    Write-Host "verified    : $Name LogonType=$logon WakeToRun=$wake StartWhenAvailable=$avail"
    if ($logon -ne 'S4U') {
        Write-Host "              runs while logged in or LOCKED, and wakes from sleep; a full log-off or shutdown defers it to next boot."
    }
    if (-not $wake) { Write-Warning "$Name did not accept WakeToRun - it will not wake the PC from sleep." }
    if (-not $avail) { Write-Warning "$Name did not accept StartWhenAvailable - a missed run will be skipped." }
}

if ($PSCmdlet.ShouldProcess($TASK_MAIN, "register one-time trigger at $nextRun")) {
    $trigger = New-ScheduledTaskTrigger -Once -At $nextRun
    Register-TaskWithFallback -Name $TASK_MAIN -Action $action -Trigger $trigger `
        -Settings $settings -Description 'Hapoel Tel Aviv stats: self-re-arming post-match update'
    Write-Host "registered  : $TASK_MAIN -> $($nextRun.ToString('yyyy-MM-dd HH:mm'))" -ForegroundColor Green
    Assert-TaskSettings -Name $TASK_MAIN
}

if ($Install) {
    if ($PSCmdlet.ShouldProcess($TASK_GUARD, 'register weekly guard task')) {
        $guardTrigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday -At '09:00'
        Register-TaskWithFallback -Name $TASK_GUARD -Action $action -Trigger $guardTrigger `
            -Settings $settings -Description 'Hapoel Tel Aviv stats: weekly reconciliation and chain repair'
        Write-Host "registered  : $TASK_GUARD -> Mondays 09:00" -ForegroundColor Green
        Assert-TaskSettings -Name $TASK_GUARD
    }
}

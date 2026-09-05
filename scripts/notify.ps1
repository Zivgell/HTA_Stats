<#
.SYNOPSIS
    Raise a Windows toast when the tracker ingested a new match.

.DESCRIPTION
    Uses the Windows Runtime toast API directly, so nothing has to be installed.

    Called only when data\fetch_status.json reports a non-empty newly_ingested, so a
    run that found nothing stays silent.

    A toast raised from an S4U task while nobody is logged on simply does not display.
    That is harmless by design: the notification is a convenience, never the record of
    what happened - logs\update.log and data\last_run.json are the record.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$Title,
    [Parameter(Mandatory)][string]$Body
)

$ErrorActionPreference = 'Stop'

try {
    [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
    [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom, ContentType = WindowsRuntime]

    # PowerShell is a registered AppUserModelID on Windows, so toasts show without
    # having to install a shortcut of our own.
    $appId = '{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe'

    $template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent(
        [Windows.UI.Notifications.ToastTemplateType]::ToastText02)

    $texts = $template.GetElementsByTagName('text')
    $texts.Item(0).AppendChild($template.CreateTextNode($Title)) | Out-Null
    $texts.Item(1).AppendChild($template.CreateTextNode($Body)) | Out-Null

    $toast = [Windows.UI.Notifications.ToastNotification]::new($template)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
}
catch {
    # Never let a cosmetic failure fail the update run.
    Write-Warning "toast notification unavailable: $_"
}

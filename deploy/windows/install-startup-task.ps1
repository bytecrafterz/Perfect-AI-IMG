<#
  Registers Estudio to start at boot and stay up.

  Creates one scheduled task, "Estudio-Vigilante", that runs
  estudio-arranque.ps1 at startup and then every 5 minutes. Because that
  script only starts what is not already running, the repeat is a watchdog
  rather than a restart loop.

  WHY TASK SCHEDULER AND NOT A SERVICE
  A Windows service needs a wrapper for a Python process (NSSM, WinSW) and
  this box blocks installers by group policy - winget already failed here with
  1625. Task Scheduler needs nothing, and the machine already runs
  CryptoRadar-Vigilante exactly this way, so there is one pattern to learn
  instead of two.

  WHY IT RUNS AS A NAMED ACCOUNT, NOT SYSTEM
  This is the important bit, and it is the whole reason today went wrong.

  nginx is signalled through a named event whose security descriptor belongs
  to whatever created the process. The previous nginx was started by Task
  Scheduler as a system account in session 0, so "nginx -s reload" failed with
  Access is denied even from an elevated prompt - the process was
  unmanageable by anyone. Running as Administrator with highest privileges
  keeps it reachable from an elevated prompt.

  MUST run elevated.
      .\install-startup-task.ps1
      .\install-startup-task.ps1 -Remove
#>
param(
    [string]$TaskName = 'Estudio-Vigilante',
    [string]$Script   = 'C:\estudio\deploy\windows\estudio-arranque.ps1',
    [int]   $EveryMinutes = 5,
    [switch]$Remove
)

$ErrorActionPreference = 'Stop'
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    $m" -ForegroundColor Yellow }
function Step($m) { Write-Host "`n$m" -ForegroundColor Cyan }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host 'Not elevated. Right-click PowerShell -> Run as administrator.' -ForegroundColor Red
    exit 1
}

if ($Remove) {
    Step "removing $TaskName"
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false; Ok 'removed' }
    catch { Warn "not registered: $($_.Exception.Message.Split([char]10)[0])" }
    Write-Host "`nNote: nothing will restart Estudio after a reboot now." -ForegroundColor Yellow
    exit 0
}

Step '[1] checks'
if (-not (Test-Path $Script)) {
    Write-Host "    Missing: $Script" -ForegroundColor Red
    Write-Host '    Copy the project to C:\estudio first (bootstrap-portable.ps1).' -ForegroundColor Red
    exit 1
}
Ok "script: $Script"

# Refuse to fight the radar's watchdog over the same processes.
$existing = Get-ScheduledTask -TaskName 'CryptoRadar-Vigilante' -ErrorAction SilentlyContinue
if ($existing) { Ok 'CryptoRadar-Vigilante present - untouched, it owns a different nginx' }

Step '[2] register'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -NoProfile -File `"$Script`""

# At boot, and then repeatedly. The boot trigger gets it up; the repeat is the
# watchdog. A 2-minute delay after boot lets the network stack settle - nginx
# binding :80 before the interface is ready fails, and a failed bind at boot
# means the site is simply down until someone notices.
$atStartup = New-ScheduledTaskTrigger -AtStartup
$atStartup.Delay = 'PT2M'

$repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)

# Administrator with highest privileges - NOT SYSTEM. See the header: a
# process created by SYSTEM cannot be signalled by an administrator, which is
# how the previous nginx became unmanageable.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
    -LogonType S4U -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $TaskName `
    -Action $action -Trigger @($atStartup, $repeat) `
    -Principal $principal -Settings $settings `
    -Description 'Starts and watches Estudio (uvicorn + nginx). Idempotent.' | Out-Null
Ok "registered as $TaskName, every $EveryMinutes min + at startup"

Step '[3] run it now'
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 12

$info = Get-ScheduledTaskInfo -TaskName $TaskName
Ok "last run: $($info.LastRunTime)  result: $($info.LastTaskResult)"

Step '[4] verify'
$good = $true
foreach ($port in 8000, 80, 443) {
    if (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue) { Ok "$port listening" }
    else { Warn "$port NOT listening"; $good = $false }
}
try {
    $h = Invoke-WebRequest 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 8
    Ok "/health -> HTTP $($h.StatusCode)"
} catch { Warn '/health did not answer'; $good = $false }

Write-Host ''
if ($good) {
    Write-Host '  Estudio will now come back on its own after a reboot.' -ForegroundColor Green
    Write-Host '  Check any time:  C:\estudio\deploy\windows\estudio-arranque.ps1 -Status' -ForegroundColor DarkGray
} else {
    Write-Host '  Registered, but something is not listening yet.' -ForegroundColor Yellow
    Write-Host '  Look at C:\estudio\logs\arranque-*.log' -ForegroundColor Yellow
}

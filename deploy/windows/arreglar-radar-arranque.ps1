<#
  Makes crypto-radar survive an unattended reboot.

  NOT OUR PROJECT. This touches CryptoRadar-Vigilante, which belongs to the
  Blockchain project, so it is a separate script you run deliberately rather
  than something folded into a deployment step. Its original definition is
  backed up first, and the restore command is printed at the end.

  WHAT IS WRONG

  Estudio and the shared proxy both come back on their own after a reboot:
  each has an AtStartup trigger plus a repeat, and each runs as Administrator
  with LogonType S4U, which means "run whether or not anyone is signed in".

  CryptoRadar-Vigilante has neither:

      triggers  : MSFT_TaskTimeTrigger        (no boot trigger)
      principal : Administrator, Interactive, Limited

  So after an unattended reboot - a Windows update at 03:00, a power event -
  crypto-radar stays down until somebody signs in to this machine. The other
  two sites come back and it does not, which is the kind of asymmetry that
  gets diagnosed as "the site is broken" rather than "nobody logged in".

  BOTH CHANGES ARE NEEDED, which is why this is one script and not two.
  LogonType Interactive means the task only runs while that user is signed in,
  so adding a boot trigger on its own changes nothing: it would fire at a
  point where no interactive session exists yet. And switching to S4U without
  a boot trigger only helps at the next scheduled repeat.

  THE RISK, STATED PLAINLY
  S4U runs without a loaded interactive profile. If the radar's start script
  depends on something that only exists in an interactive session - a mapped
  network drive, a user-scoped PATH entry, a credential in the user's vault -
  it will work today and fail after the reboot this is meant to fix. Node
  services usually do not, but "usually" is doing real work in that sentence,
  so the script runs the task afterwards and checks the site actually answers.

  MUST run elevated.
      .\arreglar-radar-arranque.ps1
      .\arreglar-radar-arranque.ps1 -Restore   put it back
#>
param([switch]$Restore)

$ErrorActionPreference = 'Continue'
function Step($m) { Write-Host "`n$m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "    $m" -ForegroundColor Red }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Bad 'Not elevated. Right-click PowerShell -> Run as administrator.'
    exit 1
}

$TASK   = 'CryptoRadar-Vigilante'
$HOSTN  = 'crypto-radar.duckdns.org'
$BACKUP = 'C:\estudio\deploy\windows\task-backups'

function Answers {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    try { (Invoke-WebRequest "https://$HOSTN/" -UseBasicParsing -TimeoutSec 12 -ErrorAction Stop).StatusCode }
    catch { $_.Exception.Response.StatusCode.value__ }
}

# ---------------------------------------------------------------------------
if ($Restore) {
    Step "[restore] newest backup of $TASK"
    $file = Get-ChildItem $BACKUP -Filter "$TASK-*.xml" -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $file) { Bad "no backup in $BACKUP"; exit 1 }
    Register-ScheduledTask -Xml (Get-Content $file.FullName -Raw) -TaskName $TASK -Force | Out-Null
    Ok "restored from $($file.Name)"
    Ok "$HOSTN -> $(Answers)"
    exit 0
}

# ---------------------------------------------------------------------------
Step '[1] before'
$task = Get-ScheduledTask -TaskName $TASK -ErrorAction SilentlyContinue
if (-not $task) { Bad "$TASK is not registered - nothing to fix"; exit 1 }
Ok "triggers  : $((($task.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ', '))"
Ok "principal : $($task.Principal.UserId) logon=$($task.Principal.LogonType) runlevel=$($task.Principal.RunLevel)"
$before = Answers
Ok "$HOSTN -> $before"

if ($task.Triggers | Where-Object { $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger' }) {
    Warn 'a boot trigger is already present - nothing to do'
    exit 0
}

Step '[2] back up the current definition'
New-Item -ItemType Directory -Force -Path $BACKUP | Out-Null
$path = Join-Path $BACKUP "$TASK-$(Get-Date -Format yyyyMMdd-HHmmss).xml"
[IO.File]::WriteAllText($path, (Export-ScheduledTask -TaskName $TASK), (New-Object Text.UTF8Encoding $false))
Ok "saved $path"

Step '[3] add the boot trigger and run without an interactive logon'
$boot = New-ScheduledTaskTrigger -AtStartup
# Two minutes, matching the other two watchdogs. Binding a port before the
# network stack is ready fails, and a failed bind at boot means the site is
# simply down until the next repeat.
$boot.Delay = 'PT2M'
$who = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest

try {
    Set-ScheduledTask -TaskName $TASK -Trigger (@($task.Triggers) + $boot) -Principal $who -ErrorAction Stop | Out-Null
    Ok 'updated'
} catch {
    Bad "failed: $($_.Exception.Message.Split([char]10)[0])"
    Warn "restore with: .\arreglar-radar-arranque.ps1 -Restore"
    exit 1
}

Step '[4] prove it still works under the new principal'
# The point of the change is a reboot nobody watches, so running it now is the
# only cheap way to find out whether S4U broke it.
Start-ScheduledTask -TaskName $TASK
Start-Sleep -Seconds 15
$info = Get-ScheduledTaskInfo -TaskName $TASK
Ok "last run $($info.LastRunTime)  result $($info.LastTaskResult)"

$after = Answers
if ($after -eq $before) {
    Ok "$HOSTN -> $after (unchanged)"
} else {
    Bad "$HOSTN -> $after  (was $before)"
    Warn 'The new principal may have broken it. Put it back with:'
    Warn '    .\arreglar-radar-arranque.ps1 -Restore'
    exit 1
}

$now = Get-ScheduledTask -TaskName $TASK
Step 'after'
Ok "triggers  : $((($now.Triggers | ForEach-Object { $_.CimClass.CimClassName }) -join ', '))"
Ok "principal : $($now.Principal.UserId) logon=$($now.Principal.LogonType) runlevel=$($now.Principal.RunLevel)"
Write-Host "`n  crypto-radar will now come back on its own after a reboot." -ForegroundColor Green
Write-Host "  Undo at any time:  .\arreglar-radar-arranque.ps1 -Restore" -ForegroundColor DarkGray

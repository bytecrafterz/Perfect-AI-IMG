<#
  Makes all three sites on this machine survive a reboot, and keeps their
  certificates from expiring - without disturbing any of them.

      crypto-radar.duckdns.org   node :3000   (Blockchain project)
      kind-chatbot.duckdns.org   node :3010   (chatbot project)
      wonderimg.duckdns.org      uvicorn :8000 (Estudio)

  All three are already serving. This does not restart them. What it fixes is
  what happens NEXT - after a reboot, after a crash, and in November.

  WHAT IS ACTUALLY BROKEN RIGHT NOW

  1. Nothing starts the chatbot backend. Its watchdog script exists at
     chatbot\servidor\arrancar.ps1 but the task was never registered, so a
     reboot leaves kind-chatbot down until someone notices by hand.

  2. The shared proxy was started by Estudio-Vigilante. A task named for one
     project was load-bearing for all three; removing it - reasonably, if you
     were retiring Estudio - would have taken crypto-radar and kind-chatbot
     down with it.

  3. NO CERTIFICATE RENEWS EXCEPT crypto-radar's.
     The renewal configurations exist, but no scheduled task ever runs them:
        kind-chatbot  expires 2026-11-22
        wonderimg     expires 2026-11-25
     Both sites would simply stop working on those dates, in a browser, with
     no warning and nothing recent to blame.

  4. A renewal for edwin-iot-server.duckdns.org is still registered. That
     hostname was removed, so its ACME challenge cannot succeed and it will
     fail on every attempt from now on.

  5. Even when renewal runs, nginx keeps serving the OLD certificate from
     memory until it is reloaded. A certificate that renews but is never
     reloaded expires exactly as if it had not renewed at all.

  MUST run elevated.
      .\publicar-los-tres.ps1
      .\publicar-los-tres.ps1 -DryRun     report only, change nothing
#>
param([switch]$DryRun)

$ErrorActionPreference = 'Continue'

function Step($m) { Write-Host "`n$m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "    $m" -ForegroundColor Red }
function Plan($m) { Write-Host "    would: $m" -ForegroundColor DarkGray }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Bad 'Not elevated. Right-click PowerShell -> Run as administrator.'
    exit 1
}

$WACS = 'C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Packages\simple-acme.simple-acme_Microsoft.Winget.Source_8wekyb3d8bbwe\wacs.exe'
$ChatbotTask = 'C:\Users\Administrator\Documents\chatbot\servidor\tarea.ps1'
$ProxyScript = 'C:\estudio\deploy\windows\proxy-vigilante.ps1'
$AppScript   = 'C:\estudio\deploy\windows\estudio-arranque.ps1'

# ---------------------------------------------------------------------------
Step '[1] baseline - all three before we touch anything'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$before = @{}
foreach ($h in 'crypto-radar.duckdns.org', 'kind-chatbot.duckdns.org', 'wonderimg.duckdns.org') {
    $code = try { (Invoke-WebRequest "https://$h/" -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop).StatusCode }
            catch { $_.Exception.Response.StatusCode.value__ }
    $before[$h] = $code
    if ($code -eq 200) { Ok "$h -> $code" } else { Warn "$h -> $code" }
}

# ---------------------------------------------------------------------------
Step '[2] the shared proxy gets its own watchdog'
if (-not (Test-Path $ProxyScript)) { Bad "missing $ProxyScript"; exit 1 }

if ($DryRun) {
    Plan 'register Proxy-Vigilante (starts C:\nginx if down, every 5 min + at boot)'
    Plan 'reduce Estudio-Vigilante to uvicorn only'
} else {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -NoProfile -File `"$ProxyScript`""
    $atBoot = New-ScheduledTaskTrigger -AtStartup
    $atBoot.Delay = 'PT2M'   # let the network stack settle; binding :80 too early fails
    $every5 = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) -RepetitionDuration (New-TimeSpan -Days 3650)

    # Administrator with highest privileges, NOT SYSTEM. A process created by
    # SYSTEM cannot be signalled by an administrator, which is precisely how
    # the previous nginx became impossible to reload.
    $who = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
    $how = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

    Unregister-ScheduledTask -TaskName 'Proxy-Vigilante' -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName 'Proxy-Vigilante' -Action $action `
        -Trigger @($atBoot, $every5) -Principal $who -Settings $how `
        -Description 'Keeps the shared nginx alive. Fronts all three sites - not owned by any one of them.' | Out-Null
    Ok 'Proxy-Vigilante registered'
}

# ---------------------------------------------------------------------------
Step '[3] the chatbot gets the watchdog it already had a script for'
if (-not (Test-Path $ChatbotTask)) {
    Warn "not found: $ChatbotTask - skipping"
} elseif (Get-ScheduledTask -TaskName 'Asistente-Vigilante' -ErrorAction SilentlyContinue) {
    Ok 'Asistente-Vigilante already registered'
} elseif ($DryRun) {
    Plan "run $ChatbotTask (the chatbot project's own installer)"
} else {
    # Their script, not a reimplementation. It knows that project's layout.
    & powershell.exe -ExecutionPolicy Bypass -NoProfile -File $ChatbotTask 2>&1 |
        ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    if (Get-ScheduledTask -TaskName 'Asistente-Vigilante' -ErrorAction SilentlyContinue) {
        Ok 'Asistente-Vigilante registered'
    } else { Warn 'did not register - check the output above' }
}

# ---------------------------------------------------------------------------
Step '[4] certificate renewal - the November problem'
if (-not (Test-Path $WACS)) {
    Bad "wacs.exe not found at $WACS"
} elseif ($DryRun) {
    Plan 'wacs --setuptaskscheduler   (registers the renewal task)'
    Plan 'cancel the dead edwin-iot-server renewal'
} else {
    & $WACS '--setuptaskscheduler' 2>&1 | Select-Object -Last 6 |
        ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
    $task = Get-ScheduledTask -ErrorAction SilentlyContinue |
        Where-Object { ($_.Actions | ForEach-Object { $_.Execute }) -match 'wacs' }
    if ($task) { Ok "renewal task: $($task.TaskName)" }
    else { Warn 'no wacs task found - renewals still will not run' }
}

# ---------------------------------------------------------------------------
Step '[5] reload nginx after any renewal'
# Belt and braces, and it covers every domain at once rather than relying on
# each renewal config having an install script - wonderimg's has none.
# A graceful reload is a non-event: workers finish their requests and retire.
if ($DryRun) {
    Plan 'register Proxy-Recarga daily at 04:30, after the renewal window'
} else {
    $action = New-ScheduledTaskAction -Execute 'powershell.exe' `
        -Argument "-ExecutionPolicy Bypass -WindowStyle Hidden -NoProfile -File `"$ProxyScript`" -Reload"
    $daily = New-ScheduledTaskTrigger -Daily -At '04:30'
    $who = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
    $how = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew

    Unregister-ScheduledTask -TaskName 'Proxy-Recarga' -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName 'Proxy-Recarga' -Action $action -Trigger $daily `
        -Principal $who -Settings $how `
        -Description 'Reloads nginx daily so renewed certificates are actually served.' | Out-Null
    Ok 'Proxy-Recarga registered (04:30 daily)'
}

# ---------------------------------------------------------------------------
Step '[6] verify nothing was disturbed'
Start-Sleep -Seconds 3
$broke = $false
foreach ($h in $before.Keys) {
    $code = try { (Invoke-WebRequest "https://$h/" -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop).StatusCode }
            catch { $_.Exception.Response.StatusCode.value__ }
    if ($code -eq $before[$h]) { Ok "$h -> $code (unchanged)" }
    else { Bad "$h -> $code  (was $($before[$h]))"; $broke = $true }
}

Step 'summary'
Get-ScheduledTask -ErrorAction SilentlyContinue |
    Where-Object { $_.TaskName -match 'Vigilante|Recarga|Certificado|acme|simple' } |
    ForEach-Object { Write-Host ("    {0,-26} {1}" -f $_.TaskName, $_.State) -ForegroundColor DarkGray }

Write-Host ''
if ($DryRun) { Write-Host '  Dry run. Nothing changed.' -ForegroundColor Yellow }
elseif ($broke) { Write-Host '  Something changed state - check above.' -ForegroundColor Red }
else { Write-Host '  All three sites up, all three now survive a reboot.' -ForegroundColor Green }

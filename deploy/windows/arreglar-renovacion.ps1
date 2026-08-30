<#
  Fixes certificate renewal properly. Three separate problems.

  1. A STAGING RENEWAL FOR wonderimg.duckdns.org STILL EXISTS.
     It was created by running get-cert with -Staging, which was the right
     way to test the ACME path. What it left behind is dangerous: staging and
     production both store to C:\nginx\conf\ssl, and simple-acme names its
     output after the domain. So both write the SAME filenames. If the staging
     renewal ever runs, it replaces a trusted certificate with an untrusted
     one and every browser refuses the site - a failure that looks like a
     hacked server, not an expired certificate.

  2. A RENEWAL FOR edwin-iot-server.duckdns.org STILL EXISTS.
     That hostname and its ACME challenge path were removed, so the renewal
     cannot validate and will fail on every attempt from here on.

  3. NO SCHEDULED TASK RUNS RENEWALS AT ALL.
     --setuptaskscheduler reported what it would create but nothing was
     registered. Without it, kind-chatbot expires 2026-11-22 and wonderimg
     2026-11-25, silently.

  MUST run elevated.
#>
$ErrorActionPreference = 'Continue'

function Step($m) { Write-Host "`n$m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Warn($m) { Write-Host "    $m" -ForegroundColor Yellow }
function Bad($m)  { Write-Host "    $m" -ForegroundColor Red }

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
        ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Bad 'Not elevated.'; exit 1
}

$WACS    = 'C:\Users\Administrator\AppData\Local\Microsoft\WinGet\Packages\simple-acme.simple-acme_Microsoft.Winget.Source_8wekyb3d8bbwe\wacs.exe'
$Prod    = 'C:\ProgramData\simple-acme\acme-v02.api.letsencrypt.org'
$Staging = 'C:\ProgramData\simple-acme\acme-staging-v02.api.letsencrypt.org'
$Backup  = "C:\ProgramData\simple-acme\retirados-$(Get-Date -Format yyyyMMdd-HHmmss)"

# ---------------------------------------------------------------------------
Step '[1] retire the staging renewal for wonderimg'
# Moved, not deleted. If this turns out to have been load-bearing for
# something nobody remembered, it is one copy away from coming back.
New-Item -ItemType Directory -Force -Path $Backup | Out-Null
$moved = 0
Get-ChildItem $Staging -Filter *.renewal.json -ErrorAction SilentlyContinue | ForEach-Object {
    $cn = [regex]::Match((Get-Content $_.FullName -Raw), '"CommonName":\s*"([^"]+)"').Groups[1].Value
    Warn "retiring staging renewal for $cn"
    Move-Item $_.FullName (Join-Path $Backup "staging-$($_.Name)") -Force
    $moved++
}
if ($moved) { Ok "$moved moved to $Backup" } else { Ok 'none present' }

# ---------------------------------------------------------------------------
Step '[2] retire the renewal for the removed edwin-iot host'
$retired = 0
Get-ChildItem $Prod -Filter *.renewal.json -ErrorAction SilentlyContinue | ForEach-Object {
    $cn = [regex]::Match((Get-Content $_.FullName -Raw), '"CommonName":\s*"([^"]+)"').Groups[1].Value
    if ($cn -eq 'edwin-iot-server.duckdns.org') {
        Warn "retiring $cn (host removed, cannot validate)"
        Move-Item $_.FullName (Join-Path $Backup $_.Name) -Force
        $retired++
    }
}
if ($retired) { Ok "$retired retired" } else { Ok 'none present' }

Step '    renewals that remain'
Get-ChildItem $Prod -Filter *.renewal.json -ErrorAction SilentlyContinue | ForEach-Object {
    $cn = [regex]::Match((Get-Content $_.FullName -Raw), '"CommonName":\s*"([^"]+)"').Groups[1].Value
    Ok "  $cn"
}

# ---------------------------------------------------------------------------
Step '[3] register the renewal task, and verify it exists'
if (-not (Test-Path $WACS)) { Bad "wacs.exe not found"; exit 1 }

& $WACS '--setuptaskscheduler' '--baseuri' 'https://acme-v02.api.letsencrypt.org/' 2>&1 |
    Select-Object -Last 4 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }

# Verify rather than trust. Last time this printed what it would create and
# then registered nothing, which is exactly the sort of thing that is only
# discovered when a certificate quietly expires.
Start-Sleep -Seconds 2
$task = Get-ScheduledTask -ErrorAction SilentlyContinue |
    Where-Object { $_.TaskName -like '*simple-acme*' -or ($_.Actions | ForEach-Object { $_.Execute }) -match 'wacs' }

if ($task) {
    foreach ($t in $task) { Ok "$($t.TaskName) [$($t.State)]" }
} else {
    Warn 'wacs did not register a task. Falling back to our own.'
    # A plain daily task calling --renew does the same job and is easier to
    # inspect than whatever wacs would have created.
    $action = New-ScheduledTaskAction -Execute $WACS `
        -Argument '--renew --baseuri "https://acme-v02.api.letsencrypt.org/"'
    $daily  = New-ScheduledTaskTrigger -Daily -At '04:05'
    $who    = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType S4U -RunLevel Highest
    $how    = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2)
    Unregister-ScheduledTask -TaskName 'Certificados-Renovar' -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName 'Certificados-Renovar' -Action $action -Trigger $daily `
        -Principal $who -Settings $how `
        -Description 'Renews every simple-acme certificate. Proxy-Recarga reloads nginx at 04:30.' | Out-Null
    Ok 'Certificados-Renovar registered (04:05 daily)'
}

# ---------------------------------------------------------------------------
Step '[4] dry-run a renewal to prove it works before November'
# --force would re-issue and spend rate limit. This checks the plan only.
& $WACS '--renew' '--baseuri' 'https://acme-v02.api.letsencrypt.org/' 2>&1 |
    Select-Object -Last 12 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }

# ---------------------------------------------------------------------------
Step '[5] certificates now'
function Read-Pem($p) {
    if (-not (Test-Path $p)) { return $null }
    $m = [regex]::Match((Get-Content $p -Raw), '-----BEGIN CERTIFICATE-----(.+?)-----END CERTIFICATE-----', 'Singleline')
    if (-not $m.Success) { return $null }
    try { New-Object Security.Cryptography.X509Certificates.X509Certificate2 (, [Convert]::FromBase64String(($m.Groups[1].Value -replace '\s', ''))) } catch { $null }
}
foreach ($c in @(
    @{ n = 'crypto-radar'; p = 'C:\Users\Administrator\Documents\Blockchain\nginx\ssl\crypto-radar-fullchain.pem' },
    @{ n = 'kind-chatbot'; p = 'C:\nginx\conf\ssl\asistente\fullchain.pem' },
    @{ n = 'wonderimg';    p = 'C:\nginx\conf\ssl\wonderimg.duckdns.org-chain.pem' })) {
    $x = Read-Pem $c.p
    if ($x) {
        $staging = $x.Issuer -match 'STAGING|Fake'
        $line = "{0,-14} expires {1:yyyy-MM-dd}  issuer {2}" -f $c.n, $x.NotAfter, ($x.Issuer -replace '.*O=([^,]+).*', '$1')
        if ($staging) { Bad "$line   <-- STAGING, browsers will not trust this" } else { Ok $line }
    } else { Warn "{0,-14} not found" -f $c.n }
}

Step '[6] all three sites'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
foreach ($h in 'crypto-radar.duckdns.org', 'kind-chatbot.duckdns.org', 'wonderimg.duckdns.org') {
    $code = try { (Invoke-WebRequest "https://$h/" -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop).StatusCode }
            catch { $_.Exception.Response.StatusCode.value__ }
    if ($code -eq 200) { Ok "$h -> $code" } else { Bad "$h -> $code" }
}

Write-Host "`n  Retired configs kept at $Backup" -ForegroundColor DarkGray

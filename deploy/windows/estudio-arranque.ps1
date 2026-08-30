<#
  Brings Estudio up, and keeps it up.

  Starts only what is not already running, so it is safe to run every few
  minutes as a watchdog as well as once at boot. Same shape as this box's
  existing CryptoRadar-Vigilante, deliberately - one pattern to understand
  rather than two.

  ONE piece: uvicorn on 127.0.0.1:8000.
  The shared nginx on 80/443 is NOT started here - it fronts all three sites
  on this box and belongs to Proxy-Vigilante. See proxy-vigilante.ps1.

  WHAT IT WILL NOT TOUCH
  The other nginx at C:\tools\nginx-1.28.0, which serves the radar's backend
  on 127.0.0.1:8080 and belongs to CryptoRadar-Vigilante. Two watchdogs
  fighting over one process is worse than no watchdog.

  WHY THIS EXISTS
  nginx on this box was started by hand once and never again. It survived
  nothing, it was owned by a session nobody could reach, and reloading it
  became impossible - which cost an hour to unpick. The point here is not
  only that it restarts, but that it restarts OWNED BY A KNOWN ACCOUNT, so
  the next person can manage it.

  Manual use:
      powershell -ExecutionPolicy Bypass -File estudio-arranque.ps1
      ... -Restart      stop uvicorn first, then start (picks up code changes)
      ... -Status       report and change nothing
#>
param(
    [switch]$Restart,
    [switch]$Status
)

$ErrorActionPreference = 'Continue'

$Root      = 'C:\estudio'
$Python    = Join-Path $Root 'python\python.exe'
$NginxExe  = 'C:\nginx\nginx.exe'
$NginxPfx  = 'C:/nginx/'
$NginxConf = 'C:/nginx/conf/nginx.conf'
$KeepPfx   = 'C:\tools\'          # the radar's nginx - never ours to touch
$LogDir    = Join-Path $Root 'logs'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Journal = Join-Path $LogDir ('arranque-' + (Get-Date -Format 'yyyy-MM-dd') + '.log')

function Note($text, $colour = 'Gray') {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $text
    Write-Host $line -ForegroundColor $colour
    Add-Content -Path $Journal -Value $line -ErrorAction SilentlyContinue
}

function PortBusy($port) {
    $null -ne (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# nginx writes everything to stderr, success included. In PowerShell 5.1 a
# 2>&1 redirect turns those lines into ErrorRecords, so judge by the text.
function NginxSays([string[]]$arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { return (& $NginxExe @arguments 2>&1 | Out-String) }
    finally { $ErrorActionPreference = $previous }
}

function OurNginx {
    @(Get-CimInstance Win32_Process -Filter "Name='nginx.exe'" |
        Where-Object { -not ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($KeepPfx, 'OrdinalIgnoreCase')) })
}

function OurUvicorn {
    # Command line first - it is the precise answer when it is readable.
    $byCommand = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -and $_.CommandLine -match 'uvicorn' -and $_.CommandLine -match 'app.main' })
    if ($byCommand.Count -gt 0) { return $byCommand }

    # Win32_Process returns a NULL CommandLine for processes this session may
    # not inspect, and an unelevated shell frequently cannot read its own.
    # The match then silently finds nothing, so -Restart reported "uvicorn ya
    # estaba en marcha" and did nothing at all - the app kept serving stale
    # code through every deploy, which is a very quiet way to lose an
    # afternoon.
    #
    # Owning the configured port IS the identity here, so fall back to that,
    # narrowed to our own interpreter so a stray listener is never killed.
    $owner = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1 -ExpandProperty OwningProcess
    if (-not $owner) { return @() }
    @(Get-CimInstance Win32_Process -Filter "ProcessId=$owner" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.Name -eq 'python.exe' -and
            (-not $_.ExecutablePath -or $_.ExecutablePath.StartsWith($Root, 'OrdinalIgnoreCase'))
        })
}

# ---------------------------------------------------------------------------
if ($Status) {
    Note '=== Estudio ===' 'Cyan'
    Note ("  uvicorn :8000 : " + $(if (PortBusy 8000) { 'up' } else { 'DOWN' }))
    Note ("  nginx   :80   : " + $(if (PortBusy 80)   { 'up' } else { 'DOWN' }))
    Note ("  nginx   :443  : " + $(if (PortBusy 443)  { 'up' } else { 'DOWN' }))
    foreach ($p in OurNginx)   { Note "  nginx pid $($p.ProcessId)" 'DarkGray' }
    foreach ($p in OurUvicorn) { Note "  uvicorn pid $($p.ProcessId)" 'DarkGray' }
    try {
        $h = Invoke-WebRequest 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 5
        Note "  /health       : HTTP $($h.StatusCode)" 'Green'
    } catch { Note '  /health       : no responde' 'Yellow' }
    exit 0
}

Note '=== Arranque de Estudio ===' 'Cyan'

# ---------------------------------------------------------------------------
if ($Restart) {
    Note 'Parando (--Restart)' 'Yellow'
    # uvicorn only. nginx belongs to Proxy-Vigilante; stopping it here would take
    # crypto-radar and kind-chatbot down and nothing here would bring them back.
    foreach ($p in OurUvicorn) {
        try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Note "  parado $($p.ProcessId)" }
        catch { Note "  no se pudo parar $($p.ProcessId): $($_.Exception.Message.Split([char]10)[0])" 'Red' }
    }
    # Let the listeners actually release. Binding while :80 is still held does
    # not fail cleanly on Windows - the new master takes whichever ports it
    # can and leaves the rest, and two nginx instances on different configs is
    # far harder to diagnose than one that never started.
    $deadline = (Get-Date).AddSeconds(15)
    while ((PortBusy 80) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
}

# --- 1. the application ----------------------------------------------------
if (PortBusy 8000) {
    Note 'uvicorn ya estaba en marcha' 'Green'
} elseif (-not (Test-Path $Python)) {
    Note "No encuentro $Python" 'Red'
} else {
    Note 'Arrancando uvicorn...' 'Yellow'
    Start-Process -FilePath $Python `
        -ArgumentList @(
            '-m','uvicorn','app.main:app',
            '--host','127.0.0.1','--port','8000',
            '--proxy-headers','--forwarded-allow-ips','127.0.0.1'
        ) `
        -WorkingDirectory $Root -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $LogDir 'uvicorn.out.log') `
        -RedirectStandardError  (Join-Path $LogDir 'uvicorn.err.log')

    $tries = 0
    while (-not (PortBusy 8000) -and $tries -lt 30) { Start-Sleep -Seconds 1; $tries++ }
    if (PortBusy 8000) { Note 'uvicorn listo en :8000' 'Green' }
    else { Note "uvicorn no responde. Mira $LogDir\uvicorn.err.log" 'Red' }
}

# --- 2. the proxy ----------------------------------------------------------
# NOT ours to start. The shared nginx belongs to Proxy-Vigilante, which runs
# proxy-vigilante.ps1 on the same 5-minute cadence.
#
# This script used to start it too. Both check "is :80 up?" first, so it looked
# harmless - but two watchdogs on the same schedule can both look at a moment
# when nginx is down and both start one. That is how this box ended up with
# two nginx masters on the same config, one holding :80 and the other :443,
# each invisible to the other. It took an hour to unpick, and the symptom was
# a site that answered on http and refused on https for no apparent reason.
#
# One owner, one starter. We only report on it here.
if (PortBusy 80) {
    Note 'nginx en marcha (lo gestiona Proxy-Vigilante)' 'Green'
} else {
    Note 'nginx PARADO - lo arranca Proxy-Vigilante, no este script' 'Yellow'
    if (-not (Get-ScheduledTask -TaskName 'Proxy-Vigilante' -ErrorAction SilentlyContinue)) {
        # Without that task nobody starts the proxy and all three sites stay
        # down, so say so plainly rather than waiting silently.
        Note 'Proxy-Vigilante NO esta registrado. Ejecuta publicar-los-tres.ps1' 'Red'
    }
}

# --- resumen ---------------------------------------------------------------
Note ''
Note '--- Estado ---' 'Cyan'
Note ("  uvicorn :8000 : " + $(if (PortBusy 8000) { 'en marcha' } else { 'PARADO' }))
Note ("  nginx   :80   : " + $(if (PortBusy 80)   { 'en marcha' } else { 'PARADO' }))
Note ("  nginx   :443  : " + $(if (PortBusy 443)  { 'en marcha' } else { 'PARADO' }))

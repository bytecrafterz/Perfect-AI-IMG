<#
  Keeps the SHARED public proxy alive.

  C:\nginx is not Estudio's. It fronts every site on this machine:

      crypto-radar.duckdns.org   -> 127.0.0.1:3000
      kind-chatbot.duckdns.org   -> 127.0.0.1:3010
      wonderimg.duckdns.org      -> 127.0.0.1:8000
      MQTT                       -> 127.0.0.1:11883

  It lives in its own watchdog for exactly that reason. It used to be started
  by Estudio-Vigilante, which meant a task named for one project was
  load-bearing for three - and anyone removing it, reasonably believing it
  belonged to Estudio, would have taken all of them down.

  Each project keeps its own watchdog for its own backend. This one owns the
  proxy and nothing else.

  DOES NOT TOUCH the second nginx at C:\tools\nginx-1.28.0, which serves the
  radar's local backend on :8080 and belongs to CryptoRadar-Vigilante.

      .\proxy-vigilante.ps1            start if down
      .\proxy-vigilante.ps1 -Reload    pick up config changes, no downtime
      .\proxy-vigilante.ps1 -Status    report only
#>
param(
    [switch]$Reload,
    [switch]$Status
)

$ErrorActionPreference = 'Continue'

$NginxExe = 'C:\nginx\nginx.exe'
$Prefix   = 'C:/nginx/'
$Conf     = 'C:/nginx/conf/nginx.conf'
$KeepPfx  = 'C:\tools\'
$LogDir   = 'C:\nginx\logs'

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
$Journal = Join-Path $LogDir ('proxy-' + (Get-Date -Format 'yyyy-MM-dd') + '.log')

function Note($text, $colour = 'Gray') {
    $line = "[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $text
    Write-Host $line -ForegroundColor $colour
    Add-Content -Path $Journal -Value $line -ErrorAction SilentlyContinue
}

# nginx writes everything to stderr, success included. In PowerShell 5.1 a
# 2>&1 redirect wraps those lines as ErrorRecords, so judge by the text.
function NginxSays([string[]]$arguments) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { return (& $NginxExe @arguments 2>&1 | Out-String) }
    finally { $ErrorActionPreference = $previous }
}

function Listening($port) {
    $null -ne (Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

if ($Status) {
    Note '=== proxy publico ===' 'Cyan'
    foreach ($p in 80, 443, 1883) { Note ("  :{0,-5} {1}" -f $p, $(if (Listening $p) { 'up' } else { 'DOWN' })) }
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    foreach ($h in 'crypto-radar.duckdns.org', 'kind-chatbot.duckdns.org', 'wonderimg.duckdns.org') {
        $code = try { (Invoke-WebRequest "https://$h/" -UseBasicParsing -TimeoutSec 10 -ErrorAction Stop).StatusCode }
                catch { $_.Exception.Response.StatusCode.value__ }
        Note ("  {0,-28} https -> {1}" -f $h, $(if ($code) { $code } else { 'sin respuesta' }))
    }
    exit 0
}

if ($Reload) {
    # Validate first. A rejected config on reload leaves the old workers
    # running, which is safe - but reporting "reloaded" when it was refused
    # is how a config change silently fails to take effect.
    $test = NginxSays @('-t', '-p', $Prefix, '-c', $Conf)
    if ($test -notmatch 'test is successful') {
        Note 'Configuracion invalida. NO se recarga.' 'Red'
        foreach ($l in ($test -split "`n" | Where-Object { $_ -match 'emerg' })) { Note "  $($l.Trim())" 'Red' }
        exit 1
    }
    $result = NginxSays @('-s', 'reload', '-p', $Prefix, '-c', $Conf)
    if ($result -match 'denied') { Note "Recarga denegada: $($result.Trim())" 'Red'; exit 1 }
    Note 'Recargado' 'Green'
    exit 0
}

# --- start if down ---------------------------------------------------------
if (Listening 80) { exit 0 }   # silent no-op: this runs every few minutes

Note 'nginx caido. Arrancando...' 'Yellow'

# Validate BEFORE starting. Three sites go through this process; a config it
# refuses means it exits and every one of them stays down.
$test = NginxSays @('-t', '-p', $Prefix, '-c', $Conf)
if ($test -notmatch 'test is successful') {
    Note 'Configuracion invalida. NO se arranca.' 'Red'
    foreach ($l in ($test -split "`n" | Where-Object { $_ -match 'emerg' })) { Note "  $($l.Trim())" 'Red' }
    exit 1
}

Start-Process -FilePath $NginxExe -ArgumentList '-p', $Prefix, '-c', $Conf `
    -WorkingDirectory 'C:\nginx' -WindowStyle Hidden
Start-Sleep -Seconds 3

if (Listening 80) {
    Note 'nginx en marcha (:80, :443, :1883)' 'Green'
} else {
    Note 'nginx no escucha. Mira C:\nginx\logs\error.log' 'Red'
}

#Requires -Version 5.1
#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Installs Estudio (uvicorn) as a Windows service that starts at boot with
    nobody logged in, and optionally does the same for nginx.

.DESCRIPTION
    Run once, elevated, on the VPS:

        powershell -ExecutionPolicy Bypass -File C:\estudio\deploy\windows\install-service.ps1

    With nginx as well:

        ... -NginxDir C:\nginx

    To remove the services (data, .env and certificates are left alone):

        ... -Uninstall

    Notes that matter, because getting them wrong is silent and expensive:

    * A console program such as python.exe cannot be registered with sc.exe or
      New-Service directly - Windows kills it with error 1053 because it never
      answers the service control manager. NSSM is the wrapper that turns it
      into a real service. nssm.exe must already be on the box; this script
      refuses to guess.

    * The application reads its configuration from os.environ only. It does NOT
      parse C:\estudio\.env by itself (in Docker that job belongs to
      docker-compose env_file). So this script reads .env and injects it into
      the service environment. Without that, SECRET_KEY is empty, every restart
      invalidates her session cookie, and ACCESS_TOKEN is empty, which means
      anyone who knows the address is let straight in.

    * .env is read, never written. Rewriting it rotates SECRET_KEY, which logs
      her out, and can point DATA_DIR somewhere new, which orphans her photos.

    * Exactly one worker process. /events/{session_id} is served from an
      in-process event bus and /previews and /finals finish their work in
      background tasks, so a second worker would answer the browser from a
      process that knows nothing about the running batch.
#>

[CmdletBinding()]
param(
    [string] $ProjectRoot = 'C:\estudio',
    [string] $ServiceName = 'estudio',
    [string] $NssmPath    = '',
    [string] $NginxDir    = '',
    [int]    $Port        = 8000,
    [switch] $Uninstall
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Write-Step { param([string]$Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Write-Note { param([string]$Message) Write-Host "    $Message" -ForegroundColor DarkGray }
function Write-Warn { param([string]$Message) Write-Host "!!  $Message" -ForegroundColor Yellow }

function Invoke-Nssm {
    # Runs nssm and fails loudly. nssm writes its diagnostics on stderr and
    # returns non-zero, both of which would otherwise be swallowed.
    param([Parameter(ValueFromRemainingArguments = $true)][string[]] $NssmArgs)
    $output = & $script:Nssm @NssmArgs 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "nssm $($NssmArgs -join ' ') failed (exit $LASTEXITCODE): $output"
    }
}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

$ProjectRoot     = $ProjectRoot.TrimEnd('\')
$VenvPython      = Join-Path $ProjectRoot '.venv\Scripts\python.exe'
$EnvFile         = Join-Path $ProjectRoot '.env'
$DataDir         = Join-Path $ProjectRoot 'data'
$LogDir          = Join-Path $DataDir 'logs'
$AppLog          = Join-Path $LogDir 'estudio.log'
$NginxService    = 'nginx'
$NginxReloadTask = 'nginx-reload-for-renewed-cert'

# ---------------------------------------------------------------------------
# Locate nssm.exe
# ---------------------------------------------------------------------------

function Resolve-Nssm {
    param([string] $Explicit)

    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    $candidates += (Join-Path $PSScriptRoot 'nssm.exe')
    $candidates += 'C:\nssm\nssm.exe'
    foreach ($c in $candidates) {
        if ($c -and (Test-Path -LiteralPath $c -PathType Leaf)) {
            return (Resolve-Path -LiteralPath $c).Path
        }
    }
    $onPath = Get-Command 'nssm.exe' -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }

    throw @"
nssm.exe not found.

A Windows service must answer the service control manager; python.exe does not,
so it cannot be registered with sc.exe or New-Service (it fails at start with
error 1053). NSSM is the standard wrapper.

Install it once, by hand, then re-run this script:

  1. Download https://nssm.cc/release/nssm-2.24.zip on any machine.
  2. Copy win64\nssm.exe to C:\nssm\nssm.exe on this server
     (or next to this script, or pass -NssmPath).

Checked: $($candidates -join ', '), and PATH.
"@
}

$script:Nssm = Resolve-Nssm -Explicit $NssmPath

# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------

function Remove-EstudioService {
    param([string] $Name)
    $existing = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if (-not $existing) {
        Write-Note "$Name is not installed."
        return
    }
    Write-Step "Removing service $Name"
    & $script:Nssm stop $Name 2>&1 | Out-Null
    & $script:Nssm remove $Name confirm 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "nssm remove $Name failed (exit $LASTEXITCODE)." }
    Write-Note "$Name removed."
}

if ($Uninstall) {
    Remove-EstudioService -Name $ServiceName
    Remove-EstudioService -Name $NginxService
    if (Get-ScheduledTask -TaskName $NginxReloadTask -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $NginxReloadTask -Confirm:$false
        Write-Note "Scheduled task $NginxReloadTask removed."
    }
    Write-Host ''
    Write-Host "Done. $EnvFile, $DataDir and any certificates were left untouched." -ForegroundColor Green
    return
}

# ---------------------------------------------------------------------------
# Preflight - refuse to install something that will fail quietly later
# ---------------------------------------------------------------------------

Write-Step 'Checking prerequisites'

if (-not (Test-Path -LiteralPath $ProjectRoot -PathType Container)) {
    throw "Project root not found: $ProjectRoot"
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'app\main.py') -PathType Leaf)) {
    throw "$ProjectRoot does not look like the Estudio checkout (app\main.py is missing)."
}
if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    throw @"
Virtualenv interpreter not found: $VenvPython

Create it first, from $ProjectRoot :

  py -3.11 -m venv .venv
  .venv\Scripts\python.exe -m pip install --upgrade pip
  .venv\Scripts\python.exe -m pip install -r requirements.txt
"@
}

# The interpreter must actually be able to start the server. Doing this now
# turns "the service flaps forever after every reboot" into an error on the
# console while a human is still watching.
$uvicornVersion = & $VenvPython -m uvicorn --version 2>&1
if ($LASTEXITCODE -ne 0) {
    throw "uvicorn is not installed in $VenvPython.`n$uvicornVersion"
}
Write-Note "Interpreter: $VenvPython"
Write-Note "$uvicornVersion"

$pyVersion = & $VenvPython -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1
if ($LASTEXITCODE -ne 0) { throw "Could not query the interpreter version: $pyVersion" }
if ("$pyVersion".Trim() -ne '3.11') {
    Write-Warn "Interpreter is Python $("$pyVersion".Trim()); the app is built and tested on 3.11."
}

if (-not (Test-Path -LiteralPath $EnvFile -PathType Leaf)) {
    throw @"
$EnvFile is missing.

This script never creates or edits it: generating a fresh SECRET_KEY would
invalidate the session cookie on her phone, and a fresh DATA_DIR would orphan
every photo already in $DataDir.

Copy .env.example to .env, fill in SECRET_KEY and ACCESS_TOKEN
(python -c "import secrets; print(secrets.token_urlsafe(32))"), then re-run.
"@
}

# ---------------------------------------------------------------------------
# Read .env into the service environment (read only, never written back)
# ---------------------------------------------------------------------------

Write-Step "Reading configuration from $EnvFile"

$envPairs = New-Object System.Collections.Specialized.OrderedDictionary
$lineNo = 0
foreach ($line in (Get-Content -LiteralPath $EnvFile -Encoding UTF8)) {
    $lineNo++
    $trimmed = $line.Trim()
    if ($trimmed.Length -eq 0 -or $trimmed.StartsWith('#')) { continue }
    if ($trimmed -match '^export\s+') { $trimmed = $trimmed -replace '^export\s+', '' }

    $split = $trimmed.IndexOf('=')
    if ($split -lt 1) {
        Write-Warn "Ignoring $EnvFile line ${lineNo}: not KEY=VALUE."
        continue
    }

    $key   = $trimmed.Substring(0, $split).Trim()
    $value = $trimmed.Substring($split + 1).Trim()

    # Strip one matched pair of surrounding quotes, the way env files are read.
    if ($value.Length -ge 2 -and
        (($value.StartsWith('"') -and $value.EndsWith('"')) -or
         ($value.StartsWith("'") -and $value.EndsWith("'")))) {
        $value = $value.Substring(1, $value.Length - 2)
    }

    if ($key -notmatch '^[A-Za-z_][A-Za-z0-9_]*$') {
        Write-Warn "Ignoring $EnvFile line ${lineNo}: '$key' is not a valid variable name."
        continue
    }
    if ($value.Length -eq 0) { continue }   # same as unset; let the app's default apply
    if ($value.Contains('"')) {
        throw "$EnvFile line ${lineNo}: $key contains a double quote, which cannot be passed through to the service environment. Remove it."
    }

    $envPairs[$key] = $value
}

# The two settings whose absence is silent and serious.
if (-not $envPairs.Contains('SECRET_KEY') -or $envPairs['SECRET_KEY'].Length -lt 32) {
    throw "SECRET_KEY is missing or too short in $EnvFile. Without it the app generates an ephemeral key, so every service restart and every reboot logs her out. Generate one with: python -c ""import secrets; print(secrets.token_urlsafe(32))"""
}
if (-not $envPairs.Contains('ACCESS_TOKEN') -or $envPairs['ACCESS_TOKEN'].Length -lt 16) {
    throw "ACCESS_TOKEN is missing or too short in $EnvFile. Without it anyone who knows the address is admitted, and the content is private photographs. Generate one with: python -c ""import secrets; print(secrets.token_urlsafe(32))"""
}

# DATA_DIR=/srv/data is correct inside the container and wrong here: Python
# resolves it to <current drive>\srv\data, so the database and the photographs
# quietly land somewhere other than C:\estudio\data.
foreach ($pathKey in @('DATA_DIR', 'CATALOG_DIR', 'PROVIDERS_CONFIG')) {
    if (-not $envPairs.Contains($pathKey)) { continue }
    $v = $envPairs[$pathKey]
    if ($v -notmatch '^[A-Za-z]:[\\/]' -and $v -notmatch '^\\\\') {
        throw "$pathKey in $EnvFile is '$v', which is not a Windows absolute path; the data would be written to a stray \$v on the current drive instead of $DataDir. Set a full path such as $DataDir, or delete the line to use the built-in default."
    }
}

Write-Note "$($envPairs.Count) variable(s) will be passed to the service."

# Set by us, not by her: unbuffered stdout so the log file is current while a
# batch is still running, and UTF-8 so accented filenames and Spanish text
# survive the Windows code page.
$envPairs['PYTHONUNBUFFERED'] = '1'
$envPairs['PYTHONUTF8']       = '1'
$envPairs['PYTHONIOENCODING'] = 'utf-8'

$envArgs = @()
foreach ($k in $envPairs.Keys) { $envArgs += ('{0}={1}' -f $k, $envPairs[$k]) }

# ---------------------------------------------------------------------------
# Directories
# ---------------------------------------------------------------------------

foreach ($dir in @($DataDir, $LogDir)) {
    if (-not (Test-Path -LiteralPath $dir -PathType Container)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
        Write-Note "Created $dir"
    }
}

# ---------------------------------------------------------------------------
# Install / reconfigure the service
# ---------------------------------------------------------------------------

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Step "Service $ServiceName already exists - stopping and reconfiguring it"
    & $script:Nssm stop $ServiceName 2>&1 | Out-Null
    Start-Sleep -Seconds 2
} else {
    Write-Step "Installing service $ServiceName"
    Invoke-Nssm install $ServiceName $VenvPython
}

# One worker, bound to loopback; nginx is the only thing that talks to it.
# --proxy-headers with --forwarded-allow-ips 127.0.0.1 is what lets the app
# trust X-Forwarded-Proto from nginx and mark the session cookie Secure. If
# the nginx vhost does not send that header she can never log in, so check it.
# --timeout-keep-alive 75 outlives nginx's idle upstream connections, so a
# reused connection is never closed underneath a request.
$appArgs = @(
    '-u', '-m', 'uvicorn', 'app.main:app',
    '--host', '127.0.0.1',
    '--port', "$Port",
    '--proxy-headers',
    '--forwarded-allow-ips', '127.0.0.1',
    '--timeout-keep-alive', '75'
) -join ' '

Invoke-Nssm set $ServiceName Application   $VenvPython
Invoke-Nssm set $ServiceName AppParameters $appArgs
Invoke-Nssm set $ServiceName AppDirectory  $ProjectRoot
Invoke-Nssm set $ServiceName DisplayName   'Estudio'
Invoke-Nssm set $ServiceName Description   'Estudio (FastAPI/uvicorn on 127.0.0.1:8000)'

# SERVICE_AUTO_START is the whole point: it comes back after a reboot with
# nobody logged in. Delayed start is deliberately not used - it would leave
# the site returning 502 for a minute or two after every reboot.
Invoke-Nssm set $ServiceName Start SERVICE_AUTO_START
Invoke-Nssm set $ServiceName ObjectName LocalSystem

# Configuration, injected from .env.
Invoke-Nssm set $ServiceName AppEnvironmentExtra @envArgs

# Logging. Append rather than truncate, and rotate at 10 MB so a long-running
# batch cannot fill the disk.
Invoke-Nssm set $ServiceName AppStdout $AppLog
Invoke-Nssm set $ServiceName AppStderr $AppLog
Invoke-Nssm set $ServiceName AppStdoutCreationDisposition 4
Invoke-Nssm set $ServiceName AppStderrCreationDisposition 4
Invoke-Nssm set $ServiceName AppRotateFiles 1
Invoke-Nssm set $ServiceName AppRotateOnline 1
Invoke-Nssm set $ServiceName AppRotateBytes 10485760

# Crash handling. AppThrottle stops a permanently broken build from restarting
# in a tight loop.
Invoke-Nssm set $ServiceName AppExit Default Restart
Invoke-Nssm set $ServiceName AppRestartDelay 5000
Invoke-Nssm set $ServiceName AppThrottle 10000

# Stop by sending Ctrl+C first and waiting, so uvicorn can close open SSE
# streams instead of being killed in the middle of a batch.
Invoke-Nssm set $ServiceName AppStopMethodSkip 0
Invoke-Nssm set $ServiceName AppStopMethodConsole 20000

# Belt and braces: if the wrapper itself ever dies, Windows restarts it.
& sc.exe failure $ServiceName 'reset=' '86400' 'actions=' 'restart/5000/restart/15000/restart/60000' | Out-Null

Write-Step "Starting $ServiceName"
Invoke-Nssm start $ServiceName

# ---------------------------------------------------------------------------
# Prove it is actually serving before claiming success
# ---------------------------------------------------------------------------

Write-Step "Waiting for http://127.0.0.1:$Port/health"

$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $response = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 5
        if ($response.StatusCode -eq 200) { $healthy = $true; break }
    } catch {
        # Not up yet.
    }
}

if (-not $healthy) {
    Write-Warn 'The service did not answer /health within 30 seconds.'
    if (Test-Path -LiteralPath $AppLog) {
        Write-Host ''
        Write-Host "--- last 40 lines of $AppLog ---" -ForegroundColor Yellow
        Get-Content -LiteralPath $AppLog -Tail 40
    }
    throw "Estudio is installed but not serving. Fix the error above, then run: $script:Nssm restart $ServiceName"
}

Write-Note '/health answered 200.'

# ---------------------------------------------------------------------------
# Optional: nginx as a service, plus a nightly reload so a renewed certificate
# is actually picked up. A certificate that renews without a reload is the
# classic 90-day time bomb - nginx keeps serving the expired one until someone
# restarts it by hand.
# ---------------------------------------------------------------------------

if ($NginxDir) {
    $NginxDir = $NginxDir.TrimEnd('\')
    $NginxExe = Join-Path $NginxDir 'nginx.exe'
    if (-not (Test-Path -LiteralPath $NginxExe -PathType Leaf)) {
        throw "nginx.exe not found at $NginxExe"
    }

    Write-Step 'Checking the nginx configuration'
    $confTest = & $NginxExe -p $NginxDir -t 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "nginx -t failed; refusing to wrap a broken config in a service:`n$confTest"
    }
    Write-Note (($confTest | Select-Object -Last 1) -join '')

    $nginxExisting = Get-Service -Name $NginxService -ErrorAction SilentlyContinue
    if ($nginxExisting) {
        Write-Step "Service $NginxService already exists - stopping and reconfiguring it"
        & $script:Nssm stop $NginxService 2>&1 | Out-Null
        Start-Sleep -Seconds 2
    } else {
        Write-Step "Installing service $NginxService"
        Invoke-Nssm install $NginxService $NginxExe
    }

    Invoke-Nssm set $NginxService Application   $NginxExe
    Invoke-Nssm set $NginxService AppParameters "-p `"$NginxDir`""
    Invoke-Nssm set $NginxService AppDirectory  $NginxDir
    Invoke-Nssm set $NginxService DisplayName   'nginx'
    Invoke-Nssm set $NginxService Description   'nginx reverse proxy for Estudio'
    Invoke-Nssm set $NginxService Start SERVICE_AUTO_START
    Invoke-Nssm set $NginxService ObjectName LocalSystem
    Invoke-Nssm set $NginxService AppExit Default Restart
    Invoke-Nssm set $NginxService AppRestartDelay 5000
    Invoke-Nssm set $NginxService AppThrottle 10000
    Invoke-Nssm set $NginxService AppStopMethodConsole 10000

    Write-Step "Starting $NginxService"
    Invoke-Nssm start $NginxService

    # Nightly graceful reload. Cheap, and it means a certificate renewed by
    # win-acme at any hour is actually being served within a day, even if its
    # own post-renewal hook was never wired up.
    Write-Step "Registering scheduled task $NginxReloadTask"
    if (Get-ScheduledTask -TaskName $NginxReloadTask -ErrorAction SilentlyContinue) {
        Unregister-ScheduledTask -TaskName $NginxReloadTask -Confirm:$false
    }
    $action    = New-ScheduledTaskAction -Execute $NginxExe -Argument "-p `"$NginxDir`" -s reload" -WorkingDirectory $NginxDir
    $trigger   = New-ScheduledTaskTrigger -Daily -At ([datetime]'03:20')
    $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
    $settings  = New-ScheduledTaskSettingsSet -StartWhenAvailable -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries
    Register-ScheduledTask -TaskName $NginxReloadTask -Action $action -Trigger $trigger `
        -Principal $principal -Settings $settings `
        -Description 'Graceful nginx reload so a renewed TLS certificate is actually served.' | Out-Null
    Write-Note 'nginx will reload nightly at 03:20.'
}

# ---------------------------------------------------------------------------

Write-Host ''
Write-Host 'Estudio is installed and running.' -ForegroundColor Green
Write-Host ''
Write-Host "  service    $ServiceName (automatic start; survives reboot with nobody logged in)"
Write-Host "  listening  http://127.0.0.1:$Port"
Write-Host "  log        $AppLog"
Write-Host "  restart    $script:Nssm restart $ServiceName"
Write-Host "  remove     powershell -File ""$PSCommandPath"" -Uninstall"
Write-Host ''
Write-Host 'Still to verify by hand, because this script cannot:' -ForegroundColor Yellow
Write-Host '  * the nginx vhost sets  proxy_set_header X-Forwarded-Proto $scheme;'
Write-Host '    otherwise the session cookie is not marked Secure and she can never log in.'
Write-Host '  * location /events/ has  proxy_buffering off;  proxy_cache off;'
Write-Host '    chunked_transfer_encoding off;  and proxy_read_timeout 300s or more,'
Write-Host '    or previews look frozen and then all arrive at once at the end.'
Write-Host '  * client_max_body_size is at least 32m, or a 30 MB phone photo is'
Write-Host '    rejected by nginx with 413 before the app ever sees it.'
Write-Host "  * no root/alias pointing at $DataDir - those photographs must only ever"
Write-Host '    be served by the app, behind the auth cookie, and autoindex stays off.'
Write-Host ''

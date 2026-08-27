<#
.SYNOPSIS
    Puts nginx for Windows in front of the estudio app with a real Let's Encrypt
    certificate that renews itself AND reloads nginx when it does.

.DESCRIPTION
    Run once, as Administrator, on the VPS. Re-running is safe (idempotent).

    What it does, in order:
      1. Finds (or installs) nginx for Windows.
      2. Writes an HTTP-only nginx.conf that can answer the ACME challenge.
      3. Registers nginx as a SYSTEM scheduled task that starts AT BOOT with
         no human logged in, plus a 5-minute watchdog that revives it.
      4. Obtains a certificate with win-acme, writing PEM files nginx can read,
         and installs a renewal hook that RELOADS nginx. Without that hook the
         site works for 90 days and then dies quietly.
      5. Writes the real HTTPS nginx.conf, tests it, reloads.
      6. Proves the renewal hook works today rather than in 90 days.

    What it deliberately does NOT do:
      * It never writes, moves or templates C:\estudio\.env. That file holds
        SECRET_KEY; rewriting it invalidates the session cookie (she is logged
        out) and ACCESS_TOKEN (her private link stops working). It is only ever
        READ, to discover DOMAIN.
      * It never lets nginx serve C:\estudio\data off disk. Every /media/ URL
        is proxied to the app so the auth cookie is still checked. The only
        thing nginx serves from the filesystem is the ACME challenge directory.

.PARAMETER Domain
    Public hostname. Defaults to DOMAIN= in C:\estudio\.env (read-only).

.PARAMETER Email
    Contact address for Let's Encrypt expiry warnings. Required.

.PARAMETER Staging
    Use the Let's Encrypt staging CA. Use this for the first run: the
    production CA has a hard rate limit of 5 failures per account per hour.

.EXAMPLE
    .\setup-tls.ps1 -Email nayane@example.com -Staging
    .\setup-tls.ps1 -Email nayane@example.com
#>

[CmdletBinding()]
param(
    [string]   $Domain,
    [Parameter(Mandatory = $true)]
    [string]   $Email,
    [string]   $ProjectRoot  = 'C:\estudio',
    [string]   $NginxRoot    = 'C:\nginx',
    [string]   $NginxVersion = '1.28.0',
    [string]   $WacsPath,
    [int]      $AppPort      = 8000,
    [switch]   $Staging
)

$ErrorActionPreference = 'Stop'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

function Say  { param([string]$m) Write-Host "  $m" }
function Step { param([string]$m) Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Warn { param([string]$m) Write-Host "  ! $m" -ForegroundColor Yellow }
function Fail { param([string]$m) Write-Host ""; Write-Host "FAILED: $m" -ForegroundColor Red; exit 1 }

# nginx.conf is parsed as bytes. A UTF-8 BOM at the top makes nginx report
# "unknown directive" on line 1, which reads like a syntax error you did not
# make. Windows PowerShell's -Encoding UTF8 writes a BOM, so never use it here.
function Write-TextNoBom {
    param([string]$Path, [string]$Text)
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($false)))
}

# nginx treats \ as an escape character inside its config, so every Windows
# path written into nginx.conf must use forward slashes.
function ConvertTo-NginxPath { param([string]$p) return ($p -replace '\\', '/') }

# Native stderr redirection inside Windows PowerShell corrupts exit codes, so
# run nginx.exe out of process and read its output back from files.
function Invoke-Nginx {
    param([string[]]$Arguments)
    $out = Join-Path $env:TEMP ("nginx-out-" + [guid]::NewGuid().ToString('N') + '.txt')
    $err = Join-Path $env:TEMP ("nginx-err-" + [guid]::NewGuid().ToString('N') + '.txt')
    $all = @('-p', $script:NginxPrefix) + $Arguments
    $p = Start-Process -FilePath $script:NginxExe -ArgumentList $all -WorkingDirectory $NginxRoot `
                       -NoNewWindow -Wait -PassThru -RedirectStandardOutput $out -RedirectStandardError $err
    $text = ''
    foreach ($f in @($out, $err)) {
        if (Test-Path $f) {
            $text += (Get-Content $f -Raw)
            Remove-Item $f -Force -ErrorAction SilentlyContinue
        }
    }
    return [pscustomobject]@{ ExitCode = $p.ExitCode; Output = $text }
}

function Test-TcpPort {
    param([string]$TargetHost, [int]$Port, [int]$TimeoutMs = 1500)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $ar = $client.BeginConnect($TargetHost, $Port, $null, $null)
        if (-not $ar.AsyncWaitHandle.WaitOne($TimeoutMs)) { return $false }
        $client.EndConnect($ar)
        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

# --------------------------------------------------------------------------
# 0. preconditions
# --------------------------------------------------------------------------

Step 'Checking preconditions'

$me = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $me.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Fail 'Run this from an elevated PowerShell (Run as Administrator).'
}

if (-not (Test-Path $ProjectRoot)) { Fail "Project root not found: $ProjectRoot" }

# .env is READ, never written. It holds SECRET_KEY and ACCESS_TOKEN; replacing
# it logs her out and breaks her private link.
$envFile = Join-Path $ProjectRoot '.env'
if (-not $Domain) {
    if (-not (Test-Path $envFile)) { Fail "No -Domain given and no $envFile to read it from." }
    foreach ($line in (Get-Content $envFile)) {
        if ($line -match '^\s*DOMAIN\s*=\s*(.+?)\s*$') {
            $Domain = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    if (-not $Domain) { Fail "DOMAIN is not set in $envFile. Pass -Domain instead." }
    Say "Domain read from .env (file not modified): $Domain"
}
if ($Domain -match '^(localhost|127\.|.*\.local)$') {
    Fail "Let's Encrypt cannot issue for '$Domain'. It must be a public name."
}

try {
    $resolved = Resolve-DnsName -Name $Domain -Type A -ErrorAction Stop | Where-Object { $_.IPAddress }
    Say ("DNS $Domain -> " + (($resolved | ForEach-Object { $_.IPAddress }) -join ', '))
} catch {
    Warn "$Domain does not resolve yet. Validation will fail until it points at this box."
}

if (Test-TcpPort -TargetHost '127.0.0.1' -Port $AppPort) {
    Say "App is answering on 127.0.0.1:$AppPort"
} else {
    Warn "Nothing is listening on 127.0.0.1:$AppPort. TLS will still be set up, but every page is 502 until uvicorn runs."
}

# Port 80 must be ours: win-acme's http-01 challenge is served through nginx.
# Windows Server frequently ships with IIS squatting on 80.
try {
    $owners = Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction Stop
    foreach ($o in $owners) {
        $proc = Get-Process -Id $o.OwningProcess -ErrorAction SilentlyContinue
        if ($proc -and $proc.ProcessName -notmatch 'nginx') {
            Fail "Port 80 is held by '$($proc.ProcessName)' (pid $($proc.Id)). If that is IIS: Stop-Service W3SVC; Set-Service W3SVC -StartupType Disabled"
        }
    }
} catch {
    # Nothing listening on 80 yet is the normal case and throws here.
}

# --------------------------------------------------------------------------
# 1. nginx
# --------------------------------------------------------------------------

Step 'nginx for Windows'

$script:NginxExe    = Join-Path $NginxRoot 'nginx.exe'
$script:NginxPrefix = ConvertTo-NginxPath ($NginxRoot.TrimEnd('\') + '\')

if (-not (Test-Path $script:NginxExe)) {
    Say "Not found at $script:NginxExe - downloading nginx $NginxVersion"
    $zip = Join-Path $env:TEMP "nginx-$NginxVersion.zip"
    $tmp = Join-Path $env:TEMP ("nginx-unzip-" + [guid]::NewGuid().ToString('N'))
    Invoke-WebRequest -Uri "https://nginx.org/download/nginx-$NginxVersion.zip" -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $tmp -Force
    $src = Join-Path $tmp "nginx-$NginxVersion"
    if (-not (Test-Path $src)) { Fail "Unexpected archive layout under $tmp" }
    New-Item -ItemType Directory -Path $NginxRoot -Force | Out-Null
    Copy-Item -Path (Join-Path $src '*') -Destination $NginxRoot -Recurse -Force
    Remove-Item $zip, $tmp -Recurse -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $script:NginxExe)) { Fail "nginx.exe still missing under $NginxRoot" }
}
foreach ($d in @('logs', 'temp', 'conf')) {
    $p = Join-Path $NginxRoot $d
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
}
Say "Using $script:NginxExe"

$confPath = Join-Path $NginxRoot 'conf\nginx.conf'
$backup   = Join-Path $NginxRoot 'conf\nginx.conf.original'
if ((Test-Path $confPath) -and -not (Test-Path $backup)) {
    Copy-Item $confPath $backup -Force
    Say "Stock config saved as $backup"
}

$acmeRoot = Join-Path $ProjectRoot 'acme'
$pemPath  = Join-Path $ProjectRoot 'certs'
foreach ($p in @($acmeRoot, $pemPath)) {
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p -Force | Out-Null }
}

# Private key on disk: SYSTEM and Administrators only. Group names are
# localised on non-English Windows, so grant by SID, not by name.
& icacls.exe $pemPath /inheritance:r /grant '*S-1-5-18:(OI)(CI)F' /grant '*S-1-5-32-544:(OI)(CI)F' | Out-Null

# --------------------------------------------------------------------------
# 2. config templates
# --------------------------------------------------------------------------

$httpOnlyConf = @'
# Generated by deploy/windows/setup-tls.ps1 - bootstrap (pre-certificate).
worker_processes  1;

events {
    worker_connections  1024;
}

http {
    include       mime.types;
    default_type  application/octet-stream;

    sendfile      off;          # not supported by nginx on Windows
    server_tokens off;
    autoindex     off;          # never list a directory

    access_log  logs/access.log;
    error_log   logs/error.log warn;

    server {
        listen      80;
        server_name __DOMAIN__;

        location ^~ /.well-known/acme-challenge/ {
            root         "__ACMEROOT__";
            default_type text/plain;
            autoindex    off;
        }

        location / {
            return 503 "estudio: certificate not issued yet\n";
        }
    }
}
'@

$tlsConf = @'
# Generated by deploy/windows/setup-tls.ps1 - do not hand-edit; re-run instead.
worker_processes  1;

events {
    worker_connections  1024;
}

http {
    include       mime.types;
    default_type  application/octet-stream;

    sendfile      off;          # not supported by nginx on Windows
    server_tokens off;
    autoindex     off;          # never list a directory, anywhere

    # A batch of previews takes 60-180 seconds, and /previews, /finals and the
    # pages that wait on them must outlive that. nginx's default read timeout
    # is 60s, which would 504 a perfectly healthy generation run.
    proxy_connect_timeout  10s;
    proxy_send_timeout     300s;
    proxy_read_timeout     300s;
    send_timeout           300s;
    keepalive_timeout      75s;

    # The app enforces its own 30 MB limit and returns a readable message, so
    # nginx's ceiling sits above it and the app is the one that answers. The
    # default 1 MB would reject every phone photo with a bare 413 page.
    client_max_body_size   40m;
    client_body_timeout    300s;
    client_header_timeout  60s;

    gzip           on;
    gzip_proxied   any;
    gzip_types     text/css text/plain application/javascript application/json image/svg+xml;

    access_log  logs/access.log;
    error_log   logs/error.log warn;

    # Plain HTTP: only the ACME challenge, everything else redirected.
    server {
        listen      80;
        server_name __DOMAIN__;

        location ^~ /.well-known/acme-challenge/ {
            root         "__ACMEROOT__";
            default_type text/plain;
            autoindex    off;
        }

        location / {
            return 301 https://$host$request_uri;
        }
    }

    server {
        listen      443 ssl;
        server_name __DOMAIN__;

        ssl_certificate           "__CHAIN__";
        ssl_certificate_key       "__KEY__";
        ssl_protocols             TLSv1.2 TLSv1.3;
        ssl_prefer_server_ciphers off;
        ssl_session_cache         shared:SSL:10m;
        ssl_session_timeout       1d;

        # Photographs of a real, identifiable person: no framing, no sniffing,
        # and no referrer leaking an image URL to wherever a link is pasted.
        add_header X-Content-Type-Options    nosniff       always;
        add_header X-Frame-Options           DENY          always;
        add_header Referrer-Policy           no-referrer   always;
        add_header Strict-Transport-Security "max-age=31536000" always;

        location ^~ /.well-known/acme-challenge/ {
            root         "__ACMEROOT__";
            default_type text/plain;
            autoindex    off;
        }

        # Server-sent events. This block is the whole reason the deployment is
        # delicate. With nginx's default proxy_buffering the tiles pile up in
        # nginx and land in one burst at the end, so a working 3-minute batch
        # looks frozen. gzip has the same effect, and the default 60s read
        # timeout would cut the stream mid-batch.
        location /events/ {
            proxy_pass         http://127.0.0.1:__APPPORT__;
            proxy_http_version 1.1;
            proxy_set_header   Connection "";
            proxy_set_header   Host              $host;
            proxy_set_header   X-Real-IP         $remote_addr;
            proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;
            proxy_set_header   X-Forwarded-Proto $scheme;

            proxy_buffering    off;
            proxy_cache        off;
            gzip               off;
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
        }

        # Everything else, /media/ included. /media/ is NOT served from disk:
        # those files live in C:\estudio\data and are private photographs behind
        # the session cookie. nginx is never given a root for them, or the URLs
        # become public to anyone who guesses one.
        location / {
            proxy_pass         http://127.0.0.1:__APPPORT__;
            proxy_http_version 1.1;
            proxy_set_header   Connection "";
            proxy_set_header   Host              $host;
            proxy_set_header   X-Real-IP         $remote_addr;
            proxy_set_header   X-Forwarded-For   $proxy_add_x_forwarded_for;

            # The app marks its session cookie Secure from this header. It is
            # set to the scheme nginx actually served, and set unconditionally,
            # so a client-supplied X-Forwarded-Proto can never be believed.
            # Wrong here means either the cookie is dropped and she can never
            # log in, or the session travels without Secure.
            proxy_set_header   X-Forwarded-Proto $scheme;
        }
    }
}
'@

function Set-NginxConf {
    param([string]$Template, [string]$Chain, [string]$Key)
    $text = $Template.
        Replace('__DOMAIN__',   $Domain).
        Replace('__ACMEROOT__', (ConvertTo-NginxPath $acmeRoot)).
        Replace('__APPPORT__',  [string]$AppPort)
    if ($Chain) {
        $text = $text.Replace('__CHAIN__', (ConvertTo-NginxPath $Chain)).
                      Replace('__KEY__',   (ConvertTo-NginxPath $Key))
    }
    Write-TextNoBom -Path $confPath -Text $text
}

Step 'Writing bootstrap config (HTTP only)'
Set-NginxConf -Template $httpOnlyConf
$t = Invoke-Nginx @('-t')
if ($t.ExitCode -ne 0) { Fail "nginx rejected the bootstrap config:`n$($t.Output)" }
Say 'Config OK'

# --------------------------------------------------------------------------
# 3. firewall + start at boot
# --------------------------------------------------------------------------

Step 'Firewall'
foreach ($port in @(80, 443)) {
    $name = "estudio nginx TCP $port"
    if (-not (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $name -Direction Inbound -Protocol TCP `
                            -LocalPort $port -Action Allow -Profile Any | Out-Null
        Say "Opened $port"
    } else {
        Say "$port already allowed"
    }
}

Step 'Registering nginx to start at boot'
# nginx for Windows is a console program, not a Windows service, so it runs as
# a SYSTEM scheduled task: SYSTEM means it comes up after a reboot with nobody
# logged in. The task engine's default 3-day execution limit must be cleared,
# or it would kill nginx mid-week.
$taskName  = 'estudio-nginx'
$action    = New-ScheduledTaskAction -Execute $script:NginxExe `
                -Argument ('-p "' + $script:NginxPrefix + '"') -WorkingDirectory $NginxRoot
$triggers  = @(
    (New-ScheduledTaskTrigger -AtStartup),
    # Cheap watchdog: with MultipleInstances=IgnoreNew this is a no-op while
    # nginx is alive, and restarts it within 5 minutes if it ever dies.
    (New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
        -RepetitionInterval (New-TimeSpan -Minutes 5) `
        -RepetitionDuration (New-TimeSpan -Days 3650))
)
$principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$settings  = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -StartWhenAvailable `
                -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                -ExecutionTimeLimit (New-TimeSpan -Seconds 0) `
                -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $triggers `
                       -Principal $principal -Settings $settings -Force | Out-Null
Say "Scheduled task '$taskName' registered (SYSTEM, at startup)"

Get-Process -Name 'nginx' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Start-ScheduledTask -TaskName $taskName
Start-Sleep -Seconds 3
if (-not (Get-Process -Name 'nginx' -ErrorAction SilentlyContinue)) {
    Fail "nginx did not start. See $NginxRoot\logs\error.log"
}
Say 'nginx running (started exactly the way a reboot will start it)'

# --------------------------------------------------------------------------
# 4. the renewal hook, written before the certificate exists
# --------------------------------------------------------------------------

Step 'Renewal hook'
# A certificate that renews but never reloads nginx is the classic silent
# death: everything works for 90 days, then every request fails at once.
$hookPath = Join-Path $ProjectRoot 'deploy\windows\reload-nginx.ps1'
$hook = @'
# Generated by setup-tls.ps1. Run by win-acme as SYSTEM after every renewal.
# Its whole job is to make nginx pick up the new certificate files.
$ErrorActionPreference = 'Stop'
$nginxRoot = '__NGINXROOT__'
$nginxExe  = Join-Path $nginxRoot 'nginx.exe'
$prefix    = '__PREFIX__'
$log       = Join-Path $nginxRoot 'logs\renewal-reload.log'
function Write-Log { param($m) Add-Content -Path $log -Value ("{0}  {1}" -f (Get-Date -Format s), $m) }

try {
    $test = Start-Process -FilePath $nginxExe -ArgumentList @('-p', $prefix, '-t') `
                          -WorkingDirectory $nginxRoot -NoNewWindow -Wait -PassThru
    if ($test.ExitCode -ne 0) { Write-Log 'config test FAILED - not reloading'; exit 1 }

    if (Get-Process -Name nginx -ErrorAction SilentlyContinue) {
        $r = Start-Process -FilePath $nginxExe -ArgumentList @('-p', $prefix, '-s', 'reload') `
                           -WorkingDirectory $nginxRoot -NoNewWindow -Wait -PassThru
        if ($r.ExitCode -ne 0) {
            # nginx on Windows occasionally refuses a signal; a full restart is
            # a few hundred milliseconds of downtime and always works.
            Write-Log 'reload failed, restarting task'
            Get-Process -Name nginx -ErrorAction SilentlyContinue | Stop-Process -Force
            Start-Sleep -Seconds 1
            Start-ScheduledTask -TaskName 'estudio-nginx'
        } else {
            Write-Log 'reloaded'
        }
    } else {
        Write-Log 'nginx was not running - starting it'
        Start-ScheduledTask -TaskName 'estudio-nginx'
    }
    exit 0
} catch {
    Write-Log ('ERROR ' + $_.Exception.Message)
    exit 1
}
'@
$hook = $hook.Replace('__NGINXROOT__', $NginxRoot.TrimEnd('\')).Replace('__PREFIX__', $script:NginxPrefix)
Write-TextNoBom -Path $hookPath -Text $hook
Say "Wrote $hookPath"

# --------------------------------------------------------------------------
# 5. certificate
# --------------------------------------------------------------------------

Step 'Certificate (win-acme)'

if ($WacsPath) {
    if (-not (Test-Path $WacsPath)) { Fail "-WacsPath does not exist: $WacsPath" }
    $wacs = $WacsPath
} else {
    $wacsDir = Join-Path $ProjectRoot 'tools\win-acme'
    $wacs    = Join-Path $wacsDir 'wacs.exe'
    if (-not (Test-Path $wacs)) {
        Say 'Downloading win-acme (latest release)'
        try {
            $rel = Invoke-RestMethod -Uri 'https://api.github.com/repos/win-acme/win-acme/releases/latest' `
                                     -Headers @{ 'User-Agent' = 'estudio-setup' } -UseBasicParsing
            $asset = $rel.assets | Where-Object { $_.name -like '*x64.pluggable.zip' } | Select-Object -First 1
            if (-not $asset) { throw 'no x64.pluggable.zip asset in the latest release' }
            $zip = Join-Path $env:TEMP $asset.name
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing
            New-Item -ItemType Directory -Path $wacsDir -Force | Out-Null
            Expand-Archive -Path $zip -DestinationPath $wacsDir -Force
            Remove-Item $zip -Force -ErrorAction SilentlyContinue
        } catch {
            Fail ("Could not fetch win-acme automatically ($($_.Exception.Message)). " +
                  "Download the x64 'pluggable' zip from " +
                  "https://github.com/win-acme/win-acme/releases, unpack it, " +
                  "and re-run with -WacsPath <path>\wacs.exe")
        }
    }
    if (-not (Test-Path $wacs)) { Fail "wacs.exe not found under $wacsDir" }
}
Say "Using $wacs"

$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$wacsArgs = @(
    '--source', 'manual',
    '--host', $Domain,
    '--friendlyname', 'estudio',
    # http-01 through the nginx webroot. Do NOT use win-acme's self-hosting
    # validation here: it wants port 80 for itself, and nginx already has it.
    '--validation', 'filesystem',
    '--webroot', $acmeRoot,
    # nginx cannot read the Windows certificate store; it needs PEM on disk.
    '--store', 'pemfiles',
    '--pemfilespath', $pemPath,
    # The hook. This is what keeps the site alive past day 90.
    '--installation', 'script',
    '--script', $psExe,
    '--scriptparameters', ('-NoProfile -ExecutionPolicy Bypass -File "' + $hookPath + '"'),
    '--accepttos',
    '--emailaddress', $Email
)
if ($Staging) {
    $wacsArgs += @('--baseuri', 'https://acme-staging-v02.api.letsencrypt.org/')
    Warn 'STAGING certificate: browsers will not trust it. Re-run without -Staging once this succeeds.'
}

$proc = Start-Process -FilePath $wacs -ArgumentList $wacsArgs -WorkingDirectory (Split-Path $wacs) `
                      -NoNewWindow -Wait -PassThru
if ($proc.ExitCode -ne 0) {
    Fail ("win-acme exited $($proc.ExitCode). Its log is under " +
          "$env:ProgramData\win-acme\. Usual causes: DNS not pointing here yet, " +
          "or port 80 not reachable from the internet.")
}

# Discover the real filenames instead of assuming them - win-acme sanitises the
# friendly name, and guessing the path is how this breaks on someone else's box.
$chain = Get-ChildItem -Path $pemPath -Filter '*-chain.pem' -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime -Descending | Select-Object -First 1
$key   = Get-ChildItem -Path $pemPath -Filter '*-key.pem' -ErrorAction SilentlyContinue |
         Sort-Object LastWriteTime -Descending | Select-Object -First 1
if (-not $chain -or -not $key) { Fail "win-acme reported success but no PEM pair appeared in $pemPath" }
Say "Certificate: $($chain.Name)"
Say "Private key: $($key.Name)"

# --------------------------------------------------------------------------
# 6. real config
# --------------------------------------------------------------------------

Step 'Writing HTTPS config'
Set-NginxConf -Template $tlsConf -Chain $chain.FullName -Key $key.FullName
$t = Invoke-Nginx @('-t')
if ($t.ExitCode -ne 0) {
    Fail "nginx rejected the TLS config (nothing reloaded, the running nginx is untouched):`n$($t.Output)"
}
Say 'Config OK'

# Reload through the same hook win-acme will call, so a broken hook shows up
# now rather than in three months.
Step 'Exercising the renewal hook'
$h = Start-Process -FilePath $psExe `
                   -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $hookPath) `
                   -NoNewWindow -Wait -PassThru
if ($h.ExitCode -ne 0) { Fail "The renewal hook failed. See $NginxRoot\logs\renewal-reload.log" }
Say 'Hook ran and nginx reloaded'

# --------------------------------------------------------------------------
# 7. verify
# --------------------------------------------------------------------------

Step 'Verifying'

$renew = @(Get-ScheduledTask | Where-Object { $_.TaskName -like 'win-acme*' })
if ($renew.Count -gt 0) {
    Say "Renewal task present: $($renew[0].TaskName)"
} else {
    Warn 'No win-acme renewal task found. The certificate will EXPIRE in ~90 days.'
    Warn "Create it with:  `"$wacs`" --setuptaskscheduler"
}

$boot = Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
if ($boot) { Say "Boot task present: $taskName ($($boot.State))" } else { Warn "Boot task $taskName missing." }

try {
    $r = Invoke-WebRequest -Uri "https://$Domain/health" -UseBasicParsing -TimeoutSec 25
    Say "https://$Domain/health -> $($r.StatusCode)"
} catch {
    Warn ("Could not fetch https://$Domain/health from this machine ({0})." -f $_.Exception.Message)
    Warn 'Some hosts cannot reach their own public IP. Check from a phone before worrying.'
}

Write-Host ''
Write-Host 'Done.' -ForegroundColor Green
Say "Config:        $confPath"
Say "Certificates:  $pemPath"
Say "Renewal hook:  $hookPath  (log: $NginxRoot\logs\renewal-reload.log)"
Say ".env:          untouched"
Write-Host ''
Say 'Confirm SSE is not buffered - tiles should trickle in, not arrive all at once:'
Say "  curl -N -H `"Cookie: estudio_session=<value>`" https://$Domain/events/<session_id>"

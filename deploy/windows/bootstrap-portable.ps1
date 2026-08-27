<#
  Stands up Estudio at C:\estudio using a PORTABLE Python.

  WHY PORTABLE. Group policy on this box blocks installers -- winget failed
  with 1625 (ERROR_INSTALL_POLICY_FAILURE) and the python.org installer will
  hit the same wall. The embeddable build is a zip: no installer, no registry,
  no elevation, nothing for policy to refuse.

  WHY C:\estudio AND NOT THE PROJECT FOLDER. The working copy lives under
  "Documents\8.22 IMG". That path contains a space AND a dot, which is a
  well-known source of Windows service failures -- the service manager splits
  an unquoted path at the space and launches "C:\Users\...\Documents\8.22"
  instead. Deploying to a short, space-free path removes that class of bug
  before it happens.

  Safe to re-run. Does not need Administrator.
#>
param(
    [string]$Root       = 'C:\estudio',
    [string]$Source     = 'C:\Users\Administrator\Documents\8.22 IMG',
    [string]$PythonVer  = '3.11.9',
    [switch]$SkipDeps
)

$ErrorActionPreference = 'Stop'
function Step($m) { Write-Host "`n$m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "    $m" -ForegroundColor Green }
function Note($m) { Write-Host "    $m" -ForegroundColor DarkGray }

# Running a native exe from PowerShell 5.1 is a trap worth wrapping once.
# `& python.exe ... 2>&1` wraps every stderr line in an ErrorRecord
# (NativeCommandError), and with $ErrorActionPreference = 'Stop' that turns
# any tool which merely WRITES to stderr into a fatal script error -- even on
# exit code 0. pip and python both write perfectly normal output to stderr.
# So: relax the preference around the call, and judge success by the exit code,
# which is the only thing that actually means success.
function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]   $Exe,
        [Parameter(Mandatory)][string[]] $Arguments,
        [switch] $Quiet
    )
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $output = & $Exe @Arguments 2>&1
        $code = $LASTEXITCODE
        if (-not $Quiet) { $output | ForEach-Object { Note $_ } }
        return [pscustomobject]@{ Code = $code; Output = ($output | Out-String) }
    } finally { $ErrorActionPreference = $previous }
}

$py = Join-Path $Root 'python'
$exe = Join-Path $py 'python.exe'

Step '[1] directories'
foreach ($d in @($Root, $py, "$Root\data", "$Root\logs")) {
    New-Item -ItemType Directory -Force -Path $d | Out-Null
}
Ok "root: $Root"

Step '[2] portable Python'
if (Test-Path $exe) {
    Ok "already present: $((& $exe --version 2>&1))"
} else {
    $zip = Join-Path $env:TEMP "python-$PythonVer-embed.zip"
    $url = "https://www.python.org/ftp/python/$PythonVer/python-$PythonVer-embed-amd64.zip"
    Note "downloading $url"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest $url -OutFile $zip -UseBasicParsing
    Expand-Archive $zip -DestinationPath $py -Force
    Remove-Item $zip -Force
    Ok "installed: $((& $exe --version 2>&1))"
}

Step '[2b] python path config'
# The ._pth file is the whole reason a portable Python is fiddly. When one is
# present, Python runs in ISOLATED PATH MODE: it ignores PYTHONPATH, it does
# NOT add the script's own directory to sys.path, and the file below is the
# entire path. So an app sitting next to the interpreter is still unimportable
# until it is named here explicitly.
#
# A bare "." does not help either - it resolves relative to the interpreter
# directory (C:\estudio\python), not the application root.
#
# Run every time, not only on first install: an upgrade or a re-extract
# silently restores the stock file and the app stops importing.
$pth = Get-ChildItem $py -Filter 'python*._pth' | Select-Object -First 1
if (-not $pth) { throw "no ._pth found in $py" }

$lines = @(Get-Content $pth.FullName) |
    ForEach-Object { $_ -replace '^#\s*import site', 'import site' }

foreach ($entry in @('Lib\site-packages', $Root)) {
    if ($lines -notcontains $entry) { $lines += $entry }
}
Set-Content $pth.FullName $lines -Encoding ascii
Ok "$($pth.Name): site enabled, app root on sys.path"
Note ($lines -join '  |  ')

Step '[3] pip'
# The embeddable build ships without pip. Check the filesystem rather than
# probing with the interpreter: a failed probe writes a traceback to stderr,
# and that is the exact thing that used to kill this script.
if (-not (Test-Path (Join-Path $py 'Lib\site-packages\pip'))) {
    $getpip = Join-Path $env:TEMP 'get-pip.py'
    Note 'bootstrapping pip'
    Invoke-WebRequest 'https://bootstrap.pypa.io/get-pip.py' -OutFile $getpip -UseBasicParsing
    $r = Invoke-Native $exe @($getpip, '--no-warn-script-location', '-q') -Quiet
    Remove-Item $getpip -Force
    if ($r.Code -ne 0) { throw "get-pip failed (exit $($r.Code)): $($r.Output)" }
}
$r = Invoke-Native $exe @('-m','pip','--version') -Quiet
if ($r.Code -ne 0) { throw "pip still unavailable: $($r.Output)" }
Ok (($r.Output.Trim() -split ' from ')[0])

Step '[4] application code'
# Copied, not moved: the working copy stays where it is and stays canonical.
# data\ is excluded so a redeploy never clobbers her uploads or the database.
$items = @('app','catalog','scripts','tests','requirements.txt','requirements-cv.txt','providers.json','pytest.ini')
foreach ($i in $items) {
    $src = Join-Path $Source $i
    if (Test-Path $src) {
        Copy-Item $src -Destination $Root -Recurse -Force
        Note "copied $i"
    }
}
Ok 'code in place'

Step '[5] dependencies'
if ($SkipDeps) { Note 'skipped' }
else {
    $req = Join-Path $Root 'requirements.txt'
    $r = Invoke-Native $exe @('-m','pip','install','-q','-r',$req,'--no-warn-script-location') -Quiet
    if ($r.Code -ne 0) { throw "pip install failed (exit $($r.Code)):`n$($r.Output)" }
    Ok 'requirements.txt installed'
    Note 'requirements-cv.txt NOT installed - without it the anti-slimming'
    Note 'proportion check cannot run. Install it once the pose model is on disk.'
}

Step '[6] .env'
$envPath = Join-Path $Root '.env'
if (Test-Path $envPath) {
    # NEVER overwrite: a new SECRET_KEY logs her out, and a new ACCESS_TOKEN
    # invalidates the private link she has saved on her phone.
    Ok 'already exists - left untouched'
} else {
    $secret = (Invoke-Native $exe @('-c','import secrets;print(secrets.token_urlsafe(32))') -Quiet).Output.Trim()
    $token  = (Invoke-Native $exe @('-c','import secrets;print(secrets.token_urlsafe(24))') -Quiet).Output.Trim()
    if (-not $secret -or -not $token) { throw 'could not generate secrets' }
    @"
OWNER_NAME=Nayane
DOMAIN=perfect-img.duckdns.org

SECRET_KEY=$secret
ACCESS_TOKEN=$token
SESSION_DAYS=365

# Fill these in. Until then: photo analysis falls back to pixel heuristics,
# the visual judge does not run, and images are placeholder cards.
ANTHROPIC_API_KEY=
FAL_API_KEY=

ANALYSER_MODEL=claude-opus-5
JUDGE_MODEL=claude-haiku-4-5

DATA_DIR=$($Root -replace '\\','/')/data
CATALOG_DIR=$($Root -replace '\\','/')/catalog

PREVIEW_COUNT=6
GENERATION_CONCURRENCY=6
CV_CONCURRENCY=2
MAX_UPLOAD_MB=30
"@.Trim() | ForEach-Object {
        # No BOM. app.config reads this with utf-8-sig so it would cope, but
        # a BOM here is invisible and turns the first key into "﻿OWNER_NAME"
        # for anything less forgiving that ever reads the file.
        [IO.File]::WriteAllText($envPath, $_ + "`r`n", (New-Object Text.UTF8Encoding $false))
    }
    Ok 'created with fresh secrets'
}

Step '[7] seed'
Push-Location $Root
try {
    Invoke-Native $exe @('scripts\make_icons.py')   | Out-Null
    Invoke-Native $exe @('scripts\seed_catalog.py') | Out-Null
} finally { Pop-Location }

Step '[8] verify it actually boots'
Push-Location $Root
try {
    # The probe MUST live in $Root. Python puts the SCRIPT's directory on
    # sys.path, never the working directory, so a probe in TEMP cannot import
    # `app` no matter where it is run from.
    $probe = Join-Path $Root '_probe.py'
    $body = @"
from app.config import DOTENV_LOADED, settings
from app.main import services
print('dotenv values loaded:', DOTENV_LOADED)
print('owner:', settings.owner_name)
print('data dir:', settings.data_dir)
print('catalog looks:', len(services.catalog))
for w in services.warnings():
    print('WARN:', w)
"@
    # WriteAllText, not Set-Content: PowerShell 5.1's utf8 encoding emits a
    # BOM, which shows up in tracebacks and can break naive parsers.
    [IO.File]::WriteAllText($probe, $body, (New-Object Text.UTF8Encoding $false))
    $r = Invoke-Native $exe @($probe)
    Remove-Item $probe -Force -ErrorAction SilentlyContinue
    if ($r.Code -ne 0) { throw "the app did not import cleanly:`n$($r.Output)" }
} finally { Pop-Location }

$token = (Select-String -Path $envPath -Pattern '^ACCESS_TOKEN=(.+)$').Matches.Groups[1].Value
Write-Host "`nReady." -ForegroundColor Green
Write-Host @"

  Start it:
      cd $Root
      .\python\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1

  Her private link, once nginx is serving HTTPS:
      https://perfect-img.duckdns.org/e/$token

  Keep that link. It is the entire login - there is no password.
"@

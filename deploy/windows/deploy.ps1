<#
.SYNOPSIS
    Bare Windows Server  ->  running "estudio" (FastAPI + uvicorn behind nginx for Windows).

.DESCRIPTION
    Idempotent. Safe to re-run as the update path: it never overwrites .env, never
    regenerates secrets that already exist, and re-registers the scheduled tasks
    from scratch every time (they are cheap and disposable, the data is not).

    What it does, in order:
      1.  Preflight: admin, OS, code present, Python 3.11, installer-blocking policy
      2.  Two low-privilege local accounts (app + web) and the batch-logon right
      3.  Directory tree and ACLs (data writable, code not, web account nowhere near either)
      4.  venv at C:\estudio\venv, requirements.txt, optional requirements-cv.txt
      5.  .env with cryptographically generated SECRET_KEY and ACCESS_TOKEN (create-only)
      6.  Seed scripts (make_icons, seed_catalog)
      7.  Scheduled task at startup for uvicorn  (survives reboot, no human login)
      8.  nginx.conf tuned for SSE + uploads + the Secure-cookie forwarded-proto contract,
          plus a daily watcher that restarts nginx when the certificate is renewed
      9.  Firewall, health check, and the private magic link

.PARAMETER Domain
    The public hostname, e.g. estudio.example.com. Used for the nginx server_name and
    for the magic link printed at the end.

.EXAMPLE
    .\deploy.ps1 -Domain estudio.example.com

.NOTES
    Run from an elevated PowerShell (Windows PowerShell 5.1 or PowerShell 7).
    Things this script deliberately does NOT do, because guessing would be worse than asking:
      - download and run the Python installer (version URLs go stale; policy may block it)
      - download nginx (same reason; pass -NginxZip if you have the official zip)
      - obtain TLS certificates (there is no certbot for nginx on Windows; see the TLS section)
#>

[CmdletBinding()]
param(
    # Public hostname. Everything private is reached through https://$Domain/e/<token>.
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^[A-Za-z0-9.-]+$')]
    [string]$Domain,

    [string]$Root      = 'C:\estudio',
    [string]$NginxRoot = 'C:\nginx',

    # Path to the official nginx Windows zip (nginx.org/download/nginx-X.Y.Z.zip),
    # used only if $NginxRoot\nginx.exe is missing.
    [string]$NginxZip,

    # Skip the optional computer-vision extras.
    [switch]$SkipCv,

    # Skip make_icons / seed_catalog on this run.
    [switch]$SkipSeed,

    # Configure the app only; leave nginx alone.
    [switch]$SkipNginx
)

$ErrorActionPreference = 'Stop'

# ----------------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------------
$AppAccount   = 'estudio_app'   # runs uvicorn. Writes data. Reads code.
$WebAccount   = 'estudio_web'   # runs nginx.  Writes nginx logs/temp. Nothing else.
$AppTaskName  = 'estudio-app'
$WebTaskName  = 'estudio-nginx'
$CertTaskName = 'estudio-cert-reload'
$TaskPath     = '\estudio\'
$DataDir      = Join-Path $Root 'data'
$LogDir       = Join-Path $Root 'logs'
$VenvDir      = Join-Path $Root 'venv'
$VenvPython   = Join-Path $VenvDir 'Scripts\python.exe'
$EnvFile      = Join-Path $Root '.env'
$CertDir      = Join-Path $NginxRoot 'certs'
$AcmeDir      = Join-Path $NginxRoot 'acme'

# Well-known SIDs instead of names. WHY: "Administrators" is "Administradores" on a
# Spanish-language image and icacls fails with a bare "no mapping" error that reads
# like a permissions bug. SIDs are identical on every locale.
$SID_SYSTEM   = '*S-1-5-18'
$SID_ADMINS   = '*S-1-5-32-544'
$SID_USERS    = '*S-1-5-32-545'

$script:Warnings = New-Object System.Collections.Generic.List[string]

# ----------------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------------
function Write-Step  { param([string]$m) Write-Host ""; Write-Host "== $m" -ForegroundColor Cyan }
function Write-Info  { param([string]$m) Write-Host "   $m" }
function Write-Ok    { param([string]$m) Write-Host "   [ok]   $m" -ForegroundColor Green }
function Write-Warn2 { param([string]$m) Write-Host "   [warn] $m" -ForegroundColor Yellow; $script:Warnings.Add($m) | Out-Null }
function Write-Die {
    param([string]$Problem, [string[]]$Fix)
    Write-Host ""
    Write-Host "FAILED: $Problem" -ForegroundColor Red
    if ($Fix) { Write-Host ""; Write-Host "What to do:" -ForegroundColor Red; foreach ($f in $Fix) { Write-Host "  - $f" } }
    Write-Host ""
    exit 1
}

function Invoke-Native {
    param(
        [Parameter(Mandatory)][string]$File,
        [string[]]$Arguments = @(),
        [string]$WorkDir,
        [switch]$AllowFail
    )
    # The child's stdout goes STRAIGHT to the host and is deliberately NOT part of this
    # function's return value. WHY: "return $code" after an unpiped "& $File" returns
    # [every line of stdout] + [exit code] as an array, so at the call site
    # "if ($code -eq 0)" evaluates the one-element array @(0), which PowerShell unwraps
    # to 0 -> $false. That reports every SUCCESSFUL pip install and seed script as a
    # failure, and can score a real nginx -t failure as a pass.
    $code = $null
    $prev = $null
    if ($WorkDir) { $prev = (Get-Location).Path; Set-Location $WorkDir }
    try {
        & $File @Arguments | Out-Host
        $code = $LASTEXITCODE
    } finally {
        if ($prev) { Set-Location $prev }
    }
    if ($null -eq $code) { $code = 1 }   # the executable could not be launched at all
    if ($code -ne 0 -and -not $AllowFail) {
        throw "$File exited with code $code"
    }
    return [int]$code
}

# 32 random bytes, base64url, no padding. base64url matters for ACCESS_TOKEN because it
# travels in a path segment (https://domain/e/TOKEN); '+' and '/' would need escaping and
# the '=' padding gets eaten by some clients.
function New-UrlSafeSecret {
    param([int]$ByteCount = 32)
    $bytes = New-Object byte[] $ByteCount
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    return ([Convert]::ToBase64String($bytes)).TrimEnd('=').Replace('+', '-').Replace('/', '_')
}

function New-AccountPassword {
    $bytes = New-Object byte[] 30
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try { $rng.GetBytes($bytes) } finally { $rng.Dispose() }
    # Mixed classes guaranteed by the trailing literals, in case a password policy demands them.
    return ([Convert]::ToBase64String($bytes)) + 'aZ9!'
}

# UTF-8 with NO byte order mark. WHY: python-dotenv reads the first key literally, so a BOM
# turns SECRET_KEY into "\ufeffSECRET_KEY" and the app starts with a missing-key error that
# points at the wrong thing. nginx also refuses a BOM'd nginx.conf on some builds.
function Write-TextFileNoBom {
    param([string]$Path, [string]$Content)
    $enc = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $enc)
}

function Get-AccountSid {
    param([string]$Name)
    $nt = New-Object System.Security.Principal.NTAccount("$env:COMPUTERNAME\$Name")
    return $nt.Translate([System.Security.Principal.SecurityIdentifier]).Value
}

# ==================================================================================
# 1. PREFLIGHT
# ==================================================================================
Write-Step "Preflight"

$id = [System.Security.Principal.WindowsIdentity]::GetCurrent()
if (-not (New-Object System.Security.Principal.WindowsPrincipal($id)).IsInRole(
        [System.Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Die "Not running elevated." @(
        "Right-click PowerShell -> Run as administrator, then re-run this script.",
        "It needs admin to create local accounts, set ACLs, register startup tasks and open firewall ports."
    )
}
Write-Ok "Elevated."

$os = Get-CimInstance Win32_OperatingSystem
Write-Info "OS: $($os.Caption) $($os.Version) ($($os.OSArchitecture))"
if ($os.OSArchitecture -notmatch '64') {
    Write-Die "This is a 32-bit Windows image." @("Use an x64 image. The wheels this app installs are x64-only.")
}

# --- code present ---------------------------------------------------------------
if (-not (Test-Path (Join-Path $Root 'requirements.txt'))) {
    Write-Die "No requirements.txt at $Root - the application code is not here yet." @(
        "Put the code at $Root first (git clone, or copy the tree), then re-run.",
        "This script deploys code; it does not fetch it."
    )
}
if (-not (Test-Path (Join-Path $Root 'app\main.py'))) {
    Write-Warn2 "$Root\app\main.py not found. The entry point 'app.main:app' will fail unless the module lives somewhere on sys.path under $Root."
}
Write-Ok "Code found at $Root"

# --- Python 3.11 -----------------------------------------------------------------
function Get-PythonVersionTag {
    param([string]$Exe)
    try {
        $v = & $Exe -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0) { return ($v | Select-Object -First 1).Trim() }
    } catch { }
    return $null
}

function Find-Python311 {
    $candidates = New-Object System.Collections.Generic.List[string]

    # The py launcher is the only reliable way to ask for a specific minor version.
    $py = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($py) {
        try {
            $out = & $py.Source -3.11 -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $out) { $candidates.Add(($out | Select-Object -First 1).Trim()) }
        } catch { }
    }
    $candidates.Add('C:\Program Files\Python311\python.exe')
    $candidates.Add('C:\Python311\python.exe')
    if ($env:LOCALAPPDATA) { $candidates.Add((Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe')) }
    $onPath = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($onPath) { $candidates.Add($onPath.Source) }

    foreach ($c in $candidates) {
        if ($c -and (Test-Path $c) -and (Get-PythonVersionTag $c) -eq '3.11') { return $c }
    }
    return $null
}

function Get-InstallerPolicyFindings {
    # Not exhaustive, but these three cover essentially every "the installer just does
    # nothing / says an administrator has blocked this" case on a locked-down VPS image.
    $found = New-Object System.Collections.Generic.List[string]
    $msi = Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Installer' -ErrorAction SilentlyContinue
    if ($msi) {
        if ($msi.PSObject.Properties['DisableMSI'] -and $msi.DisableMSI -ne 0) {
            $found.Add("Group Policy: Windows Installer disabled (DisableMSI = $($msi.DisableMSI)).")
        }
        if ($msi.PSObject.Properties['DisableUserInstalls'] -and $msi.DisableUserInstalls -ne 0) {
            $found.Add("Group Policy: per-user installs disabled (DisableUserInstalls = $($msi.DisableUserInstalls)).")
        }
    }
    $srp = Get-ItemProperty 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\Safer\CodeIdentifiers' -ErrorAction SilentlyContinue
    if ($srp -and $srp.PSObject.Properties['DefaultLevel'] -and $srp.DefaultLevel -eq 0) {
        $found.Add("Software Restriction Policy is in default-deny mode (only allow-listed paths may execute).")
    }
    if (Test-Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\SrpV2') {
        $found.Add("AppLocker rules are present (HKLM\SOFTWARE\Policies\Microsoft\Windows\SrpV2).")
    }
    return $found
}

$Python311 = Find-Python311
if (-not $Python311) {
    $policy = Get-InstallerPolicyFindings
    $fix = New-Object System.Collections.Generic.List[string]
    if ($policy.Count -gt 0) {
        $fix.Add("This image restricts installers:")
        foreach ($p in $policy) { $fix.Add("    * $p") }
        $fix.Add("Under those policies the python.org installer may silently do nothing. Options:")
        $fix.Add("    a) Ask whoever owns the policy to allow it, or run the installer from an allow-listed path.")
        $fix.Add("    b) Use a portable Python 3.11: extract the nuget package 'python' (version 3.11.*) from")
        $fix.Add("       nuget.org, then run '<extracted>\tools\python.exe -m ensurepip' and re-run this script")
        $fix.Add("       with that python first on PATH. VERIFY on first run that 'python -m venv' works there;")
        $fix.Add("       the *embeddable* zip from python.org does NOT support venv and is the wrong choice.")
    } else {
        $fix.Add("No installer-blocking policy detected, so the normal installer should work:")
    }
    $fix.Add("Official installer: https://www.python.org/downloads/windows/ -> 'Windows installer (64-bit)' for 3.11.x")
    $fix.Add("Install it FOR ALL USERS (e.g. /passive InstallAllUsers=1 PrependPath=1). WHY: the service account")
    $fix.Add("  '$AppAccount' cannot see a Python installed into another user's AppData profile, and the app will")
    $fix.Add("  start fine when you test it by hand and then fail at boot.")
    Write-Die "Python 3.11 not found." $fix.ToArray()
}
Write-Ok "Python 3.11 at $Python311"
if ($Python311 -like "$env:LOCALAPPDATA*") {
    Write-Warn2 "Python is installed under your own user profile. The venv will hard-code that path and the service account may not be able to read it. Consider reinstalling for all users."
}

# ==================================================================================
# 2. SERVICE ACCOUNTS
# ==================================================================================
Write-Step "Service accounts"

function Initialize-ServiceAccount {
    # Creates the account if it is missing. Deliberately does NOT touch the password of an
    # account that already exists: the password and the scheduled task's stored credential
    # must be rotated together, so rotation happens immediately before task registration
    # (sections 7 and 8), never here.
    param([string]$Name, [string]$Description)
    $existing = Get-LocalUser -Name $Name -ErrorAction SilentlyContinue
    if ($existing) {
        Enable-LocalUser -Name $Name
        # Re-assert every run: an account whose password expires makes the startup task fail
        # at some future boot with a credential error and no other symptom.
        Set-LocalUser -Name $Name -PasswordNeverExpires $true
        Write-Info "$Name exists."
    } else {
        $pw  = New-AccountPassword
        $sec = ConvertTo-SecureString $pw -AsPlainText -Force
        New-LocalUser -Name $Name -Password $sec -FullName $Name -Description $Description `
                      -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires | Out-Null
        Write-Ok "$Name created."
    }
    # Users membership so ordinary system paths (Python, DLLs) are readable. It is not a hole:
    # inheritance is stripped from $Root below, so Users membership grants nothing there.
    try { Add-LocalGroupMember -SID 'S-1-5-32-545' -Member $Name -ErrorAction Stop } catch { }
}

function Reset-AccountPassword {
    # Call this immediately before registering the matching task, never earlier.
    # WHY: the task stores the password at registration time and we keep the plaintext
    # nowhere, so rotating is the only idempotent option - but a rotation that is not
    # followed by a re-registration leaves a task that looks fine today and then fails at
    # the next reboot with 0x8007052E (logon failure). Rotating the web account early and
    # then skipping the nginx task under -SkipNginx did exactly that: the box came back
    # from a reboot with no nginx and no explanation.
    param([string]$Name)
    $pw  = New-AccountPassword
    $sec = ConvertTo-SecureString $pw -AsPlainText -Force
    Set-LocalUser -Name $Name -Password $sec -PasswordNeverExpires $true
    return $pw
}

Initialize-ServiceAccount -Name $AppAccount -Description 'estudio: runs uvicorn'
Initialize-ServiceAccount -Name $WebAccount -Description 'estudio: runs nginx'

function Grant-BatchLogonRight {
    param([string[]]$Accounts)
    # A scheduled task that runs "whether the user is logged on or not" is a BATCH logon.
    # Without SeBatchLogonRight the task registers fine and then fails at boot with
    # 0x80070534 / "a specified logon session does not exist" - which reads like a password bug.
    $stem = Join-Path $env:TEMP ("secpol_{0}" -f ([guid]::NewGuid().ToString('N')))
    $tmp  = "$stem.inf"
    $new  = "$stem.new.inf"
    # Absolute path for the database too: a bare "/db secedit.sdb" resolves against whatever
    # directory the script happened to be launched from, which may not be writable.
    $db   = "$stem.sdb"
    try {
        Invoke-Native -File 'secedit.exe' -Arguments @('/export','/cfg',$tmp,'/areas','USER_RIGHTS') | Out-Null
        $lines = Get-Content -LiteralPath $tmp
        $sids  = @()
        foreach ($a in $Accounts) { $sids += ('*' + (Get-AccountSid $a)) }

        $done = $false
        $out = foreach ($l in $lines) {
            if ($l -match '^SeBatchLogonRight\s*=') {
                $done = $true
                $current = ($l -split '=', 2)[1].Trim()
                $parts = @()
                if ($current) { $parts = $current -split ',' | ForEach-Object { $_.Trim() } }
                foreach ($s in $sids) { if ($parts -notcontains $s) { $parts += $s } }
                "SeBatchLogonRight = " + ($parts -join ',')
            } else { $l }
        }
        if (-not $done) {
            # The USER_RIGHTS export ends with [Privilege Rights], so appending lands in it.
            $out = $out + ("SeBatchLogonRight = " + ($sids -join ','))
        }
        # secedit .inf files are UTF-16LE. Writing UTF-8 here produces a confusing parse error.
        $out | Set-Content -LiteralPath $new -Encoding Unicode
        Invoke-Native -File 'secedit.exe' -Arguments @('/configure','/db',$db,'/cfg',$new,'/areas','USER_RIGHTS') | Out-Null
        Write-Ok "'Log on as a batch job' granted to: $($Accounts -join ', ')"
    } catch {
        Write-Warn2 "Could not grant 'Log on as a batch job' automatically ($($_.Exception.Message)). Open secpol.msc -> Local Policies -> User Rights Assignment -> 'Log on as a batch job' and add $($Accounts -join ' and ') by hand, or the startup tasks will not run at boot."
    } finally {
        Remove-Item -LiteralPath $tmp,$new,$db -Force -ErrorAction SilentlyContinue
    }
}
Grant-BatchLogonRight -Accounts @($AppAccount, $WebAccount)

# ==================================================================================
# 3. DIRECTORIES AND ACLS
# ==================================================================================
Write-Step "Directories and ACLs"

# data/ holds the SQLite database, the uploaded originals and the generated images.
# If the app expects different subdirectory names it will create them itself; these exist
# so that the ACL below is inherited by whatever gets created later.
$dirs = @(
    $DataDir,
    (Join-Path $DataDir 'uploads'),
    (Join-Path $DataDir 'generated'),
    (Join-Path $DataDir 'tmp'),
    $LogDir
)
foreach ($d in $dirs) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null; Write-Info "created $d" }
}

$appSid = '*' + (Get-AccountSid $AppAccount)
$webSid = '*' + (Get-AccountSid $WebAccount)

# Strip inheritance from the project root and rebuild the ACL explicitly.
# WHY: by default C:\ grants BUILTIN\Users read on everything beneath it. That means the
# nginx account could read .env - i.e. read ACCESS_TOKEN, which IS the login. The whole
# point of a separate web account is that it must not be able to.
Invoke-Native -File 'icacls.exe' -Arguments @($Root, '/inheritance:r', '/q') | Out-Null
Invoke-Native -File 'icacls.exe' -Arguments @(
    $Root,
    '/grant', "${SID_SYSTEM}:(OI)(CI)(F)",
    '/grant', "${SID_ADMINS}:(OI)(CI)(F)",
    '/grant', "${appSid}:(OI)(CI)(RX)",   # read + execute on the code. No write: a compromised
                                          # app process must not be able to rewrite its own code.
    '/q'
) | Out-Null
Write-Ok "$Root : inheritance removed; $AppAccount = read/execute; $WebAccount = no access at all."

# data/ is the one place the app writes.
Invoke-Native -File 'icacls.exe' -Arguments @($DataDir, '/grant', "${appSid}:(OI)(CI)(M)", '/q') | Out-Null
Invoke-Native -File 'icacls.exe' -Arguments @($LogDir,  '/grant', "${appSid}:(OI)(CI)(M)", '/q') | Out-Null
# Modify on the DIRECTORY, not just the .db file. WHY: SQLite creates and deletes sibling
# files (-journal, -wal, -shm) next to the database. Write on the file alone gives you
# "attempt to write a readonly database" at the first commit under WAL.
Write-Ok "$DataDir + $LogDir : $AppAccount = modify (directory-level, required by SQLite)."

# ==================================================================================
# 4. VIRTUALENV AND DEPENDENCIES
# ==================================================================================
Write-Step "Virtualenv and dependencies"

if (Test-Path $VenvPython) {
    $tag = Get-PythonVersionTag $VenvPython
    if ($tag -ne '3.11') {
        Write-Die "Existing venv at $VenvDir is Python $tag, not 3.11." @(
            "Delete $VenvDir and re-run. Nothing in the venv is precious - the data lives in $DataDir."
        )
    }
    Write-Info "venv already present (Python $tag), reusing."
} else {
    Invoke-Native -File $Python311 -Arguments @('-m','venv',$VenvDir) | Out-Null
    Write-Ok "venv created at $VenvDir"
}

Invoke-Native -File $VenvPython -Arguments @('-m','pip','install','--upgrade','pip','--disable-pip-version-check') | Out-Null

$reqMain = Join-Path $Root 'requirements.txt'
Write-Info "installing requirements.txt ..."
try {
    Invoke-Native -File $VenvPython -Arguments @('-m','pip','install','--disable-pip-version-check','-r',$reqMain) | Out-Null
    Write-Ok "requirements.txt installed."
} catch {
    Write-Die "pip failed on requirements.txt." @(
        "If the errors mention connection/timeout: this box has no outbound HTTPS to pypi.org. Check the egress firewall or set HTTPS_PROXY.",
        "If they mention 'Microsoft Visual C++ 14.0 or greater is required': a dependency has no prebuilt wheel for this Python/arch and is trying to compile. Install the Visual Studio Build Tools (C++ workload), or pin a version that ships a win_amd64 wheel.",
        "Full pip output is above."
    )
}

# --- optional CV extras ----------------------------------------------------------
$reqCv = Join-Path $Root 'requirements-cv.txt'
if ($SkipCv) {
    Write-Warn2 "requirements-cv.txt skipped by -SkipCv. The anti-slimming proportion check will NOT run: generated images are not verified against the subject's real proportions, so a model that quietly slims her will go unnoticed. Everything else works."
} elseif (-not (Test-Path $reqCv)) {
    Write-Info "No requirements-cv.txt in this tree; nothing optional to install."
} else {
    Write-Info "installing requirements-cv.txt (OPTIONAL) ..."
    $code = Invoke-Native -File $VenvPython -Arguments @('-m','pip','install','--disable-pip-version-check','-r',$reqCv) -AllowFail
    if ($code -eq 0) {
        Write-Ok "requirements-cv.txt installed; the anti-slimming proportion check is available."
    } else {
        Write-Warn2 "requirements-cv.txt FAILED to install (exit $code). This is not fatal and the deploy continues. Consequence: the anti-slimming proportion check does not run - generated images are not compared against her real proportions. The usual cause on Windows is a CV wheel with no win_amd64 build for Python 3.11. Re-run later with a working wheel to enable the check."
    }
}

# ==================================================================================
# 5. .env  (CREATE ONLY - NEVER OVERWRITE)
# ==================================================================================
Write-Step ".env"

$existingToken = $null
if (Test-Path $EnvFile) {
    # Hard rule. Rewriting .env would roll SECRET_KEY (invalidating her session cookie) and
    # roll ACCESS_TOKEN (invalidating the magic link, which is the entire login), and any
    # rows keyed to the old values become unreachable. Never, not even with a -Force flag.
    $info = Get-Item $EnvFile
    Write-Ok "$EnvFile already exists ($($info.Length) bytes, modified $($info.LastWriteTime)). Left untouched."
    foreach ($line in Get-Content -LiteralPath $EnvFile) {
        if ($line -match '^\s*ACCESS_TOKEN\s*=\s*(.+?)\s*$') {
            $existingToken = $Matches[1].Trim().Trim('"').Trim("'")
        }
    }
    if (-not $existingToken) {
        Write-Warn2 "No ACCESS_TOKEN found in the existing .env, so the magic link cannot be printed. Read the token out of .env yourself; do not regenerate it."
    }
} else {
    $secretKey = New-UrlSafeSecret -ByteCount 32
    $token     = New-UrlSafeSecret -ByteCount 32
    $existingToken = $token
    $envText = @"
# estudio - generated $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss K')
# NEVER regenerate these two values on a live install:
#   SECRET_KEY   rolls every session cookie (she is logged out)
#   ACCESS_TOKEN rolls the magic link (the link she has saved stops working)
SECRET_KEY=$secretKey
ACCESS_TOKEN=$token

# Forward slashes: they are valid on Windows and survive .env parsing without
# backslash-escaping surprises.
DATA_DIR=$($DataDir -replace '\\','/')
BASE_URL=https://$Domain

# VERIFY on first run that these key names match what app/config reads. If the app uses
# different names, add them here - do not rename the two secrets above once the app has run.
"@
    Write-TextFileNoBom -Path $EnvFile -Content $envText
    Write-Ok ".env created with cryptographically generated secrets."
}

# Re-assert the .env ACL on every run: cheap, and it repairs a file that was restored from a
# backup or copied in by hand with inherited (Users-readable) permissions still attached.
Invoke-Native -File 'icacls.exe' -Arguments @(
    $EnvFile, '/inheritance:r',
    '/grant', "${SID_SYSTEM}:(R)",
    '/grant', "${SID_ADMINS}:(F)",
    '/grant', "${appSid}:(R)",
    '/q'
) | Out-Null
Write-Ok "$EnvFile readable only by SYSTEM, Administrators and $AppAccount."

# ==================================================================================
# 6. SEED SCRIPTS
# ==================================================================================
Write-Step "Seed scripts"

function Find-ScriptFile {
    param([string]$Leaf)
    $hit = Get-ChildItem -LiteralPath $Root -Filter $Leaf -Recurse -File -ErrorAction SilentlyContinue |
           Where-Object { $_.FullName -notlike "$VenvDir*" } |
           Select-Object -First 1
    if ($hit) { return $hit.FullName }
    return $null
}

if ($SkipSeed) {
    Write-Info "Seed scripts skipped by -SkipSeed."
} else {
    foreach ($leaf in @('make_icons.py','seed_catalog.py')) {
        $path = Find-ScriptFile $leaf
        if (-not $path) {
            Write-Warn2 "$leaf not found anywhere under $Root - skipped. If it lives elsewhere, run it by hand with $VenvPython."
            continue
        }
        Write-Info "running $path ..."
        $env:PYTHONPATH = $Root   # so 'import app...' resolves regardless of where the script sits
        $code = Invoke-Native -File $VenvPython -Arguments @($path) -WorkDir $Root -AllowFail
        Remove-Item Env:\PYTHONPATH -ErrorAction SilentlyContinue
        if ($code -eq 0) { Write-Ok "$leaf completed." }
        else { Write-Warn2 "$leaf exited with code $code. Deploy continues; check the output above." }
    }
    Write-Info "Note: these run on EVERY deploy. If seed_catalog is not idempotent it will duplicate"
    Write-Info "rows on the update path - verify that once, and pass -SkipSeed on later runs if it is not."
}

# ==================================================================================
# 7. APP STARTUP TASK
# ==================================================================================
Write-Step "uvicorn startup task"

# A .cmd wrapper rather than calling python.exe directly from the task. WHY: a scheduled
# task discards stdout/stderr, so a Python traceback at startup vanishes and all you see is
# "the site is down". The redirect below is the only startup log you will get.
$runCmdPath = Join-Path $Root 'run-app.cmd'
$runCmd = @"
@echo off
rem Generated by deploy.ps1. Started at boot by scheduled task "$AppTaskName".
cd /d "$Root"
echo. >> "$LogDir\app.log"
echo ==== started %DATE% %TIME% ==== >> "$LogDir\app.log"
rem --proxy-headers makes uvicorn honour X-Forwarded-Proto (that is what decides whether the
rem session cookie is marked Secure); --forwarded-allow-ips restricts that trust to nginx on
rem loopback, so nobody can forge the header by reaching port 8000 some other way.
"$VenvPython" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1 >> "$LogDir\app.log" 2>&1
"@
Write-TextFileNoBom -Path $runCmdPath -Content $runCmd
Write-Ok "wrote $runCmdPath  (log: $LogDir\app.log - it is not rotated, check its size occasionally)"

function Register-StartupTask {
    param(
        [string]$Name, [string]$Program, [string]$Arguments, [string]$WorkDir,
        [string]$Account, [string]$Password, [string]$Description
    )
    $action = if ($Arguments) {
        New-ScheduledTaskAction -Execute $Program -Argument $Arguments -WorkingDirectory $WorkDir
    } else {
        New-ScheduledTaskAction -Execute $Program -WorkingDirectory $WorkDir
    }
    $trigger = New-ScheduledTaskTrigger -AtStartup
    # A short boot delay: at t=0 the network stack is not always ready and a listener that
    # cannot bind simply exits. Restart-on-failure would cover it a minute later; the delay
    # avoids the minute.
    try { $trigger.Delay = 'PT20S' } catch { }
    $settings = New-ScheduledTaskSettingsSet `
        -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -StartWhenAvailable `
        -MultipleInstances IgnoreNew `
        -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -ExecutionTimeLimit ([TimeSpan]::Zero)
    # ExecutionTimeLimit Zero = no limit. WHY: the default is 3 days, after which the Task
    # Scheduler kills a perfectly healthy server and the site dies for no visible reason.
    # RestartCount/Interval is the crash supervisor: if uvicorn exits nonzero the task is
    # restarted a minute later.

    Unregister-ScheduledTask -TaskName $Name -TaskPath $TaskPath -Confirm:$false -ErrorAction SilentlyContinue
    Register-ScheduledTask -TaskName $Name -TaskPath $TaskPath `
        -Action $action -Trigger $trigger -Settings $settings `
        -User "$env:COMPUTERNAME\$Account" -Password $Password -RunLevel Limited `
        -Description $Description | Out-Null
    Write-Ok "task $TaskPath$Name registered (at startup, as $Account, restarts on crash)."
}

function Stop-AppAndWaitForPort {
    Stop-ScheduledTask -TaskName $AppTaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 2
    # Ending the task kills the run-app.cmd wrapper but frequently leaves its python.exe
    # child running. Then the new uvicorn cannot bind 8000, the /health probe below still
    # gets a 200 - from the OLD code - and the deploy looks like it worked while nothing
    # actually changed. Only processes that ARE this venv's interpreter are touched.
    $stale = @(Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
               Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -ieq $VenvPython) })
    foreach ($p in $stale) {
        Write-Info "stopping leftover uvicorn (pid $($p.ProcessId))"
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    for ($i = 0; $i -lt 15; $i++) {
        $busy = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue
        if (-not $busy) { return }
        Start-Sleep -Seconds 1
    }
    Write-Warn2 "Something is still listening on 127.0.0.1:8000 after stopping $AppTaskName. The new uvicorn will fail to bind and /health may be answered by the old process. Find the owner with: Get-NetTCPConnection -LocalPort 8000 -State Listen | Select-Object OwningProcess"
}

# Rotate the password and register the task in the same breath - see Reset-AccountPassword.
$AppPassword = Reset-AccountPassword -Name $AppAccount
Register-StartupTask -Name $AppTaskName -Program $runCmdPath -Arguments '' -WorkDir $Root `
    -Account $AppAccount -Password $AppPassword -Description 'estudio FastAPI/uvicorn'

Stop-AppAndWaitForPort
Start-ScheduledTask -TaskName $AppTaskName -TaskPath $TaskPath

Write-Info "waiting for /health ..."
$healthy = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 2
    try {
        $r = Invoke-WebRequest -Uri 'http://127.0.0.1:8000/health' -UseBasicParsing -TimeoutSec 5
        if ($r.StatusCode -eq 200) { $healthy = $true; break }
    } catch { }
}
if ($healthy) { Write-Ok "app answering on http://127.0.0.1:8000/health" }
else {
    Write-Warn2 "App did not answer /health within 60s. Read $LogDir\app.log - the traceback is there. Common causes: a config key missing from .env, or $AppAccount cannot read $Python311."
}

# ==================================================================================
# 8. NGINX
# ==================================================================================
if ($SkipNginx) {
    Write-Step "nginx (skipped by -SkipNginx)"
    Write-Info "$WebAccount's password and the '$WebTaskName' task were both left untouched, so the"
    Write-Info "running nginx and its stored boot credential still match."
} else {
Write-Step "nginx"

$nginxExe = Join-Path $NginxRoot 'nginx.exe'
if (-not (Test-Path $nginxExe)) {
    if ($NginxZip -and (Test-Path $NginxZip)) {
        $staging = Join-Path $env:TEMP ('nginxzip_' + [guid]::NewGuid().ToString('N'))
        Expand-Archive -LiteralPath $NginxZip -DestinationPath $staging -Force
        $inner = Get-ChildItem -LiteralPath $staging -Directory | Select-Object -First 1
        if (-not $inner) { Write-Die "That zip does not look like the nginx distribution (no nginx-X.Y.Z folder inside)." @() }
        New-Item -ItemType Directory -Path $NginxRoot -Force | Out-Null
        Copy-Item -Path (Join-Path $inner.FullName '*') -Destination $NginxRoot -Recurse -Force
        Remove-Item -LiteralPath $staging -Recurse -Force -ErrorAction SilentlyContinue
        Write-Ok "nginx unpacked to $NginxRoot"
    } else {
        Write-Die "nginx not found at $nginxExe." @(
            "Download the official Windows build (a zip, listed as 'nginx/Windows-X.Y.Z') from http://nginx.org/en/download.html",
            "Then re-run:  .\deploy.ps1 -Domain $Domain -NginxZip C:\path\to\nginx-X.Y.Z.zip",
            "This script does not download it, because hard-coding a version URL guarantees a dead link later."
        )
    }
}

foreach ($d in @($CertDir, $AcmeDir, (Join-Path $NginxRoot 'logs'), (Join-Path $NginxRoot 'temp'))) {
    if (-not (Test-Path $d)) { New-Item -ItemType Directory -Path $d -Force | Out-Null }
}

# nginx reads its config and certs as $WebAccount, and writes logs, the pid file, and the
# request-body temp files (a 30 MB phone photo is spooled to disk before it is proxied).
# The Windows build is compiled with its temp paths under temp/, which is why that
# directory ships in the zip and gets modify below.
Invoke-Native -File 'icacls.exe' -Arguments @($NginxRoot, '/grant', "${webSid}:(OI)(CI)(RX)", '/q') | Out-Null
foreach ($d in @((Join-Path $NginxRoot 'logs'), (Join-Path $NginxRoot 'temp'))) {
    Invoke-Native -File 'icacls.exe' -Arguments @($d, '/grant', "${webSid}:(OI)(CI)(M)", '/q') | Out-Null
}
# NOTE the ${} around the account name: "$WebAccount:" inside a double-quoted string is
# parsed as a drive-qualified variable reference and is a FATAL PARSE ERROR for the whole
# script - nothing runs at all, not one account, not one file.
Write-Ok "nginx ACLs set (${WebAccount}: read $NginxRoot, write logs/ and temp/). Note it has no access to $Root at all."

# --- shared security headers -----------------------------------------------------
# In a separate file that gets include'd. WHY: nginx's add_header does not accumulate across
# levels - a single add_header inside a location silently drops every add_header inherited
# from the server block. Including the same file everywhere is the only reliable fix.
$headersConf = @'
# Included at server level AND inside any location that adds its own header.
add_header X-Content-Type-Options nosniff always;
add_header X-Frame-Options DENY always;
# no-referrer: the magic link (https://host/e/TOKEN) is in the URL. Any outbound link or
# third-party asset would otherwise leak the login token in the Referer header.
add_header Referrer-Policy no-referrer always;
add_header Strict-Transport-Security "max-age=31536000" always;
'@
Write-TextFileNoBom -Path (Join-Path $NginxRoot 'conf\estudio-headers.conf') -Content $headersConf

# --- shared proxy headers --------------------------------------------------------
# EXACTLY the same trap as add_header, and much more expensive here: proxy_set_header is
# inherited from the enclosing level ONLY if the current level defines none of its own.
# One proxy_set_header inside a location therefore drops Host, X-Real-IP, X-Forwarded-For
# AND X-Forwarded-Proto, replacing them with nginx's defaults (Host: <upstream name>,
# Connection: close). Losing X-Forwarded-Proto is the Secure-cookie failure, and losing it
# only on /events/ is the version of it nobody notices.
# So: no location sets a proxy header inline; every proxying location includes this file.
$proxyConf = @'
proxy_http_version 1.1;
# Clear the hop-by-hop Connection header so it is not forwarded as "close".
proxy_set_header Connection        "";
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
# THE header. The app marks its session cookie Secure based on this. If it is missing or
# wrong, either the cookie is marked Secure over a request the app believes is http and the
# browser drops it (she can never log in), or the session is treated as safe on a downgraded
# request. $scheme is safe here because the port 80 server never proxies anything - it only
# redirects - so this can never evaluate to "http".
proxy_set_header X-Forwarded-Proto $scheme;
'@
Write-TextFileNoBom -Path (Join-Path $NginxRoot 'conf\estudio-proxy.conf') -Content $proxyConf

# --- main config -----------------------------------------------------------------
# Single-quoted here-string: nginx configs are full of $variables that PowerShell would
# otherwise expand. __DOMAIN__ is substituted afterwards.
$nginxConfTemplate = @'
# Generated by deploy.ps1 - re-run the script rather than hand-editing.
# Paths use forward slashes: the Windows build accepts them and backslashes are read as
# escape characters inside quoted strings.

# One worker. The official Windows build uses select() and its workers do not share the
# listening socket the way they do on Linux; extra workers add contention, not capacity.
# For a single-user tool this is plenty.
worker_processes  1;

error_log  logs/error.log warn;
pid        logs/nginx.pid;

events {
    # select() on Windows tops out near 1024 handles. Every open /events/ stream occupies
    # one for the full 60-180s batch, so this is the ceiling on concurrent SSE clients.
    worker_connections  1024;
}

http {
    include       mime.types;
    default_type  application/octet-stream;
    server_tokens off;

    access_log  logs/access.log;

    # sendfile is unreliable on the Windows build and nginx here only proxies - nothing to gain.
    sendfile     off;
    # SSE frames are tiny; Nagle would sit on them for up to 200ms each.
    tcp_nodelay  on;

    # gzip OFF globally. gzip fills a compression buffer before flushing, which is exactly
    # the failure mode SSE cannot tolerate: events arrive in a clump minutes late, or not at
    # all. There is one user and no bandwidth problem worth risking that for.
    gzip off;

    # 32m against the app's own 30 MB limit. Slightly higher on purpose: at exactly 30m an
    # oversized upload gets nginx's bare 413 HTML page instead of the app's error, which the
    # front end cannot parse and cannot explain to her.
    client_max_body_size 32m;
    # Phone uploads over mobile data are slow; the default 60s kills a large HEIC mid-flight.
    client_body_timeout  300s;

    # Defaults for everything that proxies. Every location repeats this include, because a
    # location that sets even one proxy_set_header of its own would otherwise lose all of
    # these - X-Forwarded-Proto included.
    include estudio-proxy.conf;

    # uvicorn is bound to 127.0.0.1 and only trusts forwarded headers from 127.0.0.1,
    # so this must stay an explicit loopback address (not "localhost", which may resolve to ::1).
    upstream estudio_app {
        server 127.0.0.1:8000;
    }

    # No proxy_cache_path is defined anywhere in this file, so nginx caches nothing on disk.
    # That is deliberate: /media/ is private photographs of an identifiable person and must
    # not accumulate outside the app's own data directory.

    # Anything arriving on port 80 with a Host we do not serve gets no response at all.
    server {
        listen 80 default_server;
        server_name _;
        return 444;
    }

    server {
        listen 80;
        server_name __DOMAIN__;

        # The only thing served over plaintext, so certificate renewal can work.
        # This directory contains challenge files and nothing else.
        location ^~ /.well-known/acme-challenge/ {
            root      C:/nginx/acme;
            autoindex off;
        }

        location / {
            return 301 https://$host$request_uri;
        }
    }

    server {
        listen 443 ssl;
        server_name __DOMAIN__;

        ssl_certificate     C:/nginx/certs/fullchain.pem;
        ssl_certificate_key C:/nginx/certs/privkey.pem;
        ssl_protocols       TLSv1.2 TLSv1.3;
        ssl_prefer_server_ciphers off;
        ssl_session_cache   shared:SSL:2m;
        ssl_session_timeout 1h;

        include estudio-headers.conf;

        # Never. Not in any location. There is no directory being served from disk here at
        # all, but state it once so no future edit accidentally introduces one.
        autoindex off;

        # ---- Server-Sent Events -------------------------------------------------
        # This block is the one that is always wrong somewhere else. Every directive here
        # exists because of a specific way SSE dies behind a reverse proxy.
        location ^~ /events/ {
            proxy_pass http://estudio_app;
            # Not optional: see the header of estudio-proxy.conf. Without this include the
            # SSE route loses X-Forwarded-Proto and the Secure-cookie contract with it.
            include estudio-proxy.conf;
            include estudio-headers.conf;

            # Without this nginx accumulates the response in a buffer and forwards it when
            # the buffer fills or the response ends. The stream ends after 60-180s, so she
            # watches a dead progress bar and then everything appears at once. This single
            # line is the difference between live previews and a frozen page.
            proxy_buffering off;
            # Same idea one layer down: do not spool the response to a temp file.
            proxy_request_buffering off;

            # The stream is idle between image completions. The default 60s read timeout
            # closes the connection mid-batch and the browser reconnects in a loop, which
            # can restart work. 1h comfortably exceeds the longest batch.
            proxy_read_timeout 3600s;
            proxy_send_timeout 3600s;
            # The client-facing counterpart of those two; its default is 60s.
            send_timeout       3600s;

            # Explicit here in case gzip is ever turned on globally by a later edit.
            gzip off;

            # NOTE: X-Accel-Buffering is a RESPONSE header nginx reads FROM the upstream.
            # Sending it TO the app as a request header (a common cargo-cult line) does
            # nothing at all - and, worse, one such proxy_set_header here would wipe every
            # inherited proxy header. If you want belt and braces, have the SSE endpoint
            # emit "X-Accel-Buffering: no" on its own response.
        }

        # ---- the magic link -----------------------------------------------------
        # access_log off because the whole login is in the path: one ordinary access-log
        # line is a permanent plaintext copy of the credential, in a file the low-privilege
        # web account can write and anything with read access can lift.
        location ^~ /e/ {
            proxy_pass http://estudio_app;
            include estudio-proxy.conf;
            access_log off;
        }

        # ---- service worker -----------------------------------------------------
        location = /sw.js {
            proxy_pass http://estudio_app;
            include estudio-proxy.conf;
            # Drop whatever cache header came from upstream and force revalidation. A service
            # worker that gets cached pins the old UI on her phone permanently - there is no
            # way to push a fix past it short of clearing site data.
            proxy_hide_header Cache-Control;
            add_header Cache-Control "no-cache" always;
            include estudio-headers.conf;
        }

        # ---- uploads ------------------------------------------------------------
        location = /upload {
            proxy_pass http://estudio_app;
            include estudio-proxy.conf;
            # Buffer the body to disk before handing it over: a 30 MB photo trickling in over
            # mobile data would otherwise hold a Python worker open for the whole upload.
            proxy_request_buffering on;
            proxy_read_timeout 300s;
            proxy_send_timeout 300s;
        }

        # ---- private images -----------------------------------------------------
        # Proxied to the app, never served from disk. The app checks the auth cookie; nginx
        # cannot. A "root" pointing at the image directory here would make every photograph
        # public to anyone who guesses a URL.
        location ^~ /media/ {
            proxy_pass http://estudio_app;
            include estudio-proxy.conf;
        }

        # /static/ is also proxied rather than served by nginx. Serving it directly would
        # require pointing a root at the code tree, and the web account deliberately has no
        # access to C:\estudio. One user does not need the microseconds.
        location / {
            proxy_pass http://estudio_app;
            include estudio-proxy.conf;
            proxy_read_timeout 120s;
        }
    }
}
'@

$nginxConf = $nginxConfTemplate.Replace('__DOMAIN__', $Domain).Replace('C:/nginx', ($NginxRoot -replace '\\','/'))
$nginxConfPath = Join-Path $NginxRoot 'conf\nginx.conf'
if (Test-Path $nginxConfPath) {
    $backup = Join-Path $NginxRoot ("conf\nginx.conf.bak-{0}" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
    Copy-Item -LiteralPath $nginxConfPath -Destination $backup -Force
    Write-Info "previous nginx.conf backed up to $backup"
}
Write-TextFileNoBom -Path $nginxConfPath -Content $nginxConf
Write-Ok "wrote $nginxConfPath"

# --- TLS material ----------------------------------------------------------------
$certFile = Join-Path $CertDir 'fullchain.pem'
$keyFile  = Join-Path $CertDir 'privkey.pem'
$certOk = (Test-Path $certFile) -and (Test-Path $keyFile)
if ($certOk) {
    Invoke-Native -File 'icacls.exe' -Arguments @($CertDir, '/grant', "${webSid}:(OI)(CI)(R)", '/q') | Out-Null
    Write-Ok "certificate present; $WebAccount granted read."
} else {
    Write-Warn2 "No certificate at $certFile + $keyFile, so nginx will not start yet. There is no certbot for nginx on Windows and this build does not do automatic TLS. Obtain a certificate for $Domain (win-acme / wacs.exe is the usual tool on Windows - VERIFY its current flags against its own docs), have it write the full chain and the private key in PEM form to those two paths, then run: Start-ScheduledTask -TaskName $WebTaskName -TaskPath $TaskPath"
}

# --- certificate renewal -> nginx restart ----------------------------------------
# nginx reads the certificate ONCE, at startup. An ACME client that renews the PEM files
# every 60 days changes nothing about the running process: nginx keeps serving the old
# certificate until it expires, and the site dies roughly 90 days after deployment with a
# green renewal log and no other warning. This watcher closes that gap: it hashes the
# certificate daily and restarts nginx only when the bytes actually change.
# ("nginx -s reload" is not used: the signal travels through a named object owned by the
# account that started nginx, so it does not reliably reach a master running as $WebAccount.)
$certReloadTemplate = @'
# Generated by deploy.ps1. Runs daily as SYSTEM. Restarts nginx when the certificate changes.
$ErrorActionPreference = 'SilentlyContinue'
$cert  = '__CERT__'
$stamp = '__STAMP__'
$log   = '__LOG__'
if (-not (Test-Path -LiteralPath $cert)) { exit 0 }
$sig = (Get-FileHash -LiteralPath $cert -Algorithm SHA256).Hash
$old = ''
if (Test-Path -LiteralPath $stamp) { $old = ((Get-Content -LiteralPath $stamp -Raw) + '').Trim() }
if ($sig -eq $old) { exit 0 }
Add-Content -LiteralPath $log -Value ("{0} certificate changed on disk; restarting nginx" -f (Get-Date -Format 's'))
Stop-ScheduledTask -TaskName '__TASK__' -TaskPath '__TASKPATH__'
Start-Sleep -Seconds 2
& taskkill.exe /IM nginx.exe /F | Out-Null
Start-Sleep -Seconds 1
Start-ScheduledTask -TaskName '__TASK__' -TaskPath '__TASKPATH__'
Start-Sleep -Seconds 5
if (Get-Process -Name nginx) {
    # Stamp only on success, so a failed restart is retried tomorrow instead of forgotten.
    Set-Content -LiteralPath $stamp -Value $sig
    Add-Content -LiteralPath $log -Value ("{0} nginx is back up on the new certificate" -f (Get-Date -Format 's'))
} else {
    Add-Content -LiteralPath $log -Value ("{0} nginx did NOT come back - check the nginx error log" -f (Get-Date -Format 's'))
}
'@
$certReloadPath = Join-Path $NginxRoot 'estudio-cert-reload.ps1'
$certReload = $certReloadTemplate.
    Replace('__CERT__',     $certFile).
    Replace('__STAMP__',    (Join-Path $NginxRoot 'estudio-cert.stamp')).
    Replace('__LOG__',      (Join-Path $LogDir 'cert-reload.log')).
    Replace('__TASK__',     $WebTaskName).
    Replace('__TASKPATH__', $TaskPath)
Write-TextFileNoBom -Path $certReloadPath -Content $certReload

$certAction = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument ("-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$certReloadPath`"")
$certTrigger  = New-ScheduledTaskTrigger -Daily -At (Get-Date -Hour 3 -Minute 20 -Second 0)
$certSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Unregister-ScheduledTask -TaskName $CertTaskName -TaskPath $TaskPath -Confirm:$false -ErrorAction SilentlyContinue
Register-ScheduledTask -TaskName $CertTaskName -TaskPath $TaskPath `
    -Action $certAction -Trigger $certTrigger -Settings $certSettings `
    -User 'SYSTEM' -RunLevel Highest `
    -Description 'estudio: restart nginx after the TLS certificate is renewed' | Out-Null
Write-Ok "task $TaskPath$CertTaskName registered (daily 03:20; restarts nginx only when $certFile changes)."

# --- syntax check ----------------------------------------------------------------
Write-Info "nginx -t ..."
$nginxPrefix = ($NginxRoot -replace '\\','/') + '/'
$testCode = Invoke-Native -File $nginxExe -Arguments @('-t','-p',$nginxPrefix,'-c','conf/nginx.conf') -WorkDir $NginxRoot -AllowFail
if ($testCode -ne 0) {
    if (-not $certOk) {
        Write-Warn2 "nginx -t failed, which is expected while the certificate files are missing. Re-run 'nginx -t' after installing the certificate."
    } else {
        Write-Warn2 "nginx -t failed - see the output above. If it complains about 'ssl_protocols TLSv1.3', the OpenSSL in this Windows build predates TLS 1.3: remove TLSv1.3 from $nginxConfPath and re-test."
    }
} else {
    Write-Ok "nginx configuration syntax is valid."
}

# --- nginx startup task ----------------------------------------------------------
# nginx for Windows is a console application: there is no built-in service mode. A startup
# scheduled task is the boring way to get it back after a reboot with nobody logged in.
# (NSSM is the common alternative; it is a third-party download, so this script does not
# assume it.) Binding 80/443 needs no privilege on Windows - unlike Linux, there is no
# reserved-port rule - so nginx runs as an ordinary account here.
$WebPassword = Reset-AccountPassword -Name $WebAccount
Register-StartupTask -Name $WebTaskName -Program $nginxExe `
    -Arguments ("-p `"$nginxPrefix`" -c conf/nginx.conf") -WorkDir $NginxRoot `
    -Account $WebAccount -Password $WebPassword -Description 'estudio nginx (Windows console build)'

# Restart cleanly. Stopping the task and killing any stragglers is predictable; a reload
# signal from this admin console may not reach a master running as $WebAccount.
Stop-ScheduledTask -TaskName $WebTaskName -TaskPath $TaskPath -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1
Start-Process -FilePath 'taskkill.exe' -ArgumentList '/IM','nginx.exe','/F' -NoNewWindow -Wait -ErrorAction SilentlyContinue
if ($certOk -and $testCode -eq 0) {
    Start-ScheduledTask -TaskName $WebTaskName -TaskPath $TaskPath
    Start-Sleep -Seconds 3
    if (Get-Process -Name nginx -ErrorAction SilentlyContinue) { Write-Ok "nginx running." }
    else { Write-Warn2 "nginx did not stay up. Check $NginxRoot\logs\error.log and the task's Last Run Result in taskschd.msc." }
} else {
    Write-Info "nginx task registered but not started (waiting on the certificate / a clean nginx -t)."
}

# --- firewall --------------------------------------------------------------------
foreach ($p in @(80, 443)) {
    $ruleName = "estudio inbound $p"
    if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
        New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Action Allow `
            -Protocol TCP -LocalPort $p -Profile Any | Out-Null
        Write-Ok "firewall: opened TCP $p"
    } else {
        Write-Info "firewall: rule '$ruleName' already present"
    }
}
# Port 8000 is never opened. uvicorn binds 127.0.0.1 only, so it is unreachable from the
# network - which is also what makes --forwarded-allow-ips 127.0.0.1 meaningful.
}

# ==================================================================================
# 9. SUMMARY
# ==================================================================================
Write-Host ""
Write-Host "-------------------------------------------------------------------" -ForegroundColor Cyan
Write-Host " Deploy finished" -ForegroundColor Cyan
Write-Host "-------------------------------------------------------------------" -ForegroundColor Cyan

if ($script:Warnings.Count -gt 0) {
    Write-Host ""
    Write-Host "Warnings ($($script:Warnings.Count)):" -ForegroundColor Yellow
    foreach ($w in $script:Warnings) { Write-Host "  - $w" -ForegroundColor Yellow }
}

Write-Host ""
Write-Host "Verify by hand on the first run (these cannot be checked from here):" -ForegroundColor Cyan
Write-Host "  1. SSE is not buffered. From another machine, with a valid session cookie:"
Write-Host "       curl.exe -N -H `"Cookie: <session cookie>`" https://$Domain/events/<session_id>"
Write-Host "     Tokens must appear as they are produced. If nothing arrives until the batch"
Write-Host "     ends, buffering is on somewhere - check proxy_buffering in $NginxRoot\conf\nginx.conf"
Write-Host "     and that the app does not buffer its own generator."
Write-Host "  2. Cookie flags: log in over https and confirm the session cookie is marked Secure."
Write-Host "     If it is not, X-Forwarded-Proto is not reaching uvicorn. Every proxying location"
Write-Host "     must 'include estudio-proxy.conf;' - nginx drops ALL inherited proxy headers in"
Write-Host "     any location that defines one of its own."
Write-Host "  3. Upload a HEIC photo from the phone. HEIC decoding depends on a package in"
Write-Host "     requirements.txt; a failure here shows up as a broken preview, not an install error."
Write-Host "  4. Reboot the server and confirm the tasks come back with nobody logged in:"
Write-Host "       Get-ScheduledTask -TaskPath '$TaskPath' | Get-ScheduledTaskInfo"
Write-Host "  5. Certificate renewal: point your ACME client at $CertDir (fullchain.pem +"
Write-Host "     privkey.pem) and at the webroot $AcmeDir for the http-01 challenge."
Write-Host "     nginx only reads the certificate at startup, so the task '$CertTaskName'"
Write-Host "     restarts it within 24h of those files changing. Test that once: overwrite the"
Write-Host "     PEM, run the task by hand, and read $LogDir\cert-reload.log."
Write-Host ""
Write-Host "Logs:  $LogDir\app.log   $LogDir\cert-reload.log   $NginxRoot\logs\error.log"
Write-Host "Restart the app:  Stop-ScheduledTask -TaskName $AppTaskName -TaskPath '$TaskPath'; Start-ScheduledTask -TaskName $AppTaskName -TaskPath '$TaskPath'"
Write-Host ""

if ($existingToken) {
    Write-Host "-------------------------------------------------------------------" -ForegroundColor Magenta
    Write-Host " PRIVATE MAGIC LINK - this URL is the entire login. Do not paste it" -ForegroundColor Magenta
    Write-Host " anywhere it can be logged, indexed or forwarded." -ForegroundColor Magenta
    Write-Host "-------------------------------------------------------------------" -ForegroundColor Magenta
    Write-Host ""
    Write-Host "   https://$Domain/e/$existingToken" -ForegroundColor White
    Write-Host ""
} else {
    Write-Host "ACCESS_TOKEN could not be read from $EnvFile, so no link is printed." -ForegroundColor Yellow
    Write-Host "Read it out of that file directly. Do NOT regenerate it on a live install." -ForegroundColor Yellow
    Write-Host ""
}

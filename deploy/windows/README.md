# Deploying estudio on Windows Server

This is the runbook for taking a bare Windows Server VPS to the point where she
opens a link on her phone, uploads a photo, and watches six previews arrive one
at a time over HTTPS.

The finished shape is three pieces:

```
  browser  --HTTPS-->  nginx (C:\nginx, ports 80/443)  --HTTP-->  uvicorn (127.0.0.1:8000)
                              |                                          |
                       Let's Encrypt cert                        C:\estudio  (code)
                       renewed by win-acme                       C:\estudio\data  (SQLite + photos)
                                                                 C:\estudio\.env  (secrets + API keys)
```

Follow the stages in order. Every stage ends with a **VERIFY** block. Do not
move to the next stage until the VERIFY passes — on Windows, almost every
failure in this stack is silent, and a stage that quietly did nothing looks
exactly like a stage that worked until you reboot two weeks later.

You do not need to be a Windows sysadmin. You do need to read the VERIFY blocks.

---

## 0. Before you start

### What this costs

| Item | Cost |
|---|---|
| Windows Server VPS | The Windows licence is bundled into the hourly price, so the same hardware costs noticeably more than a Linux VPS. Check your provider's Windows pricing specifically — it is usually a substantial premium, not a rounding error. |
| Domain name | ~$10–12/year for a real domain. A free subdomain (DuckDNS and similar) works exactly as well technically — `.env.example` ships with `nayane-estudio.duckdns.org` as the example. |
| TLS certificate | Free (Let's Encrypt, via win-acme). |
| `ANTHROPIC_API_KEY` | Pay per use. Without it, photo analysis falls back to pixel heuristics and the visual judge does not run at all. The app still works; it is blinder, and it says so. |
| `FAL_API_KEY` / `REPLICATE_API_TOKEN` | Pay per image. With neither set, the app uses its local mock generator: the whole pipeline runs end to end and produces placeholder cards, not photographs. Good for proving the deployment, useless for delivering work. |
| Your time | Budget 2–3 hours for the first run, most of it waiting on downloads and DNS. |

Sizing: this app decodes and resizes phone photographs, and the optional
computer-vision gate is CPU bound (`CV_CONCURRENCY=2` in `.env.example` assumes
roughly a 4-core box). 2 vCPU / 4 GB RAM is a workable floor; disk needs to hold
the originals, the generated images, the derivatives, *and* your backups.

### What you need in hand before stage 1

- A Windows Server 2022 or 2025 **x64** VPS, with RDP access and a local
  Administrator account.
- A hostname you control (real domain or free subdomain).
- The ability to create a DNS **A record** for that hostname.
- The application code (this repository).
- Your API keys, if you are using real providers.
- An email address for Let's Encrypt expiry warnings.

### The three things that actually go wrong

Read these now, so you recognise them when you see them:

1. **The preview grid freezes for three minutes and then all six images appear
   at once.** nginx is buffering the `/events/` stream. This is the single most
   common mistake in this deployment, and the app looks broken while working
   perfectly. Stage 9 and the troubleshooting section deal with it.
2. **She logs in and is bounced straight back to the login page, forever.**
   `X-Forwarded-Proto` is not reaching uvicorn, so the session cookie is issued
   with the wrong `Secure` flag and the browser drops it. `app/main.py`
   `_is_https()` reads that header and falls back to the request scheme, which
   behind a proxy is always `http`.
3. **She is logged out after every reboot, and anyone with the address gets in.**
   Nothing loaded `.env` into the server process. **This app has no
   `python-dotenv`** — `app/config.py` reads `os.environ` directly (see the
   comment in `requirements.txt`: "pydantic-settings: app/config.py reads
   os.environ directly"). A `.env` file sitting on disk next to the code does
   *nothing* on its own. Something must inject it. That is why stage 7 exists,
   and why it is not optional.

### Which scripts you are going to run

Four files live in this directory. They overlap, and running the wrong two
together will fight over port 443. This runbook picks one route and tells you
what the others are for.

| File | What it does | Used here? |
|---|---|---|
| `install-service.ps1` | Registers uvicorn as an auto-start Windows service via NSSM, **and injects `.env` into the service environment**. Proves `/health` before claiming success. | **Yes — stage 7.** |
| `setup-tls.ps1` | Installs nginx for Windows, obtains a Let's Encrypt certificate with win-acme, registers nginx to start at boot as SYSTEM, and installs a renewal hook that reloads nginx. | **Yes — stage 8.** |
| `nginx.conf` | The reviewed, hardened reverse-proxy config: correct SSE handling, upload limits, security headers, `/media/` never served from disk. | **Yes — stage 9**, with the edits listed there. |
| `deploy.ps1` | An all-in-one alternative: low-privilege service accounts, ACLs, venv, `.env` generation, scheduled tasks, its own generated nginx.conf. | **No — see Appendix A.** It has a real strength (least-privilege accounts) and one gap you must close by hand (it does not inject `.env`). |

---

## 1. The box, the firewalls and DNS

RDP into the server as Administrator and open **PowerShell as Administrator**
(right-click → Run as administrator). Everything below assumes an elevated
prompt.

Point your DNS A record at the server's public IPv4 address. Find it with:

```powershell
(Invoke-RestMethod https://api.ipify.org?format=json).ip
```

Create the record: `estudio.example.com  →  <that IP>`. If you are using a free
subdomain, do it in that provider's control panel.

Open the ports. There are **two** firewalls and people routinely forget the
second one:

```powershell
# 1. Windows Firewall (setup-tls.ps1 also does this in stage 8; harmless to do now)
New-NetFirewallRule -DisplayName "estudio inbound 80"  -Direction Inbound -Action Allow -Protocol TCP -LocalPort 80  -Profile Any
New-NetFirewallRule -DisplayName "estudio inbound 443" -Direction Inbound -Action Allow -Protocol TCP -LocalPort 443 -Profile Any
```

2. Your **provider's** network firewall / security group, in their web console.
   Allow inbound TCP 80 and 443. A cloud provider's default Windows image often
   allows only RDP (3389).

Windows Server images frequently ship with IIS listening on port 80. It has to
go, or win-acme's challenge can never be served:

```powershell
Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue |
    ForEach-Object { Get-Process -Id $_.OwningProcess } | Select-Object Id, ProcessName
# If that shows IIS:
Stop-Service W3SVC -ErrorAction SilentlyContinue
Set-Service  W3SVC -StartupType Disabled -ErrorAction SilentlyContinue
```

**VERIFY**

```powershell
# DNS resolves to this box (a free subdomain is usually instant; a real domain can take minutes)
Resolve-DnsName estudio.example.com -Type A | Select-Object Name, IPAddress
(Invoke-RestMethod https://api.ipify.org?format=json).ip

# Nothing is squatting on 80 or 443
Get-NetTCPConnection -LocalPort 80,443 -State Listen -ErrorAction SilentlyContinue
```

The two IPs must match, and the last command should print nothing. Reachability
from the internet cannot be verified yet — there is nothing listening. Stage 8
verifies it for real.

---

## 2. Get the code onto the box

The code must end up at exactly `C:\estudio`, with `C:\estudio\app\main.py` and
`C:\estudio\requirements.txt` present. Every script in this directory assumes
that path.

If the repository is reachable from the server, install Git for Windows
(<https://git-scm.com/download/win>), then:

```powershell
git clone <your-repo-url> C:\estudio
```

If it is not, zip the tree on your own machine, copy it through the RDP session
(RDP redirects your local drives; the clipboard also works for a zip), and
expand it:

```powershell
Expand-Archive -Path C:\Users\Administrator\Downloads\estudio.zip -DestinationPath C:\estudio
```

**VERIFY**

```powershell
Test-Path C:\estudio\app\main.py         # True
Test-Path C:\estudio\requirements.txt    # True
Test-Path C:\estudio\app\static\app.css  # True  (nginx serves /static/ from here in stage 9)
Test-Path C:\estudio\catalog             # True
```

If `Expand-Archive` produced `C:\estudio\estudio\app\main.py`, you have one
directory level too many. Move the inner tree up before continuing.

---

## 3. Install Python 3.11

The app is built and tested on **3.11**, and the wheel set (`onnxruntime`,
`opencv-python-headless`, `pillow-heif`) is pinned for it.

Download "Windows installer (64-bit)" for the latest 3.11.x from
<https://www.python.org/downloads/windows/> and install it **for all users**:

```powershell
# from the directory where you downloaded it
.\python-3.11.9-amd64.exe /passive InstallAllUsers=1 PrependPath=1
```

**For all users matters.** If you install into your own profile
(`C:\Users\Administrator\AppData\...`), the app will start perfectly when you
test it by hand and then fail at boot, because the service account cannot read
another user's profile.

**VERIFY**

```powershell
py -3.11 -V          # -> Python 3.11.x
py -3.11 -c "import sys; print(sys.executable)"
```

The path printed must **not** be under `C:\Users\`. If it is, uninstall and
reinstall with `InstallAllUsers=1`.

If the installer appears to do nothing at all, this image has an installer policy
(AppLocker, Software Restriction Policy, or Group Policy Windows Installer
restrictions). `deploy.ps1` detects and names those specifically — you can run it
just to get the diagnosis, or ask whoever owns the policy.

---

## 4. Create `C:\estudio\.env`

This file is the entire configuration and the entire login. Two rules, for the
rest of this machine's life:

- **Never regenerate `SECRET_KEY`** — it signs the session cookie. A new one logs
  her out of her phone.
- **Never regenerate `ACCESS_TOKEN`** — it *is* the login. It is the token in
  `https://your-domain/e/<ACCESS_TOKEN>`, the link she was sent once. A new one
  breaks that link, and there is no password reset flow to recover through.

Generate the two secrets (43 URL-safe characters each):

```powershell
py -3.11 -c "import secrets; print(secrets.token_urlsafe(32))"   # -> SECRET_KEY
py -3.11 -c "import secrets; print(secrets.token_urlsafe(32))"   # -> ACCESS_TOKEN
```

Start from the shipped example and edit it:

```powershell
Copy-Item C:\estudio\.env.example C:\estudio\.env
notepad C:\estudio\.env
```

Set at minimum:

```ini
OWNER_NAME=Nayane
DOMAIN=estudio.example.com

SECRET_KEY=<first generated value>
ACCESS_TOKEN=<second generated value>

# Windows absolute paths, forward slashes. THIS MATTERS - see below.
DATA_DIR=C:/estudio/data
CATALOG_DIR=C:/estudio/catalog

# Optional but strongly recommended:
ANTHROPIC_API_KEY=<key>
FAL_API_KEY=<key>
```

**No trailing comments on a value.** The parser strips whitespace and quotes,
but not a `#` and everything after it, so

```ini
# WRONG - do not copy this line
FAL_API_KEY=abc123          # or REPLICATE_API_TOKEN
```

sets the key to `abc123          # or REPLICATE_API_TOKEN`. That is not empty,
so the provider registers as configured and then fails authentication on every
paid call - which reads as a rejected key rather than a malformed one. Put
comments on their own line. The app checks for this at startup and says so.

**The `DATA_DIR` trap.** `.env.example` documents the Docker values
(`DATA_DIR=/srv/data`, `CATALOG_DIR=/srv/catalog`) directly below the Windows
ones. If a leading-slash value survives into your `.env`, Python resolves
`/srv/data` on Windows to `\srv\data` **on the current drive** — the SQLite
database, the uploads and every generated image land in `C:\srv\data`,
everything appears to work, and your backup of `C:\estudio\data` is empty.
`install-service.ps1` refuses to install if it sees this. Do not defeat it.

Then lock the file down — it holds the login:

```powershell
icacls C:\estudio\.env /inheritance:r /grant "*S-1-5-18:(R)" /grant "*S-1-5-32-544:(F)"
```

(Those are the well-known SIDs for SYSTEM and Administrators. SIDs rather than
names, because group names are localised — "Administrators" is "Administradores"
on a Spanish image and `icacls` fails with an unhelpful error.)

**VERIFY**

```powershell
# The keys are present and long enough (install-service.ps1 enforces >=32 and >=16)
Select-String -Path C:\estudio\.env -Pattern '^(SECRET_KEY|ACCESS_TOKEN|DOMAIN|DATA_DIR|CATALOG_DIR)=' |
    ForEach-Object { $_.Line.Split('=')[0] + ' = ' + $_.Line.Split('=',2)[1].Length + ' chars' }

# No Docker paths left behind
Select-String -Path C:\estudio\.env -Pattern '^\s*(DATA_DIR|CATALOG_DIR)\s*=\s*/'   # must print NOTHING

# No UTF-8 BOM (a BOM turns the first key into "\ufeffOWNER_NAME")
Format-Hex -Path C:\estudio\.env -Count 3
```

`SECRET_KEY` and `ACCESS_TOKEN` should both show 43 chars. The `Format-Hex`
output must **not** begin `EF BB BF`. If it does, rewrite the file without one:

```powershell
$t = Get-Content C:\estudio\.env -Raw
[System.IO.File]::WriteAllText('C:\estudio\.env', $t, (New-Object System.Text.UTF8Encoding($false)))
```

Record the magic link now, somewhere safe — you need it in stage 10:

```powershell
"https://estudio.example.com/e/" + ((Select-String -Path C:\estudio\.env -Pattern '^ACCESS_TOKEN=(.+)$').Matches[0].Groups[1].Value)
```

Treat that URL like a password. Do not paste it into a chat, an issue tracker,
or anywhere it will be logged.

---

## 5. Virtualenv and dependencies

`install-service.ps1` expects the interpreter at
**`C:\estudio\.venv\Scripts\python.exe`** (note the leading dot).

```powershell
cd C:\estudio
py -3.11 -m venv C:\estudio\.venv
C:\estudio\.venv\Scripts\python.exe -m pip install --upgrade pip
C:\estudio\.venv\Scripts\python.exe -m pip install -r C:\estudio\requirements.txt
```

Then the optional computer-vision extras, which are deliberately separate:

```powershell
C:\estudio\.venv\Scripts\python.exe -m pip install -r C:\estudio\requirements-cv.txt
C:\estudio\.venv\Scripts\python.exe C:\estudio\scripts\fetch_models.py
```

If `requirements-cv.txt` fails (usually a wheel with no `win_amd64` build for
3.11), **the deployment still works**. The consequence is specific: the quality
gate reports UNKNOWN for identity, proportions and hands rather than PASS, so
generated images are never checked against her real proportions — a model that
quietly slims her goes unnoticed. The app announces this on every screen. Fix it
later; do not block the deploy on it.

**VERIFY**

```powershell
C:\estudio\.venv\Scripts\python.exe -m uvicorn --version
C:\estudio\.venv\Scripts\python.exe -c "import fastapi, sse_starlette, pillow_heif, PIL; print('core ok')"
C:\estudio\.venv\Scripts\python.exe -c "import onnxruntime, cv2; print('cv ok')"   # may fail; see above

# The test suite is a cheap, honest smoke test of the whole tree
cd C:\estudio
C:\estudio\.venv\Scripts\python.exe -m pytest -q
```

`pillow_heif` importing is what makes iPhone HEIC uploads work. If that import
fails, HEIC photos will upload and then produce a broken preview — which reads
as an app bug, not an install problem.

---

## 6. Seed the catalog and the icons

```powershell
cd C:\estudio
$env:PYTHONPATH = 'C:\estudio'
C:\estudio\.venv\Scripts\python.exe C:\estudio\scripts\make_icons.py
C:\estudio\.venv\Scripts\python.exe C:\estudio\scripts\seed_catalog.py
Remove-Item Env:\PYTHONPATH
```

**VERIFY**

```powershell
Get-ChildItem C:\estudio\app\static\icon-*.png | Select-Object Name, Length
Get-ChildItem C:\estudio\catalog | Measure-Object | Select-Object Count
```

Both icons should exist with non-zero size, and the catalog directory should not
be empty. Stage 7's `/health` check confirms the catalog actually loaded.

**Do not re-run `seed_catalog.py` blindly on later deploys** unless you have
confirmed it is idempotent — if it is not, it duplicates rows every time.

---

## 7. Run the app as a real service (`install-service.ps1`)

This stage makes the app survive a reboot with nobody logged in, **and** gets
`.env` into the process. Both matter equally.

### Why NSSM

A Windows service must answer the Service Control Manager within about 30 seconds
of starting. `python.exe` does not — it has no idea the SCM exists. So `sc.exe
create` and `New-Service` pointed at python produce a service that installs
cleanly and then fails at every boot with **"Error 1053: The service did not
respond to the start request in a timely fashion"**, with nothing useful anywhere.
NSSM is the standard wrapper that sits between the two.

`install-service.ps1` refuses to guess where NSSM is. Install it once:

1. Download <https://nssm.cc/release/nssm-2.24.zip> (on any machine).
2. Copy `win64\nssm.exe` to **`C:\nssm\nssm.exe`** on this server.

```powershell
New-Item -ItemType Directory -Path C:\nssm -Force
# ...then copy nssm.exe into it, e.g. through the RDP redirected drive
Test-Path C:\nssm\nssm.exe   # must be True
```

### Install

```powershell
powershell -ExecutionPolicy Bypass -File C:\estudio\deploy\windows\install-service.ps1
```

**Do not pass `-NginxDir` yet.** nginx does not exist on this box, and stage 8
takes ownership of nginx's boot story. Passing it here creates a second,
competing supervisor.

What the script does, and why each part is there:

- Refuses to run at all if `SECRET_KEY` is missing or short, or `ACCESS_TOKEN` is
  missing or short — because both failures are silent and serious.
- Reads `.env` and injects every variable into the service environment
  (`nssm set estudio AppEnvironmentExtra ...`). **It never writes `.env`.**
- Adds `PYTHONUNBUFFERED=1`, `PYTHONUTF8=1`, `PYTHONIOENCODING=utf-8`, so the log
  is current while a batch runs and accented filenames survive the Windows code
  page.
- Starts exactly one worker:
  `--host 127.0.0.1 --port 8000 --proxy-headers --forwarded-allow-ips 127.0.0.1 --timeout-keep-alive 75`.
  One worker is a requirement, not a tuning choice: `/events/{session_id}` is
  served from an in-process event bus and `/previews` / `/finals` finish their
  work in background tasks, so a second worker would answer the browser from a
  process that knows nothing about the running batch.
- `Start SERVICE_AUTO_START` — comes back after a reboot with nobody logged in.
- Restart on crash, with throttling so a permanently broken build cannot restart
  in a tight loop.
- `AppStopMethodConsole 20000` — sends Ctrl+C and waits up to 20s on stop, so
  uvicorn can close open SSE streams instead of being killed mid-batch.
- Logs to `C:\estudio\data\logs\estudio.log`, rotating at 10 MB.
- Polls `http://127.0.0.1:8000/health` for 30 seconds and **tails the log and
  fails** if it never answers. It will not tell you it worked when it did not.

**VERIFY**

```powershell
Get-Service estudio | Format-List Name, Status, StartType
# Status = Running, StartType = Automatic

Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 4
```

Read that JSON; do not just check for a 200:

- `"ok": true`
- `"catalog"` — a number greater than 0. If it is 0, `seed_catalog.py` did not
  take, or `CATALOG_DIR` points somewhere wrong.
- `"identity_verification"` — `true` if `requirements-cv.txt` installed and the
  models were fetched; `false` means the anti-slimming proportion check is off.
- `"warnings"` — this array is the app telling you what is degraded. Empty, with
  real API keys configured, is the ideal. Warnings about missing provider keys
  mean the mock generator is in use.

Then prove the environment actually arrived. This is the check that catches the
`.env` gap:

```powershell
C:\nssm\nssm.exe get estudio AppEnvironmentExtra
```

You should see `SECRET_KEY=`, `ACCESS_TOKEN=`, `DATA_DIR=` and your API keys. If
that list is empty or missing those keys, stop and fix it here. Everything
downstream will look fine while she is logged out on every restart and the front
door stands open.

Finally, prove the data directory is where you think it is:

```powershell
Get-ChildItem C:\estudio\data
Test-Path C:\srv\data      # must be False
```

After a first run you will see `estudio.sqlite3` plus `uploads`, `images`,
`derivatives`, `profile`, `models` and `logs` appear as the app uses them.

---

## 8. nginx and TLS (`setup-tls.ps1`)

There is no certbot for nginx on Windows. `setup-tls.ps1` does the whole job:
downloads nginx, writes a bootstrap HTTP-only config, registers nginx to start at
boot **as SYSTEM**, obtains a certificate with win-acme, installs a renewal hook
that reloads nginx, writes the real HTTPS config, and then *exercises the hook*
so a broken hook is discovered today instead of in three months.

It reads `DOMAIN` from `.env` and **never writes to `.env`**.

### First run: staging

Let's Encrypt's production CA rate-limits you to 5 failures per account per hour.
Burn your mistakes on staging:

```powershell
powershell -ExecutionPolicy Bypass -File C:\estudio\deploy\windows\setup-tls.ps1 `
    -Domain estudio.example.com -Email you@example.com -Staging
```

**VERIFY (staging)**

```powershell
Get-Process nginx | Select-Object Id, ProcessName
Get-ScheduledTask -TaskName estudio-nginx | Select-Object TaskName, State
Get-ChildItem C:\estudio\certs

# -k because a staging certificate is deliberately untrusted
curl.exe -k -s -o NUL -w "%{http_code}\n" https://estudio.example.com/health
```

You want `200`. A staging certificate makes browsers complain loudly — that is
correct and expected at this point.

If you get nothing at all, the problem is reachability, not TLS: re-check your
provider's firewall from stage 1. Test from your phone on mobile data rather than
from the server itself — some hosts cannot reach their own public IP, which
produces a confusing failure that has nothing to do with your config.

### Second run: production

```powershell
powershell -ExecutionPolicy Bypass -File C:\estudio\deploy\windows\setup-tls.ps1 `
    -Domain estudio.example.com -Email you@example.com
```

If win-acme complains about an existing renewal for this host, run it with no
arguments and use its interactive menu to list and cancel the staging renewal:

```powershell
C:\estudio\tools\win-acme\wacs.exe
```

Check flag names in win-acme's own menus rather than copying them from anywhere,
including this file — they change between versions.

**VERIFY (production)**

```powershell
# A real, trusted certificate, and how long it has left
$h = 'estudio.example.com'
$c = New-Object Net.Sockets.TcpClient($h, 443)
$s = New-Object Net.Security.SslStream($c.GetStream(), $false, {$true})
$s.AuthenticateAsClient($h)
$cert = New-Object Security.Cryptography.X509Certificates.X509Certificate2($s.RemoteCertificate)
$cert | Format-List Subject, Issuer, NotBefore, NotAfter
$s.Dispose(); $c.Close()
```

`Issuer` must name Let's Encrypt and must **not** contain "STAGING". `NotAfter`
should be roughly 90 days out.

```powershell
# No trusted-cert override needed any more
curl.exe -s -o NUL -w "%{http_code}\n" https://estudio.example.com/health   # 200

# http redirects to https, preserving the path
curl.exe -s -o NUL -w "%{http_code} %{redirect_url}\n" http://estudio.example.com/ajustes
# -> 301 https://estudio.example.com/ajustes

# The renewal task exists. Without it the site dies silently at day 90.
Get-ScheduledTask | Where-Object TaskName -like 'win-acme*' | Select-Object TaskName, State

# The reload hook ran today and logged it
Get-Content C:\nginx\logs\renewal-reload.log -Tail 5
```

That last file should contain a `reloaded` line dated today. That line is your
proof that a renewal in 60 days will actually be *served* — nginx reads the
certificate once, at startup, and an ACME client that renews the files on disk
changes nothing about the running process.

---

## 9. Install the reviewed `nginx.conf`

`setup-tls.ps1` writes a working, deliberately minimal config. `nginx.conf` in
this directory is the reviewed one, and it is materially better on the things
this app cares about:

- `/events/` with `proxy_buffering off`, `proxy_cache off`, `gzip off`,
  `postpone_output 0`, `chunked_transfer_encoding on` and 3600s timeouts.
- `gzip_proxied` left at its default (off), so nothing coming from uvicorn can
  ever be compressed — and therefore buffered — by accident.
- `/upload` at 32m (not 30m: the app enforces 30 MB on the *file*, nginx measures
  the whole multipart body) with a readable JSON 413 instead of nginx's stock
  HTML page.
- `/media/` proxied to the app with `Cross-Origin-Resource-Policy: same-origin`,
  and `access_log off` so `access.log` never becomes a durable plaintext index of
  her photographs.
- `/sw.js` with the upstream cache headers stripped and `no-store` forced.
- A catch-all `server_name _;` returning 444 to the scanners that hit the raw IP,
  with the TLS parameters set at `http` level so the catch-all — which owns
  `default_server` on 443, and therefore governs the handshake — cannot silently
  fall back to nginx defaults that still include TLS 1.0.
- `daemon off;`, which also fixes `setup-tls.ps1`'s watchdog: with nginx staying
  in the foreground, the task's 5-minute repeating trigger becomes a true no-op
  while nginx is alive (`MultipleInstances IgnoreNew`) instead of relaunching a
  second nginx that fails to bind.

### Edits you must make first

Open `C:\estudio\deploy\windows\nginx.conf` in a real editor (VS Code, Notepad++
— **not** `Set-Content -Encoding UTF8`, which in Windows PowerShell 5.1 writes a
BOM that makes nginx report "unknown directive" on line 1).

1. **Hostname** — replace `estudio.example.com` in both `server_name` lines.

2. **Certificate paths** — the file ships with win-acme's default store path, but
   `setup-tls.ps1` writes PEMs to `C:\estudio\certs`. Find the real names:

   ```powershell
   Get-ChildItem C:\estudio\certs\*-chain.pem, C:\estudio\certs\*-key.pem
   ```

   Then set **all four** lines (the app server block *and* the catch-all):

   ```nginx
   ssl_certificate     C:/estudio/certs/estudio-chain.pem;
   ssl_certificate_key C:/estudio/certs/estudio-key.pem;
   ```

   Forward slashes. A backslash is an escape character in the nginx parser, so
   `C:\estudio\certs` is silently wrong.

3. **ACME challenge root** — the file says `root C:/nginx/html;`, but
   `setup-tls.ps1` gave win-acme `--webroot C:\estudio\acme`. They must match, or
   the renewal in 60 days fails validation:

   ```nginx
   location ^~ /.well-known/acme-challenge/ {
       root         C:/estudio/acme;
       autoindex    off;
       access_log   off;
       default_type text/plain;
   }
   ```

4. **Add an `/e/` location.** This is not in the shipped file and it needs to be.
   The magic-link token travels in the *path* (`/e/<ACCESS_TOKEN>`), and the
   config's `log_format` uses `$uri` — which strips the query string but not the
   path. Without this block, every visit writes the entire login credential in
   plaintext into `C:\nginx\logs\access.log`, and into every backup of it. Insert
   it immediately above `location / {`:

   ```nginx
   # The token in this path IS the login. Never log it.
   location ^~ /e/ {
       proxy_pass http://estudio_app;
       access_log off;
   }
   ```

   Note it deliberately sets no `proxy_set_header` and no `add_header`: one of
   either inside a location wipes out *every* inherited directive of that type,
   which is exactly how `X-Forwarded-Proto` disappears from one route and nobody
   notices.

5. **`/static/` is correct as shipped** — `root C:/estudio/app;` maps
   `/static/app.css` to `C:\estudio\app\static\app.css`, which is where
   `app/main.py` mounts `StaticFiles`. Leave `expires 1h;` alone: the filenames
   are `app.css` and `sw.js`, not content-hashed, so a long `max-age` with
   `immutable` would pin the stylesheet in her browser *and* in the service
   worker's cache, and no reload would ever dislodge it.

6. **IPv6** — if this VPS has IPv6 disabled, nginx cannot bind `[::]` and
   **exits**, taking the whole site down rather than just IPv6. Delete every
   `listen [::]:...` line.

### Install it

```powershell
# nginx needs these to exist and be writable, or uploads fail with a 500
New-Item -ItemType Directory -Force -Path C:\nginx\temp\client_body, C:\nginx\temp\proxy | Out-Null

# Keep the working config
Copy-Item C:\nginx\conf\nginx.conf "C:\nginx\conf\nginx.conf.bak-$(Get-Date -Format yyyyMMdd-HHmmss)"

# Copy the bytes (Copy-Item cannot introduce a BOM)
Copy-Item C:\estudio\deploy\windows\nginx.conf C:\nginx\conf\nginx.conf -Force

# TEST BEFORE RESTARTING. Not optional. A rejected config means nginx exits
# instead of starting, and from outside that is indistinguishable from a
# switched-off machine.
C:\nginx\nginx.exe -t -p C:/nginx/ -c C:/nginx/conf/nginx.conf
```

Only if that prints `syntax is ok` / `test is successful`, apply it through the
same hook win-acme uses (it tests, reloads, and falls back to a full restart if
nginx refuses the signal — which it occasionally does on Windows):

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File C:\estudio\deploy\windows\reload-nginx.ps1
Get-Content C:\nginx\logs\renewal-reload.log -Tail 3
```

**VERIFY**

```powershell
$D = 'estudio.example.com'

# Still alive
curl.exe -s -o NUL -w "%{http_code}\n" https://$D/health              # 200

# Security headers present
curl.exe -sI https://$D/ | Select-String 'strict-transport|x-frame|x-content-type|referrer-policy|content-security'

# Service worker must never be cached
curl.exe -sI https://$D/sw.js | Select-String 'cache-control|content-type'
# -> no-cache, no-store, must-revalidate   and a JavaScript content type

# Static assets served, and NOT pinned for a year
curl.exe -sI https://$D/static/app.css | Select-String 'cache-control|expires|content-type'
# -> max-age=3600, and NOT "immutable"

# No directory listing, anywhere, ever
curl.exe -s -o NUL -w "%{http_code}\n" https://$D/static/     # 403 or 404 - never a listing
curl.exe -s https://$D/static/ | Select-String 'Index of'     # must print NOTHING

# Private images are behind the cookie, not on disk
curl.exe -s -o NUL -w "%{http_code}\n" https://$D/media/anything.jpg   # 401/403/404 - never 200 with image bytes

# Scanners hitting the raw IP get nothing
curl.exe -sk -o NUL -w "%{http_code}\n" https://<server-ip>/           # empty / 000 (connection closed, 444)

# The version is not advertised
curl.exe -sI https://$D/ | Select-String -Pattern '^Server:'           # -> "Server: nginx", no version
```

---

## 10. End-to-end verification — the part that actually matters

Everything above proves the plumbing. This proves the product. Do it before you
send her the link.

### 10.1 She can log in, and the cookie is right

Open the magic link from stage 4:
`https://estudio.example.com/e/<ACCESS_TOKEN>`

You should land on `/`, logged in. In devtools → **Application → Cookies**, find
`estudio_session` and confirm:

- **Secure** is checked
- **HttpOnly** is checked
- **SameSite** is `Lax`

Or from the command line (this prints the token to your console and shell
history — clear it afterwards if that matters):

```powershell
curl.exe -sD - -o NUL "https://estudio.example.com/e/<ACCESS_TOKEN>" | Select-String 'Set-Cookie'
```

The `Set-Cookie` line must contain `Secure`. If it does not, `X-Forwarded-Proto`
is not reaching uvicorn — go to troubleshooting, and do not hand out the link
until it is fixed. Her session would be travelling without `Secure`.

### 10.2 SSE is not buffered — the big one

In the browser, start a real batch (upload a photo, request previews). Open
devtools → **Network**, click the `/events/<session_id>` request, and watch the
**EventStream** tab.

- **Correct:** events appear one at a time across the 60–180 seconds of the batch,
  and tiles fill into the grid progressively.
- **Broken:** absolutely nothing for the whole batch, then six events with
  near-identical timestamps at the very end.

From a shell, with the cookie value copied out of devtools and the session id
taken from the request URL:

```powershell
curl.exe -N -H "Cookie: estudio_session=<value>" "https://estudio.example.com/events/<session_id>"
```

`-N` disables curl's own buffering. Lines must trickle out as work completes.
`sse-starlette` also emits periodic keepalive pings, so a healthy idle stream is
not silent — but do not treat those pings as proof: they would keep the
connection alive even while real event frames sat stuck in an nginx buffer.

Note that **this app does not send `X-Accel-Buffering: no`** on the SSE response
(it returns a bare `EventSourceResponse`). There is no app-side safety net.
`proxy_buffering off` in the `/events/` location is the only thing standing
between her and a frozen grid.

### 10.3 A real photo, from a real phone

Upload a large HEIC straight from an iPhone over mobile data — not a small JPEG
from your desktop. That exercises `pillow-heif`, the 32m body limit, the 120s
`client_body_timeout`, and the temp-file buffering that keeps the single uvicorn
worker from being held open for the whole of a slow upload.

Then check the 413 path with an oversized file:

```powershell
$f = "$env:TEMP\big.bin"
$fs = [IO.File]::Create($f); $fs.SetLength(40MB); $fs.Close()
curl.exe -s -o - -w "\n%{http_code}\n" -F "file=@$f" https://estudio.example.com/upload
Remove-Item $f
```

With the reviewed config you get the JSON `{"error":"file_too_large", ...}`. Some
browsers surface an nginx 413 as a bare connection reset regardless, because
nginx rejects on `Content-Length` before reading the body — the reliable fix for
her is a client-side size check before the request is sent.

### 10.4 The gallery still needs a cookie

Open a private/incognito window and paste a `/media/...` URL taken from the
logged-in session. It must **not** load. If it does, something is serving
`C:\estudio\data` from disk and every photograph on the box is readable by anyone
holding a URL.

---

## 11. Reboot test

Not optional, and not paranoia. "Survives a reboot with nobody logged in" is a
requirement, and it is the requirement most deployments fail — quietly, weeks
later, at 3am during Windows Update.

```powershell
Restart-Computer -Force
```

Wait 2–3 minutes. **From your own machine, not from RDP:**

```powershell
curl.exe -s -o NUL -w "%{http_code}\n" https://estudio.example.com/health   # 200
```

Then RDP back in and confirm both halves came back on their own:

```powershell
Get-Service estudio | Select-Object Status, StartType          # Running / Automatic
Get-Process nginx  | Select-Object Id, ProcessName             # at least one
Get-ScheduledTask -TaskName estudio-nginx | Get-ScheduledTaskInfo |
    Select-Object TaskName, LastRunTime, LastTaskResult
```

`LastTaskResult` of `267009` (0x00041301) means "the task is currently running",
which is exactly right for a foreground nginx. `0` means it started and exited —
if nginx is not in the process list, that is a failure. `0x8007052E` is a logon
failure (stored credential no longer matches) and `0x80070534` means the account
lacks the "Log on as a batch job" right.

Also reopen the site in the browser you used in stage 10 and confirm **she is
still logged in**. If the reboot logged you out, `SECRET_KEY` is not reaching the
process and the app generated an ephemeral signing key. Go back to stage 7's
`AppEnvironmentExtra` check.

---

## 12. Backups

The entire state of this system is two things:

| What | Where | If you lose it |
|---|---|---|
| The data directory | `C:\estudio\data` — `estudio.sqlite3` plus `uploads`, `images`, `derivatives`, `profile`, `models` | Her photographs and every session. Unrecoverable. |
| The configuration | `C:\estudio\.env` | `SECRET_KEY` (she is logged out) and `ACCESS_TOKEN` (the link she saved stops working, with no reset flow). |

Everything else — code, venv, nginx, the certificate — is reproducible by
re-running this runbook.

### Taking a backup

Do not zip `estudio.sqlite3` while the app is running. SQLite keeps `-wal` and
`-shm` siblings, and a naive file copy can capture a torn database. Use SQLite's
own online backup:

```powershell
$stamp   = Get-Date -Format 'yyyyMMdd-HHmmss'
$staging = "C:\backups\staging-$stamp"
New-Item -ItemType Directory -Force -Path $staging, "$staging\data" | Out-Null

# Consistent snapshot of the database, taken live
C:\estudio\.venv\Scripts\python.exe -c "import sqlite3,sys; s=sqlite3.connect(sys.argv[1]); d=sqlite3.connect(sys.argv[2]); s.backup(d); d.close(); s.close()" `
    C:\estudio\data\estudio.sqlite3 "$staging\data\estudio.sqlite3"

# The image trees (plain files; safe to copy live)
robocopy C:\estudio\data "$staging\data" /E /XF estudio.sqlite3 estudio.sqlite3-wal estudio.sqlite3-shm /XD logs /NFL /NDL /NJH /NJS

# The configuration
Copy-Item C:\estudio\.env "$staging\.env"

Compress-Archive -Path "$staging\*" -DestinationPath "C:\backups\estudio-$stamp.zip" -Force
Remove-Item $staging -Recurse -Force
```

`robocopy` exits with codes 0–7 on success (1 = files copied). Do not treat a
non-zero exit as failure here.

### Rules

- **That zip contains private photographs of an identifiable person and the
  `ACCESS_TOKEN` that unlocks them.** Encrypt it, and never leave it anywhere
  nginx can serve.
- **It is not a backup until it is off this machine.** Copy it down over the RDP
  redirected drive, or to object storage. A provider snapshot of the whole VM is
  a fine second layer; do not rely on it as the only layer.
- Exclude `C:\nginx\temp\proxy` from any backup — it holds short-lived plaintext
  copies of her images while responses are spooled.
- **Test the restore once, now**, on a throwaway box. An untested backup is a
  rumour.

### Restoring

```powershell
Stop-Service estudio
Expand-Archive C:\backups\estudio-<stamp>.zip -DestinationPath C:\restore
Remove-Item C:\estudio\data -Recurse -Force
Copy-Item C:\restore\data C:\estudio\data -Recurse
Copy-Item C:\restore\.env C:\estudio\.env -Force
Start-Service estudio
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json
```

Restore `.env` from the same backup as the data. A mismatched `SECRET_KEY` logs
her out; a mismatched `DATA_DIR` points the app at an empty directory.

### A scheduled nightly backup

```powershell
$a = New-ScheduledTaskAction -Execute 'powershell.exe' `
      -Argument '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File C:\estudio\deploy\windows\backup.ps1'
$t = New-ScheduledTaskTrigger -Daily -At 04:00
$p = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
Register-ScheduledTask -TaskName 'estudio-backup' -Action $a -Trigger $t -Principal $p `
      -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable)
```

Put the commands above into `backup.ps1` first, plus whatever off-box copy and
retention you want. There is no `backup.ps1` in this repository — write it to
suit where your backups go.

---

## 13. Updating

Always back up before updating. Always.

```powershell
# 1. Back up (section 12)

# 2. New code
cd C:\estudio
git pull

# 3. Dependencies, in case they moved
C:\estudio\.venv\Scripts\python.exe -m pip install -r C:\estudio\requirements.txt

# 4. Re-run the installer. It is idempotent: it reconfigures the existing
#    service, re-reads .env into the service environment, and never writes .env.
powershell -ExecutionPolicy Bypass -File C:\estudio\deploy\windows\install-service.ps1
```

`install-service.ps1` stops the service, reconfigures it, restarts it, and then
polls `/health` for 30 seconds — failing loudly with the last 40 lines of the log
if the new code does not come up. That is the whole update path.

Do **not** re-run `seed_catalog.py` unless you have confirmed it is idempotent.

nginx only needs touching if you changed `nginx.conf`:

```powershell
C:\nginx\nginx.exe -t -p C:/nginx/ -c C:/nginx/conf/nginx.conf
powershell -NoProfile -ExecutionPolicy Bypass -File C:\estudio\deploy\windows\reload-nginx.ps1
```

**VERIFY after any update**

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 4
Get-Content C:\estudio\data\logs\estudio.log -Tail 30
curl.exe -s -o NUL -w "%{http_code}\n" https://estudio.example.com/health
```

Then load the UI with a **hard refresh** (Ctrl+Shift+R). `/static/` is cached for
an hour and the service worker caches the app shell — if the UI looks unchanged,
read the "old version after a deploy" symptom below before assuming the deploy
failed.

---

## 14. Troubleshooting, by symptom

Find your symptom. The cause is underneath it.

### "The preview grid sits completely still for the whole batch, then all six images appear at once at the end."

nginx is buffering the SSE stream. It accumulates the response in `proxy_buffers`
and forwards it when a buffer fills or the response ends — and for a stream,
"ends" means the end of the batch.

```powershell
Select-String -Path C:\nginx\conf\nginx.conf -Pattern 'location .*/events/' -Context 0,20
```

Inside a location that actually matches `/events/` you need: `proxy_buffering
off;`, `proxy_cache off;`, `gzip off;`, `proxy_http_version 1.1;`. Check the
prefix matches your URL — `location ^~ /events/` matches `/events/abc123`. Check
`gzip_proxied` is not set to `any` at `http` level: compressing a stream
reintroduces exactly this stall, because the compressor is itself a buffer.

If the config looks right, confirm nothing else is in the path — a CDN, a
provider "web application firewall", or Cloudflare proxying in front of your A
record will buffer this stream no matter what nginx does. The DNS record must
point straight at the VPS.

### "The grid freezes partway through and a 504 appears in the Network tab after about a minute."

`proxy_read_timeout` is at its 60s default on `/events/`. The stream is *meant*
to go quiet while six images render. Set `proxy_read_timeout 3600s;` and
`proxy_send_timeout 3600s;` inside the `/events/` location, plus `send_timeout
300s;` for the client-facing side.

These are per-*read* timeouts, not total-request timeouts — the clock resets on
every byte — but the whole point of this route is that it can legitimately go
quiet for longer than a minute.

### "She logs in through the link and is thrown straight back to the login page, every time."

The browser is dropping the session cookie. `_is_https()` in `app/main.py` read
`x-forwarded-proto` as something other than `https`, so the cookie was issued
without `Secure` while the origin was HTTPS — or the reverse.

```powershell
# Is the header set at all?
Select-String -Path C:\nginx\conf\nginx.conf -Pattern 'X-Forwarded-Proto'
```

It must be `proxy_set_header X-Forwarded-Proto $scheme;`, and it must be in
effect for **the location that serves `/e/` and `/`**. The classic version of
this bug: a location contains one `proxy_set_header` of its own, which silently
discards *every* `proxy_set_header` inherited from the server block —
`X-Forwarded-Proto` included. nginx inherits these directives from the enclosing
level only if the current level defines none of its own.

Also confirm uvicorn is trusting it — the service must run with
`--proxy-headers --forwarded-allow-ips 127.0.0.1`:

```powershell
C:\nssm\nssm.exe get estudio AppParameters
curl.exe -sD - -o NUL "https://estudio.example.com/e/<TOKEN>" | Select-String 'Set-Cookie'
```

### "The cookie is set, but devtools shows Secure unchecked."

Same cause, opposite direction: the app believes the request arrived over plain
HTTP. Same fix. Do not leave this — her session is travelling without `Secure`
and will leak on any downgraded request.

### "She is logged out after every restart or reboot and has to use the magic link again."

`SECRET_KEY` is not in the server process's environment, so the app generated an
ephemeral signing key — which changes on every start.

```powershell
C:\nssm\nssm.exe get estudio AppEnvironmentExtra
```

If `SECRET_KEY=` is not in that output, the service was installed without it, or
`.env` has changed since. Re-run `install-service.ps1`. Remember: **this app has
no python-dotenv.** A `.env` file that nothing reads is decoration.

### "Anyone who opens the address is let straight in, without the token."

`ACCESS_TOKEN` is empty in the process environment — the app's documented
behaviour when it is unset is to admit everyone. Same check as above. This is the
most serious failure in the list: private photographs of an identifiable person,
publicly readable. Fix it before anything else, then confirm:

```powershell
curl.exe -s -o NUL -w "%{http_code}\n" https://estudio.example.com/
# expect a redirect to /entrar, not 200 with content
```

### "Every page returns 502, or a JSON page saying the application is not responding."

uvicorn is down; nginx is fine.

```powershell
Get-Service estudio
Get-Content C:\estudio\data\logs\estudio.log -Tail 60
Invoke-WebRequest http://127.0.0.1:8000/health -UseBasicParsing
```

The traceback is in that log. Usual causes: a missing key in `.env`, a syntax
error in new code, or a dependency that vanished from the venv. Restart with
`C:\nssm\nssm.exe restart estudio`.

### "It worked for three months. Now the browser shows a full-page certificate error."

The certificate renewed on disk and nginx never reloaded. nginx reads the
certificate **once**, at startup, and holds it in memory.

```powershell
Get-Content C:\nginx\logs\renewal-reload.log -Tail 20
Get-ChildItem C:\estudio\certs | Select-Object Name, LastWriteTime
Get-ScheduledTask | Where-Object TaskName -like 'win-acme*' | Get-ScheduledTaskInfo
```

- Files recently modified, no matching `reloaded` line → the hook is broken. Run
  it by hand:
  `powershell -File C:\estudio\deploy\windows\reload-nginx.ps1`
- Files not modified at all → the renewal never ran. Check the win-acme task's
  `LastTaskResult` and its logs under `C:\ProgramData\win-acme\`.
- Validation failing at renewal is usually a webroot mismatch: the
  `/.well-known/acme-challenge/` `root` in `nginx.conf` must be the same directory
  win-acme was given as `--webroot` (`C:\estudio\acme`).

### "nginx is not running and will not start."

```powershell
C:\nginx\nginx.exe -t -p C:/nginx/ -c C:/nginx/conf/nginx.conf
Get-Content C:\nginx\logs\error.log -Tail 40
```

In rough order of likelihood:

- **`unknown directive` on line 1** — the config was saved with a UTF-8 BOM.
  Windows PowerShell's `-Encoding UTF8` writes one. Rewrite with
  `[System.IO.File]::WriteAllText($p, $t, (New-Object System.Text.UTF8Encoding($false)))`.
- **A path it cannot open** — backslashes in the config. nginx treats `\` as an
  escape character. `C:/nginx/logs`, never `C:\nginx\logs`.
- **`bind() to [::]:443 failed`** — IPv6 is disabled on this host. nginx exits
  rather than degrading. Delete every `listen [::]:...` line.
- **`bind() to 0.0.0.0:80 failed (10048)`** — something else has the port: IIS, or
  a second nginx (see below).
- **`cannot load certificate key`** — either the account nginx runs as cannot read
  the key file (win-acme tightens ACLs on its own output), or the key has a
  passphrase. A password-protected key makes nginx block at startup waiting on a
  console prompt that, under a service or a task, nobody will ever answer.
- **`directive is not allowed here`** — a main-context directive such as
  `worker_shutdown_timeout` placed inside `http { }`. nginx rejects the whole file
  and exits; under a supervisor that looks exactly like "the site is gone".

### "The site works while I'm logged in over RDP, and is gone after a reboot."

Something is running interactively instead of at boot.

```powershell
Get-Service estudio | Select-Object Status, StartType         # StartType must be Automatic
Get-ScheduledTask -TaskName estudio-nginx | Get-ScheduledTaskInfo
```

- `0x8007052E` — logon failure: the task's stored password no longer matches the
  account. Re-register the task.
- `0x80070534` — the account lacks "Log on as a batch job" (`secpol.msc` → Local
  Policies → User Rights Assignment).
- Task ran and exited immediately — under `setup-tls.ps1`'s generated config nginx
  daemonises and the task completes. Survivable, but it means the watchdog cannot
  tell whether nginx is alive. The reviewed `nginx.conf` sets `daemon off;`, which
  fixes it.

### "`C:\nginx\logs\error.log` fills with `bind() ... failed (10048)` every five minutes."

`setup-tls.ps1` registers a 5-minute repeating trigger as a watchdog. If nginx
daemonised (no `daemon off;` in the config) the task is never "running", so
`MultipleInstances IgnoreNew` does not suppress the trigger: a second nginx is
launched every five minutes, fails to bind, and exits. Noisy, not fatal. Install
the reviewed `nginx.conf` with `daemon off;` and it stops.

### "There are two nginx processes / my config edits have no effect."

Two boot mechanisms are fighting. This happens if you ran `deploy.ps1` (which
registers `\estudio\estudio-nginx`) *and* `setup-tls.ps1` (which registers
`estudio-nginx` at the task-scheduler root), or if you also passed `-NginxDir` to
`install-service.ps1` (which creates an `nginx` Windows service).

```powershell
Get-ScheduledTask | Where-Object TaskName -like '*nginx*' | Select-Object TaskPath, TaskName, State
Get-Service nginx -ErrorAction SilentlyContinue
Get-Process nginx | Select-Object Id, Path
```

Pick **one**. Disable or unregister the others, kill every nginx process, and
start the survivor:

```powershell
Get-Process nginx | Stop-Process -Force
Start-ScheduledTask -TaskName estudio-nginx
```

### "Uploading a photo from the phone fails with no message at all."

```powershell
Get-Content C:\nginx\logs\error.log -Tail 30
```

- **413 in `access.log`** — `client_max_body_size`. It must be **32m**, not 30m:
  the app enforces 30 MB on the file, nginx measures the whole multipart body
  including boundaries and field names. At exactly 30m nginx rejects files the app
  would have accepted, and nginx's rejection is the ugly one.
- **A `client_body_temp` error, or a 500** — `C:\nginx\temp\client_body` does not
  exist or is not writable by the account nginx runs as. Create it.
- **A timeout mid-upload** — `client_body_timeout`. A 30 MB photo over a weak
  mobile uplink can trickle; the 60s default is too tight. Use 120s.
- Some browsers surface an nginx 413 as a connection reset rather than showing the
  JSON body, because nginx rejects on `Content-Length` before reading the body.
  The reliable fix is a size check in the upload form.

### "The HEIC uploaded but the preview is broken."

`pillow-heif` is not importable in the venv.

```powershell
C:\estudio\.venv\Scripts\python.exe -c "import pillow_heif; print(pillow_heif.__version__)"
```

Reinstall it. This surfaces as an app bug rather than an install error, which is
why it is easy to misdiagnose.

### "After deploying, the UI is unchanged. Ctrl+F5 does not help."

The service worker is serving its own cached copy of the app shell.

```powershell
curl.exe -sI https://estudio.example.com/sw.js | Select-String 'cache-control'
```

It must say `no-cache, no-store, must-revalidate`. If `/sw.js` was ever served
with a long cache lifetime, the only cure on her device is unregistering the
worker in devtools — which for a remote single-user tool means it effectively
cannot be cleared. This is why `/static/` is capped at `expires 1h;` and why
`immutable` must never appear there while filenames are `app.css` and `sw.js`.

### "Images 404 on `/media/` but the rest of the app works."

Check the app log first — this is an app-side or data-path issue.

**Do not "fix" it by pointing nginx at `C:\estudio\data`.** `/media/` is proxied
to the app on purpose: those files are private photographs protected by a session
cookie that only the app can verify. nginx has no idea whether a cookie is valid.
A `root` there serves every image to anyone holding a URL, with no authentication
at all. There is no configuration in this repository that does that, and there
must never be.

### "The database and her earlier sessions have vanished."

```powershell
Test-Path C:\srv\data
Get-ChildItem C:\estudio\data
```

If `C:\srv\data` exists, `DATA_DIR` was a Docker-style `/srv/data` and Python
resolved it to the current drive. Fix `.env`, move the data across, re-run
`install-service.ps1`.

### "PowerShell refuses to run the script."

```
File ... cannot be loaded because running scripts is disabled on this system.
```

Run them the way this runbook writes them, with the policy bypassed for that one
invocation:

```powershell
powershell -ExecutionPolicy Bypass -File C:\estudio\deploy\windows\install-service.ps1
```

And check the window is actually elevated — the scripts fail fast if it is not,
but the message scrolls past easily.

### "Port 443 is refused from outside, but nginx is running here."

```powershell
Get-NetTCPConnection -LocalPort 443 -State Listen
Get-NetFirewallRule -DisplayName 'estudio*' | Select-Object DisplayName, Enabled, Direction, Action
```

If both look right, it is the provider's network firewall / security group, in
their web console. Also try from a phone on mobile data: some hosts cannot reach
their own public IP, which produces a failure that looks like a firewall problem
and is not.

---

## 15. Windows-specific rough edges, honestly

This stack is genuinely rougher on Windows than on Linux. None of these are
show-stoppers, but pretending they do not exist is how people lose an afternoon.

- **nginx is not a Windows service.** It is a plain console program with no
  Service Control Manager handshake. `sc create` pointed at `nginx.exe` produces a
  service that fails to start. It needs a SYSTEM scheduled task (what
  `setup-tls.ps1` does) or a wrapper like NSSM/WinSW.
- **`python.exe` is not a Windows service either.** Same reason. Error 1053, every
  boot, forever. That is what NSSM is for.
- **nginx.conf paths must use forward slashes.** `\` is an escape character in the
  nginx parser, so `C:\nginx\logs` is *silently* wrong.
- **A UTF-8 BOM breaks nginx.conf.** Windows PowerShell 5.1's `-Encoding UTF8`
  writes one, and so does Notepad in some versions. nginx reports "unknown
  directive" on line 1, which reads like a typo you did not make.
- **nginx/Windows uses `select()`.** No epoll, no IOCP. `worker_connections` is
  bounded by an `FD_SETSIZE` of 1024 compiled into the official build, and only
  one worker actually accepts connections — the others idle. For one user that is
  orders of magnitude more headroom than needed, but it is why
  `worker_processes 1;`.
- **No `sendfile()`.** Zero-copy is unavailable; the config leaves it off so
  nothing depends on behaviour this platform does not have.
- **No logrotate.** `C:\nginx\logs\access.log` grows forever. Rotate it with a
  scheduled task that renames the files and then runs
  `C:\nginx\nginx.exe -p C:/nginx/ -s reopen` — without the reopen, nginx keeps
  writing to the renamed handle.
- **No certbot.** win-acme is the tool, it needs the `pemfiles` store to produce
  something nginx can read, the key must have no passphrase, and *you* are
  responsible for the reload hook. A certificate that renews without a reload is a
  90-day time bomb with a green log.
- **`nginx -s reload` signals travel through a named object owned by the account
  that started nginx.** A reload issued from your admin console does not reliably
  reach a master running as another account. `reload-nginx.ps1` handles this by
  falling back to a restart.
- **Scheduled tasks have a 3-day default execution limit.** A long-running server
  registered without `-ExecutionTimeLimit (New-TimeSpan -Seconds 0)` gets killed
  mid-week for no visible reason. The scripts here set it correctly.
- **"Log on as a batch job" is a separate right.** A task that runs "whether the
  user is logged on or not" needs it; without it the task registers fine and then
  fails at boot with `0x80070534`, which reads like a password bug.
- **Group names are localised.** `icacls` with `Administrators` fails on a
  Spanish-language image. Grant by SID (`*S-1-5-32-544`).
- **Windows PowerShell 5.1 corrupts native exit codes** when you redirect a native
  program's stderr inside the shell (`2>&1` wraps each line in an ErrorRecord and
  sets `$?` to false even on exit code 0). That is why `setup-tls.ps1` runs
  `nginx.exe` out of process and reads its output back from files.
- **Windows Defender** scans `C:\estudio\data` as images are written. If you see
  intermittent file-in-use errors or unexplained slowness during batches, consider
  an exclusion for the data directory — weighed against whatever else runs on this
  box.
- **Local admin access to this machine is equivalent to full access to her
  photographs.** NSSM stores the injected environment (including `ACCESS_TOKEN`)
  under the service's registry key. The `.env` ACL keeps ordinary accounts out; it
  does not, and cannot, keep an administrator out. Treat the box accordingly.

---

## Appendix A: the `deploy.ps1` all-in-one route

`deploy.ps1` does the whole job in a single command, and adds something this
runbook's route does not: **two low-privilege local accounts**, `estudio_app`
(runs uvicorn, can write `data\`, cannot write the code) and `estudio_web` (runs
nginx, has no access to `C:\estudio` at all — so it cannot read `.env`). That is
real defence in depth, and it is why the script is worth knowing about.

```powershell
powershell -ExecutionPolicy Bypass -File C:\estudio\deploy\windows\deploy.ps1 -Domain estudio.example.com
```

Know these differences before choosing it:

1. **It does not inject `.env`.** It writes `run-app.cmd`, which launches uvicorn
   with whatever environment the scheduled task happens to have — and the app has
   no python-dotenv. `SECRET_KEY` and `ACCESS_TOKEN` will be empty. **You must
   close this gap**, either by using `install-service.ps1` for the app instead of
   its scheduled task, or by another mechanism of your choosing. Setting them as
   machine-scope environment variables works, but publishes the token to every
   local account, which defeats the ACL design the script just built.
2. **Different venv path.** `deploy.ps1` builds `C:\estudio\venv`;
   `install-service.ps1` expects `C:\estudio\.venv`. To use both, link them:

   ```powershell
   New-Item -ItemType Junction -Path C:\estudio\.venv -Target C:\estudio\venv
   ```

3. **Different supervision.** It registers scheduled tasks under `\estudio\`
   (`estudio-app`, `estudio-nginx`, `estudio-cert-reload`). If you also run
   `setup-tls.ps1` you end up with two different tasks both called `estudio-nginx`
   at different task paths, both launching nginx. Disable one.
4. **It obtains no certificate.** It expects `C:\nginx\certs\fullchain.pem` +
   `privkey.pem` to appear by some other means, and registers a daily task that
   hashes the certificate and restarts nginx when the bytes change. That watcher is
   a good idea; the certificate still has to come from win-acme.
5. **It writes its own `nginx.conf`**, using `include` files
   (`estudio-proxy.conf`, `estudio-headers.conf`) to work around the `add_header` /
   `proxy_set_header` inheritance trap. It proxies `/static/` to the app rather
   than serving it from disk — necessarily, since `estudio_web` has no access to
   `C:\estudio`. If you install the reviewed `nginx.conf` instead, its
   `root C:/estudio/app;` for `/static/` will 403 under that account: either grant
   `estudio_web` read on `C:\estudio\app\static`, or delete the `/static/` block
   and let the app serve it.
6. **It rotates the service accounts' passwords on each run** and re-registers the
   tasks in the same breath. If a run dies partway (a pip failure, say), re-run it
   to completion **before** rebooting.

Its `-SkipNginx` switch is the app-only update path, and `-SkipSeed` avoids
re-running the seed scripts.

---

## Appendix B: where everything lives

| Path | What |
|---|---|
| `C:\estudio` | Application code |
| `C:\estudio\.env` | Configuration, `SECRET_KEY`, `ACCESS_TOKEN`, API keys. **Back up. Never regenerate.** |
| `C:\estudio\.venv` | Python 3.11 virtualenv (`install-service.ps1` route) |
| `C:\estudio\data` | SQLite (`estudio.sqlite3`), `uploads`, `images`, `derivatives`, `profile`, `models`. **Back up.** |
| `C:\estudio\data\logs\estudio.log` | Application log (uvicorn stdout/stderr, rotated at 10 MB) |
| `C:\estudio\catalog` | Seeded catalog content |
| `C:\estudio\certs` | Let's Encrypt PEM pair from win-acme (`*-chain.pem`, `*-key.pem`) |
| `C:\estudio\acme` | ACME http-01 challenge webroot — the **only** filesystem path nginx serves besides `/static/` |
| `C:\estudio\tools\win-acme\wacs.exe` | The ACME client |
| `C:\estudio\deploy\windows\reload-nginx.ps1` | Post-renewal hook (generated by `setup-tls.ps1`) |
| `C:\nginx` | nginx for Windows |
| `C:\nginx\conf\nginx.conf` | Live config |
| `C:\nginx\logs\error.log` / `access.log` | nginx logs — no rotation, watch their size |
| `C:\nginx\logs\renewal-reload.log` | Proof the renewal hook fires |
| `C:\nginx\temp\client_body`, `C:\nginx\temp\proxy` | Upload spool and response spool. Short-lived plaintext copies of her images live here — exclude from backups, include in disk encryption. |
| `C:\nssm\nssm.exe` | Service wrapper |

Services and tasks: `estudio` (Windows service, Automatic), `estudio-nginx`
(scheduled task, SYSTEM, at startup), and a `win-acme` renewal task.

---

## Appendix C: routine commands

```powershell
# Is everything up?
Get-Service estudio; Get-Process nginx -ErrorAction SilentlyContinue
Invoke-RestMethod http://127.0.0.1:8000/health | ConvertTo-Json -Depth 4
curl.exe -s -o NUL -w "%{http_code}\n" https://estudio.example.com/health

# Restart the app
C:\nssm\nssm.exe restart estudio

# Follow the app log
Get-Content C:\estudio\data\logs\estudio.log -Tail 50 -Wait

# Test and apply an nginx config change
C:\nginx\nginx.exe -t -p C:/nginx/ -c C:/nginx/conf/nginx.conf
powershell -NoProfile -ExecutionPolicy Bypass -File C:\estudio\deploy\windows\reload-nginx.ps1

# Restart nginx outright
Get-Process nginx | Stop-Process -Force; Start-ScheduledTask -TaskName estudio-nginx

# How long has the certificate got?
$h='estudio.example.com'; $c=New-Object Net.Sockets.TcpClient($h,443)
$s=New-Object Net.Security.SslStream($c.GetStream(),$false,{$true}); $s.AuthenticateAsClient($h)
(New-Object Security.Cryptography.X509Certificates.X509Certificate2($s.RemoteCertificate)).NotAfter
$s.Dispose(); $c.Close()

# Recent nginx errors only
Get-Content C:\nginx\logs\error.log -Tail 40
```
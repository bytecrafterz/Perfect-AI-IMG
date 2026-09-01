# Estudio — operating manual

For whoever runs this. Written after a full verification pass, so the numbers
here are measured rather than intended.

**Live at https://wonderimg.duckdns.org** · 538 tests · source of truth in
`c:\Users\Administrator\Documents\8.22 IMG`, deployed copy at `C:\estudio`.

---

## 1. What it does

Nayane sends one photograph, taps a few styles, and gets finished photographs
back. No prompts, no settings, no vocabulary to learn.

Two stages, and the split is the whole cost model:

| stage | what it is | who pays |
|---|---|---|
| **previews** | 4–9 cheap options to choose between | free (Cloudflare) |
| **finals** | only the ones she picks, at full quality | $0.04 each (fal) |

She never pays for the options she doesn't want.

---

## 2. Daily use

**Her side.** Open the private link once — it stays signed in for 365 days.
Upload a photo, tap a style, wait, keep what she likes. That is the entire
interface.

**Your side.** Nothing, unless something below says otherwise.

### Her link

```powershell
"https://wonderimg.duckdns.org/e/" + ((Get-Content C:\estudio\.env |
  Select-String '^ACCESS_TOKEN=') -replace 'ACCESS_TOKEN=','')
```

That token **is** her login — there is no password. Anyone holding the link is
her. Send it once, directly. If it leaks, change `ACCESS_TOKEN` in `.env`,
restart, send a new link; every existing session dies immediately.

### Restarting

```powershell
& 'C:\estudio\deploy\windows\estudio-arranque.ps1' -Restart
```

**Must be elevated.** The app is started by a scheduled task running as
Administrator, so an ordinary shell gets `Access is denied` and the script
reports "uvicorn ya estaba en marcha" while doing nothing.

---

## 3. Money

Two accounts, both hers, neither reachable by this software — it holds API
keys, which spend credit that already exists and cannot buy more. **Nothing
here can charge her card.**

| | cost |
|---|---|
| preview | **$0.000** (Cloudflare free tier, 10,000 neurons/day) |
| final | **$0.040** (fal flux-pro/kontext) |
| photo analysis | ~$0.004, cached per photo |
| visual judge | ~$0.001 per final |
| **typical session** (6 previews + 2 finals) | **~$0.09** |

### Balance warnings

Neither service publishes a remaining-credit endpoint — both announce it by
failing the next call. So **Ajustes → Tu saldo** counts down from what she
tells it she topped up, using spending recorded to the cent.

**She must tap the top-up amount after paying**, or the countdown has nothing
to count from. It warns at 25% and again at 10%, and **stops generating** when
a service is empty rather than failing one call at a time.

### Caps

`$1.50` per session, `$10.00` per day, enforced before each paid call and
surviving a restart. Change in `.env` (`CAP_PER_SESSION_USD`,
`CAP_PER_DAY_USD`).

---

## 4. The quality gate

Nothing reaches her that has not passed here. Verdicts are **PASS / FAIL /
UNKNOWN**, and UNKNOWN is never quietly upgraded — a check that could not run
has not passed.

| check | what it measures | state |
|---|---|---|
| exposure, sharpness | is the image usable | working |
| **identity** | is it actually her (ArcFace) | working |
| **proportions** | anti-slimming | working, with a caveat below |
| **eyes** | placement and alignment | working |
| **hands** | located and confident | working |
| skin tone | CIELAB against her reference | working |
| containment | nothing changed outside the request | working |
| judge | a vision model looks at it | working |

**Previews propose, finals deliver.** A preview is never discarded for a
resemblance judgement — only for a broken image. Otherwise nothing would
survive to choose from, and the stage that *can* preserve her would never be
reached. Every measurement still runs and still orders the grid.

### Two honest limits

**The proportions check refuses cross-framing comparisons.** Width-over-length
ratios move with camera distance: her own photographs vary 2.8–8.2% within one
framing and 19.2% across framings. When the generator reframes, the check
returns UNKNOWN rather than a number that looks like evidence. The threshold is
**15%**, calibrated against her own measured spread of 3.7–13.3% — a coarse
backstop against gross reshaping, not a precision instrument.

**Scene-changing looks lose her.** Measured against her centroid (her own
photos score 0.83–0.87, threshold 0.45):

```
change only the clothing        identity 0.703   it is her
change scene + pose + clothing  identity 0.088   a stranger
```

This is why `STRICT_GATE=off` in `.env`. The gate is not wrong — those looks
genuinely do not preserve her. Turning strictness on is safe once the
scene-change looks are tuned to hold identity; that is a prompt-and-strength
problem, not a gate problem.

---

## 5. The catalog

**21 looks, 17 offered, 4 withheld** by the coverage policy.

Coverage is enforced in three layers: exposing wording is rewritten out of the
prompt (Spanish *and* English), 52 negatives are attached, and the visual judge
independently rejects all six exposure conditions.

**To restore the 4 withheld looks at handover**, one line in `.env`:

```ini
COVERAGE_POLICY=off
```

They are withheld, never edited — the authored looks survive intact.

### Adding a look

One JSON file in `catalog/`. Add a cover with
`python scripts/make_covers.py` (free, ~1,200 neurons for all of them).
Seeds derive from the look id, so `--force` reproduces the same covers rather
than redecorating the catalog.

---

## 6. Her profile

Built from `input/Nayane` — 24 photographs.

```
identity centroid   24 photos, dispersion 0.150, threshold 0.545
proportions         hips visible in 10, ankles in 2
skin reference      set
```

Rebuild after adding photos:

```powershell
C:\estudio\python\python.exe C:\estudio\scripts\build_profile.py "C:\estudio\input\Nayane"
```

**What more photographs would buy:** only the proportion baseline, and it
already has enough. They would **not** improve resemblance — her face reaches
each image through the single photo she uploads that session, not through the
profile.

---

## 7. When something is wrong

| symptom | cause | fix |
|---|---|---|
| "No he podido empezar" | no provider configured | check `.env` keys; the alert now names the reason |
| Photos look like a stranger | scene-change look | use a clothing-change look, or tune the prompt |
| "0 photos" after a session | gate discarded them | read the per-check report in the logs |
| Settings changes do nothing | app not restarted | restart, elevated |
| Broken image icons | derivatives missing | they rebuild on demand; check `data/derivatives` |
| HTTP 401 from a provider | wrong key in the slot | the app warns on shape and duplication at startup |

**Logs:** `C:\estudio\logs\uvicorn.out.log` — provider failures are printed
there as well as shown on screen.

**Health:** `http://127.0.0.1:8000/health` lists every degraded subsystem.

---

## 8. The three sites on this machine

One nginx at `C:\nginx` fronts all three. It is owned by **`Proxy-Vigilante`**,
not by any single project — removing one project's task must not take the
others down.

| | port | watchdog |
|---|---|---|
| crypto-radar | 3000 | `CryptoRadar-Vigilante` |
| kind-chatbot | 3010 | `Asistente-Vigilante` |
| wonderimg | 8000 | `Estudio-Vigilante` |

Certificates renew via `Certificados-Renovar` (04:05) and nginx reloads at
04:30 so a renewed certificate is actually served.

**Two known issues, both documented in scripts:**

- **crypto-radar will not survive an unattended reboot** — no boot trigger and
  it runs `Interactive`. Fix: `deploy\windows\arreglar-radar-arranque.ps1`
  (backs up first, reverts with `-Restore`). It belongs to another project, so
  it is opt-in.
- **A fourth project (a Next.js portfolio) also binds :3000.** crypto-radar
  wins today because it binds `127.0.0.1` specifically. If it ever stops, the
  portfolio inherits the port and nginx serves the wrong site — and the
  radar's watchdog checks only "is the port busy", so it would never restart.

---

## 9. Still open

**insightface pins an uninstallable version.** `requirements-cv.txt` pins
`0.7.3`, which has no Windows wheel. `1.0.1` installs and is API-compatible —
that is what is running. Update the pin.

**Multi-tenancy** — the client called it fundamental. Designed, not built.

**Scene-change looks lose identity** — the largest open quality item, and the
one blocking `STRICT_GATE=on`.

---

## 10. Configuration reference

`C:\estudio\.env` — never committed; `.env.example` is the template.

```ini
ACCESS_TOKEN=        # her login. rotating it invalidates every session
SECRET_KEY=          # session signing
ANTHROPIC_API_KEY=   # judge + analyser. starts sk-ant-
FAL_API_KEY=         # finals. contains a colon: id:secret
CF_ACCOUNT_ID=       # 32 hex chars
CF_API_TOKEN=        # free previews
COVERAGE_POLICY=full # 'off' restores the 4 withheld looks
STRICT_GATE=off      # see section 4
PREVIEW_COUNT=6      # overridden by the Ajustes setting
```

**No trailing comments on a value.** The parser strips quotes and whitespace
but not a `#`, so `FAL_API_KEY=abc  # note` sets the key to `abc  # note` — it
is not empty, so every "is it configured?" check passes and the first paid call
returns 401. The app checks shape and duplication at startup and will tell you.

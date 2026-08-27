# Estudio

A photo production robot. She uploads a photo, style options appear, she marks
what she wants, six options come back one by one, she taps the keepers and they
arrive finished.

One button, a few taps, under two minutes. Nothing typed, nothing reviewed for
defects, nothing corrected by hand.

Full design rationale: [`RECOMMENDED-METHOD-mobile-zero-cost.txt`](RECOMMENDED-METHOD-mobile-zero-cost.txt).

---

## Run it now, with no keys and no spend

```bash
cp .env.example .env
pip install -r requirements.txt
python scripts/make_icons.py
python scripts/seed_catalog.py
uvicorn app.main:app --reload
```

Open <http://127.0.0.1:8000/e/anything> — with `ACCESS_TOKEN` unset the magic
link accepts any token — and the whole journey works against a local mock
generator that costs nothing.

The mock produces placeholder cards, not photographs. That is the point: it
exercises every stage of the real pipeline so the orchestration can be seen and
clicked before a euro is spent.

```bash
pytest          # 320 tests, no network, no keys
```

---

## What actually happens when she taps

```
upload ──► PHOTO ROUTER ──► is she in this photo?
                             yes → analyse as source
                             no  → ask what to take from it, make it a chip
             │
             ▼
          ANALYSER (opus 5, once per photo) ──► AttributeIR
             │
             ▼
          PROPOSAL ENGINE ──► the styles that suit THIS photo
             │
        she marks rows (none / one / several)
             │
             ▼
   ┌─── STAGE A · PREVIEWS ──────────────── ~25 s, cheap ───┐
   │  combination walk → 6 concurrent generations           │
   │  FREE CPU SCREEN: identity, proportions, hands, sanity │
   │  failures replaced silently, no paid call              │
   │  each tile pushed over SSE the moment it lands         │
   └────────────────────────────────────────────────────────┘
             │
        she picks the keepers
             │
   ┌─── STAGE B · FINALS ─────────────── ~60 s, quality ────┐
   │  derived from the chosen preview, same provider        │
   │  FULL GATE + visual judge (haiku 4.5)                  │
   │  localised defect → silent region inpaint → re-QA      │
   └────────────────────────────────────────────────────────┘
             │
             ▼
        PREFERENCE LEARNER ── her taps, both halves
```

**The structural saving.** The expensive model, the paid judge and the repair
loop never run on an image she did not choose. That is where roughly a third of
the per-photo cost goes, and it is not a trick — it is where selection sits in
the pipeline.

---

## The idea worth understanding

Every attribute row is multi-select, and **how many she picks is the whole
instruction**:

| She selects | The robot does |
|---|---|
| nothing | varies it freely across the six |
| one | fixes it — identical in all six |
| several | varies across **exactly those**, nothing else |

She is not choosing a style. She is describing a *space*, and the previews are
drawn from inside it. *"The dress and the casual outfit, standing and walking,
smiling softly"* is four taps, and it is a photo shoot brief.

The walk through that space uses a stride coprime to the space size, so a batch
is never six variations of one corner, and `[Otras 6]` **continues** rather than
repeating what she already rejected.

---

## Layout

```
app/
  contracts/      the four contracts everything crosses
  compile/        combination walk + prompt compiler
  providers/      fal + replicate adapters, registry, cost-aware bandit router
  gate/           the quality gate — the only approver, incl. anti-slimming
  orchestrator/   two-stage engine + SSE event bus
  analysis/       photo → AttributeIR, and "is she in this photo?"
  profile/        identity centroid, proportion baseline, coverage
  templates/      six screens, no more
  static/         one stylesheet, one service worker
catalog/          the look catalog — data, not code
scripts/          profile builder, catalog seed, icons
tests/            320 tests
```

### The four contracts

No module anywhere references an image provider by name. The router selects on
declared capability, so adding one is an adapter plus a descriptor, and swapping
one is a config edit.

- **`LookRecipe`** — a catalog entry: recipe, `applies_to`, axes, locks, chips
- **`AttributeIR`** — subject and scene; what is locked, what may move
- **`QAReport`** — verdict, per-check measurements, defects with boxes
- **`ProviderDescriptor`** — capabilities, resolution, cost, latency, priors

---

## Two rules the code keeps

**UNKNOWN is not PASS.** The gate reports three states. A check that could not
run — no model installed, no face found, judge errored — blocks the image
rather than waving it through. The gate is the only approver of finals; there
is no human behind it, so "we could not check" must never resolve to "fine".

**A half-configured system announces itself.** Missing keys, absent CV models
and an open access token are listed at boot, on `/health`, and on the settings
screen in her language. She should never be the one to discover that identity
verification was switched off.

Right now, without the pose model installed, it will tell you this:

```
AVISO: verificacion de proporciones y manos no disponible —
       EL CONTROL ANTI-ADELGAZAMIENTO NO ESTA ACTIVO
```

---

## The anti-slimming check

She told us an earlier tool had made her look thinner without being asked.
Every generator is instructed, on every single call, not to. **Words in a
prompt are a request; [`app/gate/pose.py`](app/gate/pose.py) is the
enforcement.**

Body keypoints are measured in the generated image and compared against a
baseline built from her real photos. Every measurement is **a ratio against
torso length**, which is what makes it work: a generated image is a different
crop, zoom and pose, so raw pixel distances are meaningless — but ratios
against the torso are invariant to all three. A change in one is a change in
*her*, not in the framing.

One subtlety worth knowing, because the obvious measurement is blind to the
actual complaint:

| Measurement | Catches |
|---|---|
| `shoulder_hip_ratio` | disproportionate reshaping — waist taken in, hips widened |
| `shoulder_torso_ratio`, `hip_torso_ratio` | **uniform slimming** — the whole body narrowed |

Shoulder-over-hip is a ratio of two widths, so when a generator narrows the
whole body *both terms shrink and the ratio barely moves*. Narrowing her by
15% shifts it about 2%. Width measured against torso **length** — which
slimming does not change — moves by the full 15%. Only the second pair
actually catches what she complained about.

The threshold is 6% drift, and the check is symmetric: an unrequested change
is unrequested in either direction. When it fires it names what moved —
*"anchura de caderas: más estrecho (−15%)"* — because "proportions changed"
is not something she can agree or disagree with.

The same keypoints locate her hands, which gives the repair loop a region to
repaint. It does **not** count fingers — COCO-17 has wrists, not digits — and
whether a hand is broken is the visual judge's call on finals. A pixel metric
cannot tell six fingers from five, and a language model asked to measure a
body will agree with whatever it is shown. Each is used for what it is good
at.

---

## Going live

### 1. The quality gate

```bash
pip install -r requirements-cv.txt
python scripts/fetch_models.py        # buffalo_l + yolov8n-pose
python scripts/build_profile.py /path/to/her/photos
```

`build_profile.py` prints the coverage report — how many photos were usable, and
whether there are enough **full-body** shots. That matters twice over: it decides
whether a LoRA is viable, and it is the only source of the proportion baseline.

### 2. Keys

```bash
python -c "import secrets; print(secrets.token_urlsafe(32))"   # ×2
```

Set `SECRET_KEY` and `ACCESS_TOKEN` in `.env`. Her private link is then
`https://your-domain/e/<ACCESS_TOKEN>` — sent once, opened once.

Add `ANTHROPIC_API_KEY` for the real analyser and judge.

### 3. Image providers

Two adapters ship — **fal.ai** and **Replicate** — configured in
[`providers.json`](providers.json), which holds model ids, capabilities and
prices. Set `FAL_API_KEY` or `REPLICATE_API_TOKEN` and the matching entries
register themselves; entries without a key are skipped silently, so the file
can list everything and the machine runs whatever it has keys for.

Two adapters rather than one is deliberate. *"No quiero quedar atada a una
única API"* is only credible if it is demonstrable, so swapping provider is
editing `enabled` in a config file, and the DoD item "provider swap
demonstrated live" is a thing you can actually do in front of her.

**Before generating a real batch:**

```bash
python scripts/check_provider.py
```

One real call per provider, a few cents, printing the request, the response
and any failure verbatim. The adapters are unit-tested against a mock
transport — request building, response parsing, retry policy, the API key
never reaching the CDN — but a mock cannot prove a live service accepts these
bodies. Model input schemas differ between models and change between versions.
Find that out for a few cents, not by watching a batch die after paying for
every image.

Endpoints, auth schemes and model paths are verified against both live
services. What is not yet verified is whether each model accepts these exact
input fields.

### 4. Deploy

```bash
DOMAIN=your-domain docker compose up -d --build
```

Caddy obtains and renews the certificate on its own. No certbot, no cron job.

### 5. Watch it

Point a free uptime monitor at `/health`. Self-hosting introduced a failure mode
a chat bot never had: the site can go down and nobody notices.

---

## Still to do before this is deliverable

Honest list, not a roadmap.

- **The catalog.** 9 seed looks; the target is 15–20 hand-authored. This is the
  manual professional work and it is what she is actually buying — weak recipes
  make a weak product no matter how good the pipeline is.
- **Preview fidelity, measured.** Stage B does img2img from the accepted preview
  at low denoise, which makes drift structurally impossible. The alternative —
  same seed re-rendered at full resolution — has a higher quality ceiling but
  varies by provider. Measure ~20 pairs before choosing.
- **Threshold calibration.** Every number in `Thresholds` is a placeholder until
  fitted to her own photos with her judging ~40 borderline candidates. That hour
  of her time is the one the project genuinely needs.
- **Deferred to phase 2:** wardrobe, voice input, standing orders.

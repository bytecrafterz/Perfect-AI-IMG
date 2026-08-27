# syntax=docker/dockerfile:1
#
# One image, built for linux/arm64 (Oracle Cloud Always Free).
#
# The CV layer is a separate, optional build stage: the app runs without it,
# reports honestly that identity verification is off, and adding it later is a
# rebuild rather than a redesign.  Keeping it optional also keeps the default
# image small enough to rebuild quickly on a free-tier machine.

FROM python:3.11-slim-bookworm AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# libheif is what lets Pillow open iPhone photos.  Without it every HEIC
# upload fails, which on her phone is most of them.
RUN apt-get update && apt-get install --no-install-recommends -y \
      libheif1 \
      libjpeg62-turbo \
      libwebp7 \
      curl \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt


# --- optional: the quality gate's computer vision ---------------------------
# Build with:  docker build --target cv -t estudio:cv .
FROM base AS cv
COPY requirements-cv.txt .
RUN apt-get update && apt-get install --no-install-recommends -y \
      libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/* \
    && pip install -r requirements-cv.txt


# --- runtime ----------------------------------------------------------------
FROM base AS runtime

COPY app/ ./app/
COPY catalog/ ./catalog/
COPY scripts/ ./scripts/

# Runs unprivileged.  The data volume is chowned so the app can write uploads,
# derivatives and the database without running as root.
RUN useradd --system --uid 10001 --home /srv estudio \
    && mkdir -p /srv/data \
    && chown -R estudio:estudio /srv
USER estudio

ENV DATA_DIR=/srv/data \
    CATALOG_DIR=/srv/catalog

EXPOSE 8000

# The uptime monitor hits this too - self-hosting introduced a failure mode a
# chat bot never had: the site can go down and nobody notices.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://127.0.0.1:8000/health || exit 1

# --proxy-headers so the app can tell whether the browser actually reached
# Caddy over TLS, which decides whether the session cookie is marked Secure.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]

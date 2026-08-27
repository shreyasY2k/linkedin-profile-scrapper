# Pinned 2026-08-27: python:3.13-slim-trixie publishes a native linux/arm64
# manifest, matching both the Apple Silicon dev machine and the Oracle Ampere
# A1 target. Do not add a `platform:` anywhere — QEMU is not wanted.
FROM python:3.13-slim-trixie AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# Dependency layer first: source edits do not re-resolve the wheel set.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Non-root from here on. Fixed uid so bind-mounted files behave predictably.
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid 10001 --no-log-init --create-home app

COPY --chown=app:app app ./app
COPY --chown=app:app tests ./tests

USER app

EXPOSE 8000

# uvicorn imports app.main, which imports app.config: a missing or blank
# required variable aborts here, non-zero, naming the field.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]


# Test image. Built only on demand (`docker compose build --build-arg` is not
# needed — use `docker build --target test .`), so pytest and httpx never enter
# the image that ships.
FROM base AS test
USER root
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
# Dummy values only; lets tests assert .env.example covers every Settings field.
COPY .env.example ./
USER app
# no:cacheprovider — /srv is root-owned and pytest's cache is worthless here.
CMD ["pytest", "-q", "-p", "no:cacheprovider"]


# Default target: keep the runtime stage last so a plain `docker build .` and
# compose's `build: .` both produce the shipping image.
FROM base AS runtime

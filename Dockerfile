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

# Application source only. Test sources are added in the `test` stage below, so
# they never reach the image that ships.
COPY --chown=app:app app ./app

USER app

EXPOSE 8000

# uvicorn imports app.main, which imports app.config: a missing or blank
# required variable aborts here, non-zero, naming the field.
#
# --proxy-headers / --forwarded-allow-ips: story 2 puts host nginx in front,
# terminating TLS. Without these the app reads the proxy's IP as the client and
# builds http:// URLs for an https:// request. Keycloak's equivalent is
# KC_PROXY_HEADERS; this is the API's. The allow-list is "*" because the only
# thing that can reach this container is host nginx over the compose network —
# the published port binds 127.0.0.1, so no untrusted client can forge headers.
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*"]


# Test image. Built only on demand (`docker build --target test .`), so pytest,
# httpx2 and the test sources never enter the image that ships.
FROM base AS test
USER root
COPY requirements-dev.txt ./
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY --chown=app:app tests ./tests
# Dummy values only. The contract tests read both files to prove .env.example
# documents every Settings field and every ${VAR} compose interpolates.
COPY --chown=app:app .env.example docker-compose.yml ./
USER app
# no:cacheprovider — /srv is root-owned and pytest's cache is worthless here.
CMD ["pytest", "-q", "-p", "no:cacheprovider"]


# Default target: keep the runtime stage last so a plain `docker build .` and
# compose's `build: .` both produce the shipping image.
FROM base AS runtime

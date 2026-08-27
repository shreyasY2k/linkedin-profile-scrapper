"""The story-1 edge-case matrix, as tests.

| Scenario       | Expected                                                    |
|----------------|-------------------------------------------------------------|
| Liveness       | ``GET /health`` -> ``200 {"status": "ok"}``                  |
| Missing config | the process dies at import, non-zero, naming the field       |
| Env contract   | ``.env.example`` documents every variable anything reads     |
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1 import router as v1_router
from app.config import Settings
from app.main import create_app
from tests.conftest import REQUIRED_ENV

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(name="client")
def _client() -> TestClient:
    return TestClient(create_app())


# --- Liveness ---------------------------------------------------------------


def test_health_returns_ok(client: TestClient) -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_needs_no_authorization(client: TestClient) -> None:
    """The container healthcheck has no token to present."""
    assert client.get("/health", headers={}).status_code == 200


def test_health_status_is_a_closed_enum(client: TestClient) -> None:
    """Story 9 ships this document; it must name the only legal value."""
    schema = client.get("/openapi.json").json()["components"]["schemas"][
        "HealthResponse"
    ]["properties"]["status"]

    # pydantic renders a single-value Literal as `const`; tolerate `enum` in
    # case that rendering changes.
    legal = {schema["const"]} if "const" in schema else set(schema["enum"])
    assert legal == {"ok"}


# --- The /api/v1 seam -------------------------------------------------------


def test_v1_router_carries_the_prefix_response_schema_fixes() -> None:
    assert v1_router.prefix == "/api/v1"


def test_every_v1_route_lives_under_the_prefix() -> None:
    """Survives stories 5-8 adding routes; an unprefixed one fails here."""
    for route in v1_router.routes:
        assert route.path.startswith("/api/v1"), route.path


def test_v1_seam_is_mounted_on_the_built_app() -> None:
    """Deleting `include_router(v1.router)` from create_app() must fail a test.

    The seam has no routes of its own yet, so mounting it is invisible in
    `app.routes`. Attach a probe route, build the app, and call it.
    """
    probe = APIRouter()

    @probe.get("/__mount_probe__")
    async def _probe() -> dict[str, bool]:
        return {"mounted": True}

    v1_router.include_router(probe)
    try:
        client = TestClient(create_app())
        response = client.get("/api/v1/__mount_probe__")
    finally:
        v1_router.routes[:] = [
            route
            for route in v1_router.routes
            if getattr(route, "path", "") != "/api/v1/__mount_probe__"
        ]

    assert response.status_code == 200
    assert response.json() == {"mounted": True}


def test_openapi_document_is_generated(client: TestClient) -> None:
    """Story 9 uses this document as the README's API documentation."""
    document = client.get("/openapi.json").json()

    assert document["info"]["title"] == "LinkedIn Profile API"
    assert "/health" in document["paths"]


# --- Missing configuration --------------------------------------------------


@pytest.mark.parametrize("missing", sorted(REQUIRED_ENV))
def test_settings_rejects_an_unset_required_variable(
    monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv(missing, raising=False)

    with pytest.raises(ValidationError) as caught:
        # _env_file=None: a developer's real .env must not rescue this test.
        Settings(_env_file=None)

    assert missing.lower() in str(caught.value).lower()


@pytest.mark.parametrize("blank", ["", "   ", "\t", "\n  \n"])
@pytest.mark.parametrize("variable", sorted(REQUIRED_ENV))
def test_settings_rejects_a_blank_required_variable(
    monkeypatch: pytest.MonkeyPatch, variable: str, blank: str
) -> None:
    """`FOO=` and `FOO="   "` are both as broken as `FOO` being absent.

    Whitespace-only matters on its own: it satisfies a bare `min_length=1`, so
    without the strip in `app.config.RequiredSetting` a service would boot with
    an all-spaces encryption key and only fail once it tried to use it.
    """
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(variable, blank)

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)

    assert variable.lower() in str(caught.value).lower()


def test_settings_strips_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A trailing space in a .env line must not end up inside the value."""
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("KEYCLOAK_REALM", "  test-realm  ")

    assert Settings(_env_file=None).keycloak_realm == "test-realm"


def _import_app_main(env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    """Run `python -c "import app.main"` the way the container's CMD does.

    `cwd` is a scratch directory rather than the repo root, and the repo reaches
    the interpreter through PYTHONPATH instead. That matters: `Settings` reads
    `env_file=".env"` relative to the working directory, so running from the
    repo root would let a developer's real `.env` rescue a test that exists to
    watch the process die.
    """
    return subprocess.run(
        [sys.executable, "-c", "import app.main"],
        env={**env, "PYTHONPATH": str(REPO_ROOT)},
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_importing_the_app_dies_when_a_required_variable_is_missing(
    tmp_path: Path,
) -> None:
    """The guarantee the container actually depends on.

    Constructing `Settings(...)` by hand proves the model validates. It does not
    prove the *boot path* enforces it — that depends on `Settings()` running at
    `app.config` module scope. Shell out exactly as the container's CMD does.
    """
    # A scrubbed environment: nothing ambient on the developer's machine is
    # inherited, so only what is listed here can satisfy Settings.
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        **REQUIRED_ENV,
    }
    del env["SESSION_ENCRYPTION_KEY"]

    completed = _import_app_main(env, tmp_path)

    assert completed.returncode != 0, completed.stdout
    assert "session_encryption_key" in completed.stderr.lower(), completed.stderr


def test_importing_the_app_succeeds_with_a_complete_environment(
    tmp_path: Path,
) -> None:
    """The negative test above must be failing for the right reason."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
        **REQUIRED_ENV,
    }

    completed = _import_app_main(env, tmp_path)

    assert completed.returncode == 0, completed.stderr


def test_settings_accepts_a_complete_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)

    settings = Settings(_env_file=None)

    assert settings.keycloak_realm == REQUIRED_ENV["KEYCLOAK_REALM"]


def test_settings_has_no_environment_name_to_branch_on() -> None:
    """No APP_ENV, by design: nothing can branch on an environment name."""
    fields = set(Settings.model_fields)

    assert not {"app_env", "environment", "env", "stage"} & fields


# --- The env contract -------------------------------------------------------


def _env_example_assignments() -> set[str]:
    """Variable names assigned in `.env.example`, ignoring comment lines."""
    path = REPO_ROOT / ".env.example"
    # A hard assertion, not a skip: if a .dockerignore or Dockerfile edit stops
    # shipping this file, the contract tests below must go red rather than
    # silently turning into green skips.
    assert path.is_file(), f"{path} is missing — the env contract is unverified"

    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", stripped)
        if match:
            names.add(match.group(1))
    return names


def _compose_interpolations() -> set[str]:
    """Variable names `docker-compose.yml` interpolates, ignoring comments."""
    path = REPO_ROOT / "docker-compose.yml"
    assert path.is_file(), f"{path} is missing — the env contract is unverified"

    names = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        # ${VAR}, ${VAR:?msg}, ${VAR:-default}, ${VAR-default}. `$$VAR` is a
        # compose escape for a literal $, not an interpolation, and has no
        # brace form, so this pattern cannot match it.
        names.update(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)[:\-?}]", stripped))
    return names


def test_env_example_documents_every_setting() -> None:
    """`.env.example` is the env contract stories 3-8 extend."""
    documented = _env_example_assignments()

    for field in Settings.model_fields:
        assert field.upper() in documented, (
            f"{field.upper()} is read by Settings but missing from .env.example"
        )


def test_env_example_documents_every_compose_variable() -> None:
    """Compose-only variables are part of the contract too.

    POSTGRES_* and KEYCLOAK_ADMIN_* never reach `Settings`, so the test above
    cannot see them — yet an undocumented one makes `docker compose up` fail on
    a clean clone, which is acceptance criterion one.
    """
    documented = _env_example_assignments()
    interpolated = _compose_interpolations()

    assert interpolated, "parsed no ${VAR} interpolations — the parser is broken"

    missing = sorted(interpolated - documented)
    assert not missing, f"interpolated by compose but absent from .env.example: {missing}"

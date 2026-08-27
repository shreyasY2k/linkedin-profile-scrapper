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
from app.auth import require_claims
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


#: Routes that legitimately live outside `/api/v1`: story 1's liveness probe,
#: plus the documentation routes FastAPI mounts itself.
UNVERSIONED_PATHS = frozenset(
    {"/health", "/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"}
)


def _mounted_paths(routes: list, prefix: str = "") -> list[str]:
    """Every route path actually mounted, reconstructed with its full prefix.

    Since FastAPI 0.141 `include_router` no longer copies routes: it leaves a
    lazy `_IncludedRouter` marker carrying the sub-router and the prefix it was
    mounted under. Walking that marker is the only way to see routes attached
    through a sub-router — and `include_in_schema=False` routes, which never
    appear in the OpenAPI document at all.
    """
    paths: list[str] = []
    for route in routes:
        context = getattr(route, "include_context", None)
        if context is not None:
            paths.extend(
                _mounted_paths(context.included_router.routes, prefix + context.prefix)
            )
            continue
        path = getattr(route, "path", None)
        if path is not None:
            paths.append(prefix + path)
    return paths


def test_every_v1_route_lives_under_the_prefix() -> None:
    """Survives stories 5-8 adding routes; an unprefixed one fails here.

    Walks the assembled app's routes rather than the OpenAPI document. The
    document would miss exactly the route most likely to escape scrutiny — one
    declared `include_in_schema=False` — and would pass vacuously if the paths
    dict were ever empty. The non-emptiness assertion below closes the second
    hole; the walk closes the first.
    """
    paths = _mounted_paths(create_app().routes)

    assert paths, "walked no routes at all — the walk is broken, not the app"
    for path in paths:
        assert path in UNVERSIONED_PATHS or path.startswith("/api/v1"), path


def test_the_route_walk_sees_more_than_the_openapi_document(
    client: TestClient,
) -> None:
    """Guards the guard: a walk that silently saw nothing would pass above."""
    walked = set(_mounted_paths(create_app().routes))
    documented = set(client.get("/openapi.json").json()["paths"])

    assert documented, "the OpenAPI document has no paths"
    assert documented <= walked, sorted(documented - walked)
    assert "/openapi.json" in walked and "/openapi.json" not in documented


def test_v1_seam_is_mounted_on_the_built_app() -> None:
    """Deleting `include_router(v1.router)` from create_app() must fail a test.

    The seam has no routes of its own yet, so mounting it is invisible in
    `app.routes`. Attach a probe route, build the app, and call it.

    Since story 3 the seam also carries bearer validation, which would answer
    401 before the probe ran. The dependency is overridden rather than
    satisfied with a real token: this test is about *mounting*, and
    `tests/test_auth.py` is where the validation itself is exercised.
    """
    probe = APIRouter()

    @probe.get("/__mount_probe__")
    async def _probe() -> dict[str, bool]:
        return {"mounted": True}

    # Snapshot/restore rather than filtering by path: since FastAPI 0.141 an
    # `include_router` leaves an `_IncludedRouter` marker that carries no
    # `path`, so a path-based filter would leak the probe into every later
    # test in the session.
    saved = list(v1_router.routes)
    v1_router.include_router(probe)
    try:
        application = create_app()
        application.dependency_overrides[require_claims] = lambda: {"sub": "probe"}
        client = TestClient(application)
        response = client.get("/api/v1/__mount_probe__")
    finally:
        v1_router.routes[:] = saved

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

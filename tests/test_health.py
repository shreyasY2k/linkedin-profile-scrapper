"""The story-1 edge-case matrix, as tests.

| Scenario       | Expected                                                  |
|----------------|-----------------------------------------------------------|
| Liveness       | ``GET /health`` -> ``200 {"status": "ok"}``                |
| Missing config | ``Settings`` raises, and the error names the missing field |
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.v1 import router as v1_router
from app.config import Settings
from app.main import create_app
from tests.conftest import REQUIRED_ENV


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


# --- The /api/v1 seam -------------------------------------------------------


def test_v1_router_carries_the_prefix_response_schema_fixes() -> None:
    assert v1_router.prefix == "/api/v1"


def test_v1_seam_is_empty_for_now() -> None:
    """Stories 5-8 own the routes; story 1 only carves the seam."""
    assert v1_router.routes == []


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


@pytest.mark.parametrize("blank", sorted(REQUIRED_ENV))
def test_settings_rejects_a_blank_required_variable(
    monkeypatch: pytest.MonkeyPatch, blank: str
) -> None:
    """`FOO=` in a .env file is just as broken as `FOO` being absent."""
    for name, value in REQUIRED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv(blank, "")

    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None)

    assert blank.lower() in str(caught.value).lower()


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


def test_env_example_documents_every_setting() -> None:
    """`.env.example` is the env contract stories 3-8 extend."""
    from pathlib import Path

    example = Path(__file__).resolve().parents[1] / ".env.example"
    if not example.is_file():
        pytest.skip(".env.example is not shipped inside the runtime image")

    text = example.read_text(encoding="utf-8")
    for field in Settings.model_fields:
        assert f"{field.upper()}=" in text, f"{field.upper()} missing from .env.example"

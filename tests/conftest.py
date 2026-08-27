"""Test environment.

The environment is populated *before* ``app.config`` is imported anywhere,
because importing that module is the configuration read — a test session
started with an incomplete environment would fail at collection instead of
inside the test that is supposed to observe the failure.
"""

from __future__ import annotations

import os
import socket
from typing import Any

import pytest

from cryptography.fernet import Fernet

#: A real Fernet key, generated per session rather than written down.
#:
#: Story 5 makes ``SESSION_ENCRYPTION_KEY`` a *validated* Fernet key: importing
#: ``app.vault`` builds the cipher, so a key that is merely a non-empty string
#: kills the process at boot — which is the contract, and which the old
#: ``"test-encryption-key"`` placeholder would trip on every import.
#:
#: Generated rather than committed for two reasons. No key material enters the
#: repository, so gitleaks has nothing to find and nobody can copy a "known
#: good" key into a deployment; and every session encrypts under a different
#: key, so a test that accidentally depended on a fixed one fails immediately
#: rather than on someone else's machine.
SESSION_ENCRYPTION_KEY = Fernet.generate_key().decode()

#: The complete env surface ``Settings`` reads.
#:
#: Values are assigned unconditionally, never via ``setdefault``: a developer
#: with a real ``DATABASE_URL`` or ``KEYCLOAK_*`` exported in their shell would
#: otherwise have it silently win, and the suite would quietly be exercising
#: their live configuration instead of these fixtures.
REQUIRED_ENV = {
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "KEYCLOAK_SERVER_URL": "http://localhost:8080",
    # Deliberately different from KEYCLOAK_SERVER_URL: the suite must fail if
    # the issuer is ever derived from the in-network address, which is the bug
    # that would make every deployed token look foreign.
    "KEYCLOAK_ISSUER_URL": "https://issuer.example.test",
    "KEYCLOAK_REALM": "test-realm",
    "KEYCLOAK_CLIENT_ID": "test-client",
    "KEYCLOAK_CLIENT_SECRET": "test-client-secret",
    "SESSION_ENCRYPTION_KEY": SESSION_ENCRYPTION_KEY,
}

for _name, _value in REQUIRED_ENV.items():
    os.environ[_name] = _value


# Import only after the environment is complete.
from app.config import Settings  # noqa: E402

# A field added to `Settings` without a matching REQUIRED_ENV entry would lose
# every missing/blank-variable assertion in `tests/test_health.py` without
# failing anything. Assert the two stay in lockstep at collection time, where
# it cannot be missed.
#
# The lockstep is against the REQUIRED fields specifically. Story 4 introduced
# the contract's one optional field (the developer's LinkedIn session, used
# only by the opt-in live check), and putting an optional field in REQUIRED_ENV
# would be worse than leaving it out: the parametrised "unset variable must
# fail validation" tests would assert that deleting it kills the process, which
# is exactly the behaviour it exists NOT to have.
_declared = set(Settings.model_fields)
_required = {name for name, f in Settings.model_fields.items() if f.is_required()}
_optional = _declared - _required
_covered = {name.lower() for name in REQUIRED_ENV}

assert _covered == _required, (
    "REQUIRED_ENV is out of sync with Settings' required fields: "
    f"missing from REQUIRED_ENV={sorted(_required - _covered)}, "
    f"stale in REQUIRED_ENV={sorted(_covered - _required)}"
)
assert not (_covered & _optional), (
    "REQUIRED_ENV lists an OPTIONAL field, which would make the "
    f"missing-variable tests assert the opposite of the contract: {sorted(_covered & _optional)}"
)

#: Optional fields, asserted explicitly rather than implied by the difference
#: above — so that making a required field optional (which silently weakens
#: every boot-time guarantee) has to be a deliberate edit here.
OPTIONAL_SETTINGS = {"linkedin_dev_cookie"}
assert _optional == OPTIONAL_SETTINGS, (
    f"Settings' optional fields changed: {sorted(_optional)}. "
    "Every optional field weakens the fail-at-boot contract — justify it at "
    "the field and update OPTIONAL_SETTINGS."
)


def pytest_configure(config: object) -> None:
    """Register the `live` marker.

    The one live check is skipped by default (see
    `tests/test_linkedin_live.py`), so CI and a grader running `pytest` never
    reach LinkedIn. The marker exists so it can also be *selected* deliberately
    with `-m live`, and so an unregistered-marker warning does not train
    everyone to ignore warnings.
    """
    config.addinivalue_line(  # type: ignore[attr-defined]
        "markers",
        "live: hits the real LinkedIn API. Skipped unless LINKEDIN_LIVE_CHECK=1.",
    )
    config.addinivalue_line(  # type: ignore[attr-defined]
        "markers",
        "postgres: hits a real Postgres. Skipped unless POSTGRES_LIVE_CHECK=1.",
    )


# --- No test reaches a real network -------------------------------------------
#
# The suite has to pass under `docker run --network none`, and asserting that in
# CI is not the same as noticing when it stops being true: a test that silently
# starts talking to the internet passes everywhere the internet is reachable.
#
# It happened. `VoyagerClient`'s transport default was bound at `def` time, so a
# test that replaced `app.linkedin.client.urllib_transport` on the module was
# never calling its substitute — and had been fetching the real linkedin.com,
# from the offline suite, for two stories. Nothing failed; the live response
# simply classified as the code the assertion expected.
#
# So the guard is structural rather than a convention. Any attempt to open a
# socket fails the test that made it, naming what it tried to reach. The two
# opt-in live checks are exempted by their markers, because reaching a real
# service is the entire point of those.

#: Markers whose tests are *supposed* to use the network.
LIVE_MARKERS = frozenset({"live", "postgres"})


class NetworkUseInTests(RuntimeError):
    """A test tried to open a real connection."""


@pytest.fixture(autouse=True)
def _no_real_network(request: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that opens a socket, unless it is a marked live check."""
    if LIVE_MARKERS & set(request.node.keywords):
        return

    def refuse(*args: Any, **kwargs: Any) -> Any:
        target = args[1] if len(args) > 1 else kwargs.get("address", args[:1])
        raise NetworkUseInTests(
            f"this test tried to open a real connection to {target!r}. The suite "
            "runs under `--network none` and must not depend on anything "
            "outside the process: inject the transport, or mark the test `live`."
        )

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

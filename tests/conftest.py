"""Test environment.

The environment is populated *before* ``app.config`` is imported anywhere,
because importing that module is the configuration read — a test session
started with an incomplete environment would fail at collection instead of
inside the test that is supposed to observe the failure.
"""

from __future__ import annotations

import os

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
    "SESSION_ENCRYPTION_KEY": "test-encryption-key",
}

for _name, _value in REQUIRED_ENV.items():
    os.environ[_name] = _value


# Import only after the environment is complete.
from app.config import Settings  # noqa: E402

# A field added to `Settings` without a matching REQUIRED_ENV entry would lose
# every missing/blank-variable assertion below without failing anything. Assert
# the two stay in lockstep at collection time, where it cannot be missed.
_declared = set(Settings.model_fields)
_covered = {name.lower() for name in REQUIRED_ENV}
assert _covered == _declared, (
    "REQUIRED_ENV is out of sync with Settings: "
    f"missing from REQUIRED_ENV={sorted(_declared - _covered)}, "
    f"stale in REQUIRED_ENV={sorted(_covered - _declared)}"
)

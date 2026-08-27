"""Test environment.

The environment is populated *before* ``app.config`` is imported anywhere,
because importing that module is the configuration read — a test session
started with an incomplete environment would fail at collection instead of
inside the test that is supposed to observe the failure.
"""

from __future__ import annotations

import os

#: The complete env surface ``Settings`` reads. Kept here rather than imported
#: from the app so that a field added to ``Settings`` without a matching entry
#: in ``.env.example`` shows up as a test failure.
REQUIRED_ENV = {
    "DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "KEYCLOAK_SERVER_URL": "http://localhost:8080",
    "KEYCLOAK_REALM": "test-realm",
    "KEYCLOAK_CLIENT_ID": "test-client",
    "KEYCLOAK_CLIENT_SECRET": "test-client-secret",
    "SESSION_ENCRYPTION_KEY": "test-encryption-key",
}

for _name, _value in REQUIRED_ENV.items():
    os.environ.setdefault(_name, _value)

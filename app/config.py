"""The single configuration read for the whole service.

Twelve-factor, one object, no environment names. Every setting arrives as an
environment variable and is read exactly once, here. Nothing anywhere in the
codebase may branch on which environment it is running in — there is no such
variable to branch on.

Fields are declared required even when the story that consumes them has not
been written yet (stories 5-8 own the session vault, the JWT validation and the
cache). A deployment missing ``SESSION_ENCRYPTION_KEY`` must die at boot, not at
the first ``PUT /api/v1/session``.
"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment contract for the LinkedIn Profile API.

    Every field is required and non-empty. An unset *or* blank variable fails
    validation at import time with the offending field named on stderr, so the
    container exits non-zero and never reports itself healthy.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Datastore ----------------------------------------------------------
    database_url: str = Field(
        min_length=1,
        description="SQLAlchemy/DBAPI URL for the Postgres instance shared with Keycloak.",
    )

    # --- Identity provider --------------------------------------------------
    keycloak_server_url: str = Field(
        min_length=1,
        description="Base URL of the Keycloak server as reachable from the API container.",
    )
    keycloak_realm: str = Field(
        min_length=1,
        description="Realm that issues the tokens this API accepts.",
    )
    keycloak_client_id: str = Field(
        min_length=1,
        description="Client the API validates the token audience against.",
    )
    keycloak_client_secret: str = Field(
        min_length=1,
        description="Confidential client secret used for the service-account lane.",
    )

    # --- Application secrets ------------------------------------------------
    session_encryption_key: str = Field(
        min_length=1,
        description="Key encrypting stored LinkedIn li_at cookies at rest.",
    )


#: Module-level instance. Importing this module *is* the configuration read;
#: it raises ``pydantic.ValidationError`` when the environment is incomplete.
settings = Settings()

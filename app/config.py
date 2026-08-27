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

from typing import Annotated

from pydantic import Field, StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict

#: A setting that must be present and must carry actual content.
#:
#: ``strip_whitespace`` runs before the length check, so ``FOO=`` and
#: ``FOO="   "`` fail identically to ``FOO`` being unset. Without the strip, a
#: whitespace-only value would satisfy ``min_length=1`` and boot the service
#: with an empty encryption key — exactly the failure this contract exists to
#: prevent.
RequiredSetting = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1)
]


class Settings(BaseSettings):
    """Environment contract for the LinkedIn Profile API.

    Every field is required and non-blank. An unset, empty or whitespace-only
    variable fails validation at import time with the offending field named on
    stderr, so the container exits non-zero and never reports itself healthy.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Not "forbid": compose's env_file injects the POSTGRES_* and
        # KEYCLOAK_ADMIN_* variables the other services need, and forbidding
        # extras would make the api container refuse to start.
        extra="ignore",
    )

    # --- Datastore ----------------------------------------------------------
    database_url: RequiredSetting = Field(
        description=(
            "DBAPI URL for the Postgres instance shared with Keycloak. Inside "
            "compose this is composed from the POSTGRES_* parts and injected "
            "by the api service, overriding any value in .env."
        ),
    )

    # --- Identity provider --------------------------------------------------
    keycloak_server_url: RequiredSetting = Field(
        description="Base URL of the Keycloak server as reachable from the API container.",
    )
    keycloak_realm: RequiredSetting = Field(
        description="Realm that issues the tokens this API accepts.",
    )
    keycloak_client_id: RequiredSetting = Field(
        description="Client the API validates the token audience against.",
    )
    keycloak_client_secret: RequiredSetting = Field(
        description="Confidential client secret used for the service-account lane.",
    )

    # --- Application secrets ------------------------------------------------
    session_encryption_key: RequiredSetting = Field(
        description="Key encrypting stored LinkedIn li_at cookies at rest.",
    )


#: Module-level instance. Importing this module *is* the configuration read;
#: it raises ``pydantic.ValidationError`` when the environment is incomplete.
settings = Settings()

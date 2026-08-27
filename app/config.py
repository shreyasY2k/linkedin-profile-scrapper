"""The single configuration read for the whole service.

Twelve-factor, one object, no environment names. Every setting arrives as an
environment variable and is read exactly once, here. Nothing anywhere in the
codebase may branch on which environment it is running in — there is no such
variable to branch on.

Fields are declared required even when the story that consumes them has not
been written yet (stories 5-8 own the session vault and the cache). A
deployment missing ``SESSION_ENCRYPTION_KEY`` must die at boot, not at the
first ``PUT /api/v1/session``.
"""

from __future__ import annotations

from typing import Annotated

from pydantic import AfterValidator, Field, StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict


def _normalise_base_url(value: str) -> str:
    """Normalise a base URL so callers can concatenate paths safely.

    ``http://keycloak:8080/`` and ``http://keycloak:8080`` are the same server,
    but concatenated with ``/realms/...`` the first produces a double slash.
    Keycloak tolerates that in a request path; the ``iss`` claim comparison in
    :mod:`app.auth` does not — that is an exact string match, and a stray
    trailing slash in ``.env`` would reject every legitimate token while every
    other symptom looked healthy.

    Trailing whitespace is stripped again afterwards, not only before: the
    outer strip runs first, so ``http://keycloak:8080  /`` survives it intact
    and then loses only the slash, leaving two trailing spaces inside the
    value. Stripping slashes and whitespace together in one pass closes that.

    A value that is *only* slashes normalises to the empty string, which would
    slip past ``min_length=1`` because the constraint has already run. Reject
    it here instead, where the field name still reaches the error message.
    """
    normalised = value.rstrip("/ \t\r\n")
    if not normalised:
        raise ValueError("must be a base URL such as http://keycloak:8080")
    return normalised


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

#: A required setting that is a base URL: whitespace stripped, trailing slash
#: dropped, still non-blank. ``FOO=/`` fails rather than becoming ``""``.
RequiredBaseUrl = Annotated[RequiredSetting, AfterValidator(_normalise_base_url)]


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
    #
    # Two URLs, deliberately. They are the same Keycloak seen from two sides,
    # and collapsing them into one variable breaks whichever side loses:
    #
    #   keycloak_server_url  — where THIS PROCESS reaches Keycloak to fetch the
    #                          realm's JWKS. In compose that is the service name
    #                          on the internal network, unreachable from outside
    #                          it, and never the host a caller typed.
    #   keycloak_issuer_url  — the base URL a caller MINTS through, and hence
    #                          the base of the `iss` claim their token carries.
    #                          Locally that is 127.0.0.1:8080; deployed it is
    #                          the public https:// name nginx serves.
    #
    # The story-1 review recorded this as the one deferred finding; this is
    # where it lands.
    keycloak_server_url: RequiredBaseUrl = Field(
        description=(
            "Base URL of the Keycloak server as reachable from the API "
            "container. Used to fetch the realm JWKS, never to compare an "
            "issuer against."
        ),
    )
    keycloak_issuer_url: RequiredBaseUrl = Field(
        description=(
            "External base URL through which tokens are minted. The `iss` "
            "claim must equal exactly f'{this}/realms/{keycloak_realm}'."
        ),
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

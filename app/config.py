"""The single configuration read for the whole service.

Twelve-factor, one object, no environment names. Every setting arrives as an
environment variable and is read exactly once, here. Nothing anywhere in the
codebase may branch on which environment it is running in — there is no such
variable to branch on.

Fields are declared required even when the story that consumes them has not
been written yet (stories 5-8 own the session vault and the cache). A
deployment missing ``SESSION_ENCRYPTION_KEY`` must die at boot, not at the
first ``PUT /api/v1/session``.

There is exactly one exception, added by story 4 and argued for at the field
itself: a developer-only LinkedIn session used by the opt-in live check. It is
optional because the real session arrives per-caller at runtime (story 5), so
requiring it would stop every deployment from booting to serve a variable no
deployment has.
"""

from __future__ import annotations

from typing import Annotated, Optional

from pydantic import AfterValidator, Field, SecretStr, StringConstraints
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


def _blank_secret_is_absent(value: Optional[SecretStr]) -> Optional[SecretStr]:
    """Treat a present-but-empty optional secret as unset.

    ``.env.example`` must *assign* every variable the codebase reads — the
    contract test in ``tests/test_health.py`` skips comment lines, so a
    commented-out line would not document it. That means the shipped example
    carries ``LINKEDIN_DEV_COOKIE=``, which pydantic-settings reads as the
    empty string rather than as absent. Without this, every developer who
    copies the example would hold a "configured" session whose value is ``""``,
    and the live check would spend a real request proving it is worthless.
    """
    if value is None or not value.get_secret_value().strip():
        return None
    return value


#: An optional secret. ``SecretStr`` is the type, not ``str``, so that the
#: value cannot reach a log, a traceback, a ``repr`` or a ``model_dump()`` by
#: accident — all four render it as ``**********``. Reading it requires the
#: explicit, greppable ``.get_secret_value()``.
#: ``Optional[SecretStr]`` rather than ``SecretStr | None``: this expression
#: is evaluated at runtime inside ``Annotated``, where ``from __future__ import
#: annotations`` does not reach, so the ``|`` form would need Python 3.10+ at
#: import time even though every annotation in this file is already a string.
#:
#: The name matches ``RequiredSetting`` / ``RequiredBaseUrl`` above — and must
#: not be shortened to ``OptionalSecret``: gitleaks' ``linkedin-client-id`` rule
#: matches ``linkedin`` followed by a 14-16 character token, so the field
#: declaration below would read as a leaked credential and the pre-commit hook
#: would refuse the commit for a type annotation.
OptionalSecretSetting = Annotated[Optional[SecretStr], AfterValidator(_blank_secret_is_absent)]


class Settings(BaseSettings):
    """Environment contract for the LinkedIn Profile API.

    Every field is required and non-blank, except the one explicitly marked
    optional below. An unset, empty or whitespace-only *required* variable
    fails validation at import time with the offending field named on stderr,
    so the container exits non-zero and never reports itself healthy.
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
        description="Key encrypting stored LinkedIn session cookies at rest.",
    )

    # --- Developer-only, optional -------------------------------------------
    #
    # The ONE optional field in this contract, and the reasoning is not
    # stylistic:
    #
    #   * Required would break every deploy. Production callers supply their
    #     own LinkedIn session through `PUT /api/v1/session` (story 5); the
    #     server holds no session of its own, so a required variable would
    #     force operators to invent a value for something the service does not
    #     use.
    #   * Absent entirely would leave the opt-in live check in
    #     `tests/test_linkedin_live.py` with nowhere to read a session from but
    #     an ad-hoc environment variable no contract mentions — which is how a
    #     real cookie ends up pasted into a shell history or a test file.
    #
    # It is read in exactly one place (the live check) and never by the
    # request path. `app/linkedin/client.py` takes the session as an argument
    # and does not import this module.
    linkedin_dev_cookie: OptionalSecretSetting = Field(
        default=None,
        description=(
            "Developer's own LinkedIn session cookie, used ONLY by the opt-in "
            "live check. Optional and unset in every deployment: real sessions "
            "arrive per-caller at runtime. Never read by the request path."
        ),
    )


#: Module-level instance. Importing this module *is* the configuration read;
#: it raises ``pydantic.ValidationError`` when the environment is incomplete.
settings = Settings()

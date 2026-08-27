"""Shared test helpers. The single seam between test modules.

``tests/test_profile_api.py`` used to import fixtures from four separate test
modules, which made those modules import-time dependencies of a fifth: renaming
a helper in ``test_vault.py`` would break a file that tests something else
entirely, and a collection error in any one of them took the others with it.

Everything shared now lives here, or is re-exported from here. A test module
imports from ``tests.support`` and from nothing else under ``tests/``.

Two things are re-exported rather than moved, deliberately:

* ``bearer`` / ``make_token`` — the token minting in ``tests/test_auth.py`` is
  built on an RSA key that file generates in-process and on the JWKS double it
  installs. Lifting it out would mean moving that machinery too, and it is the
  subject of the tests that live beside it.
* ``InMemoryStore`` and the cookie constants — the store is the structural
  double that ``tests/test_vault.py`` exists to exercise.

Re-exporting keeps one import site for everyone downstream without moving code
away from the tests it is the subject of.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.linkedin.client import (
    SECTION_RESOURCES,
    RawProfile,
    SectionFetch,
    element_urns,
    reported_total,
    resolve_elements,
)

# Re-exports. See the module docstring for why these are not moved.
from tests.test_auth import RecordingFetcher, bearer, make_token  # noqa: F401
from tests.test_vault import COOKIE, OTHER_COOKIE, InMemoryStore  # noqa: F401

FIXTURES = Path(__file__).resolve().parent / "fixtures"

PROFILE_URL = "https://www.linkedin.com/in/ada-placeholder"
PUBLIC_ID = "ada-placeholder"
PROFILE_URN = "urn:li:fsd_profile:SYNTHETIC-ada-placeholder"

#: Fixed rather than "now": `fetched_at` is the caller's only staleness signal
#: and story 7 depends on it completely, so the tests assert the exact value
#: rather than a shape. A test that used `now()` could not tell the difference
#: between the fetch's timestamp and the response's.
FETCHED_AT = datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)

#: Two different Keycloak subjects.
SUBJECT_A = "615225e6-fb6a-4d02-a323-7b1fe4b6e88b"
SUBJECT_B = "9f2c1d84-0a77-4a15-bd0e-1c7a3f5b2e40"


def load(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


#: The complete profile: every section populated.
FULL_SECTIONS = {
    "experience": "voyager_experience.json",
    "education": "voyager_education.json",
    "skills": "voyager_skills.json",
    "certifications": "voyager_certifications.json",
    "languages": "voyager_languages.json",
}

#: The sparse profile: a member who genuinely has almost nothing. Two sections
#: they do have, minimally; three they do not have at all.
SPARSE_SECTIONS = {
    "experience": "voyager_sparse_experience.json",
    "education": "voyager_empty_section.json",
    "skills": "voyager_empty_section.json",
    "certifications": "voyager_sparse_certifications.json",
    "languages": "voyager_empty_section.json",
}


def ok_section(name: str, payload: dict[str, Any]) -> SectionFetch:
    """A successful fetch, with the facts derived exactly as the client derives them.

    Deliberately reuses `element_urns`, `reported_total` and `resolve_elements`
    from `app.linkedin.client` rather than hand-writing the counts: a test that
    invented its own `element_count` would keep passing after the client changed
    what it records, which is the one thing these assertions are about.
    """
    return SectionFetch(
        name=name,
        resource=SECTION_RESOURCES[name],
        ok=True,
        payload=payload,
        element_count=len(element_urns(payload)),
        reported_total=reported_total(payload),
        resolved_count=len(resolve_elements(payload)),
    )


def failed_section(name: str, code: str = "UPSTREAM_ERROR") -> SectionFetch:
    """What the client records for a section it could not read.

    Note `element_count` stays `None`. That third state — distinct from 0 — is
    what makes absent and unreadable decidable downstream.
    """
    return SectionFetch(
        name=name, resource=SECTION_RESOURCES[name], ok=False, error_code=code
    )


def raw_profile(
    *,
    core_fixture: str = "voyager_core.json",
    sections: dict[str, str] | None = None,
    overrides: dict[str, SectionFetch] | None = None,
    profile: Any = None,
    core: Any = None,
    public_id: str = PUBLIC_ID,
    fetched_at: datetime | None = None,
) -> RawProfile:
    loaded = load(core_fixture)
    envelope = loaded if core is None else core
    entity = profile if profile is not None else loaded["included"][0]
    built = {
        name: ok_section(name, load(fixture))
        for name, fixture in (sections or FULL_SECTIONS).items()
    }
    built.update(overrides or {})
    return RawProfile(
        url=PROFILE_URL,
        public_id=public_id,
        profile_urn=PROFILE_URN,
        core=envelope,
        profile=entity,
        sections=built,
        call_count=6,
        fetched_at=fetched_at or FETCHED_AT,
    )

"""LinkedIn retrieval.

One module, :mod:`app.linkedin.client`. It is the only place in this codebase
that knows a LinkedIn session cookie exists, the only place that makes an
outbound call to linkedin.com, and the only place that knows the Voyager
endpoint map. Everything above it — story 6's mapper, story 7's cache — works
from the raw payloads it returns.

What this package re-exports is what the next stories consume, and nothing
else. Story 5 needs :class:`LinkedInSession` to validate a cookie at the moment
it is stored rather than at first use; story 6 needs the envelope helpers to do
the ``*elements``/``included`` join; story 7 needs :class:`RawProfile` and
:class:`SectionFetch` to decide what is worth caching. Anything deeper —
transports, the classifier, the wire constants — is reached through
``app.linkedin.client`` explicitly, so that importing it reads as the deliberate
act it is.
"""

from __future__ import annotations

from app.linkedin.client import (
    SECTION_RESOURCES,
    LinkedInSession,
    RawProfile,
    SectionFetch,
    VoyagerClient,
    element_urns,
    find_entity,
    is_collection_envelope,
    parse_profile_url,
    reported_total,
    resolve_elements,
)

__all__ = [
    "SECTION_RESOURCES",
    "LinkedInSession",
    "RawProfile",
    "SectionFetch",
    "VoyagerClient",
    "element_urns",
    "find_entity",
    "is_collection_envelope",
    "parse_profile_url",
    "reported_total",
    "resolve_elements",
]

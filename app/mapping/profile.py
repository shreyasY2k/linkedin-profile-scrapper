"""Raw Voyager entities to the ``response-schema.md`` profile object.

===============================================================================
THE DECISION THIS MODULE EXISTS TO MAKE
===============================================================================

``response-schema.md``: *"a consumer must be able to distinguish 'this profile
has no certifications' from 'we could not read the certifications'."* Those are
two different claims about a real person, and collapsing them publishes a
confident falsehood about one of them.

So every contract field is routed to exactly one of two outcomes:

**Absent** — the member genuinely has none. The key is present and its value is
``[]`` or ``null``. The field name does **not** appear in ``partial[]``.

**Unreadable** — this run could not retrieve it. The key is **omitted entirely**
and the field name is added to the envelope's ``partial[]``.

Nothing is ever silently defaulted, and no third outcome exists. In particular
there is no "unreadable value published anyway": a raw URN in a field a caller
reads as a human-readable label is exactly that, and is why ``employment_type``
is omitted rather than filled with ``urn:li:fsd_employmentType:12``.

===============================================================================
HOW THE SECTION DECISION IS MADE — FACTS, NOT ONE SIGNAL
===============================================================================

"Zero elements came back" is not evidence of an empty section. Measured on
2026-08-27: ``profileLanguages`` returned **0 elements on one call and 3 on an
identical call minutes later**, HTTP 200 both times. Routing every empty section
to ``partial[]`` instead would put most real profiles' certifications and
languages there permanently, which reads as broken.

:class:`~app.linkedin.client.SectionFetch` therefore records success, size and
LinkedIn's own stated total as separate facts, and this module reads them all:

===========================================  ==================  ===============
Section state                                Outcome             ``partial[]``
===========================================  ==================  ===============
``ok`` false                                 unreadable          yes
``ok``, no element count recorded            unverifiable        yes
``ok``, total absent or 0, zero elements     genuinely ``[]``    no
``ok``, ``paging.total`` > elements          truncated           yes
fewer entries produced than elements named   short               yes
an element with no readable content at all   unreadable          yes
===========================================  ==================  ===============

The last three rows are one argument: a list missing entries is not a shorter
list, it is an incomplete one, and presenting it as a person's whole career is
the failure mode with no recovery. Note the shortfall check counts **what this
module produced** against LinkedIn's own element count, rather than trusting a
``resolved_count`` computed elsewhere — a fact this module can verify itself is
one that cannot silently stop being computed.

``partial[]`` says *"this may be incomplete"*, not *"this is definitely
truncated"*. A section returning exactly one full page with no stated total is
indistinguishable from a complete one, and the two errors are not symmetric: a
false positive costs a caller a caveat, a false negative publishes a partial
career as a whole one.

===============================================================================
SUB-FIELDS IN ``partial[]``
===============================================================================

``partial[]`` normally names a top-level contract field. One entry is a **dotted
path**: ``experience.employment_type``, meaning *that sub-field was unreadable
for at least one entry in that array, and is omitted from the entries where it
could not be read*. Entries whose value WAS readable still carry it, and an
entry where LinkedIn states no employment type at all keeps ``null`` — that is
absence, not unreadability, and it does not put anything in ``partial[]``.

The dotted form is documented in ``response-schema.md`` and in the README. It
exists because the alternatives were both dishonest: publishing the URN dresses
an unreadable value as a readable one, and dropping the whole ``experience``
array over one unresolvable label throws away a career to report a label.

===============================================================================
IT NAMES THE CONTRACT FIELD, NOT THE VOYAGER RESOURCE
===============================================================================

A caller reads ``response-schema.md``, not our endpoint map, so ``partial[]``
says ``certifications`` and never ``profileCertifications``. That naming is
fixed upstream — :data:`~app.linkedin.client.SECTION_RESOURCES` is keyed by the
contract's own field names, precisely so the two cannot drift.

===============================================================================
PURE AND TOTAL, AND CONTAINED PER FIELD
===============================================================================

Nothing here does I/O, reads configuration or raises. The mapper is called with
whatever LinkedIn happened to send, which the story is explicit is not always
the measured shape — the story-4 deferred-work log records that the endpoint map
was verified against exactly one profile, so *"the mapper must treat every field
as optional rather than trusting the measured shape table."*

Failure is contained **per field**. Every contract field is produced inside its
own guard, so a bug reading ``images`` costs ``images`` and nothing else. The
outer guard around :func:`map_profile` is the last resort behind that, and it is
reachable — which is why ``response-schema.md`` says any contract field may
appear in ``partial[]``, not only the five section fields.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field as dataclass_field
from typing import Any, Callable, Iterable, Mapping

from app.linkedin.client import RawProfile, SectionFetch, resolve_elements
from app.mapping.dates import date_range, month_or_year, month_year, year
from app.mapping.images import absolute_url
from app.mapping.text import renderable, web_url

logger = logging.getLogger(__name__)


#: Every field ``response-schema.md`` names, in the order its table lists them.
#: The response object is built in this order so the JSON a caller reads matches
#: the document they read it against.
CONTRACT_FIELDS: tuple[str, ...] = (
    "name",
    "headline",
    "location",
    "about",
    "experience",
    "education",
    "skills",
    "certifications",
    "languages",
    "images",
)

#: The five that come from a section fetch.
SECTION_FIELDS: tuple[str, ...] = (
    "experience",
    "education",
    "skills",
    "certifications",
    "languages",
)

#: The one sub-field that can be unreadable independently of its section. See
#: "SUB-FIELDS IN ``partial[]``" above.
EMPLOYMENT_TYPE_PATH = "experience.employment_type"

#: LinkedIn's own public URL shapes for the two URN kinds the entities carry.
#: Building these is a derivation, not an invention — the id is the source's,
#: and ``linkedin.com/company/{id}`` and ``linkedin.com/school/{id}`` resolve.
_COMPANY_URN_RE = re.compile(r"^urn:li:(?:fsd_)?company:(\d{1,20})$")
_SCHOOL_URN_RE = re.compile(r"^urn:li:(?:fsd_)?school:(\d{1,20})$")

COMPANY_URL_TEMPLATE = "https://www.linkedin.com/company/{id}"
SCHOOL_URL_TEMPLATE = "https://www.linkedin.com/school/{id}"

#: ISO 3166-1 alpha-2: exactly two ASCII letters. Anything else in
#: ``location.countryCode`` is not a country code, and the OpenAPI document
#: declares this field as alpha-2 — so publishing arbitrary text there would
#: make the documentation false.
_COUNTRY_CODE_RE = re.compile(r"^[A-Za-z]{2}$")

#: ``$type`` suffix of the entity the decorated core request delivers for a
#: member's location. See :mod:`app.linkedin.client` for the decoration.
GEO_TYPE_SUFFIX = "common.Geo"

#: Where a readable place name has been observed, in the order tried.
_GEO_NAME_KEYS = ("defaultLocalizedName", "localizedName", "name")


@dataclass(frozen=True)
class MappedProfile:
    """The two halves of the answer, kept separate because they are separate.

    ``profile`` is the contract object. ``partial`` is the envelope-level list
    of names that could not be retrieved — and every top-level name in it is a
    key **absent** from ``profile``. That invariant is asserted by the tests
    rather than merely intended.
    """

    profile: dict[str, Any]
    partial: list[str]


@dataclass
class _Context:
    """What an entry mapper needs beyond the entity itself.

    ``degraded`` collects dotted sub-field paths — see the module docstring.
    It is a set on purpose: one unresolvable employment type and forty of them
    are the same statement to a caller.
    """

    resolve: Callable[[Any], str | None]
    degraded: set[str] = dataclass_field(default_factory=set)


# --- Scalars -----------------------------------------------------------------


def _text(value: Any) -> str | None:
    """A non-empty string, or ``None``.

    Whitespace-only is ``None``: LinkedIn returns ``""`` for a field a member
    cleared, and publishing ``""`` would say "their headline is the empty
    string" where the truth is that they have none.

    **The value is never edited.** Not stripped — ``about`` is "full summary
    text, newlines preserved" per the contract, and a mapper that trimmed it
    would be editing a person's own words — and, since this review pass, never
    truncated either. An earlier version cut at 20 000 characters while its own
    docstring promised it did not, which would publish a cut-off summary as
    somebody's whole ``about``. The size bound lives where it belongs, in
    :data:`app.linkedin.client.MAX_BODY_BYTES`, which caps the response this
    string was parsed out of.

    The one substitution is a lone surrogate, which is unrepresentable rather
    than merely long: see :func:`app.mapping.text.renderable`.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    return renderable(value)


def _urn_url(value: Any, pattern: re.Pattern[str], template: str) -> str | None:
    """A LinkedIn page URL from an entity URN, or ``None``."""
    if not isinstance(value, str):
        return None
    match = pattern.match(value.strip())
    if match is None:
        return None
    return template.format(id=match.group(1))


# --- Core fields -------------------------------------------------------------


def _name(entity: Mapping[str, Any]) -> dict[str, Any]:
    """``{first, last, full}``.

    ``full`` is composed rather than read: the dash ``Profile`` carries no
    single full-name field. It is ``None`` when neither half is readable —
    ``""`` would be a name nobody has.

    No middle name. ``response-schema.md`` names three keys and adding a fourth
    is an Ask First change to the contract, so a middle name LinkedIn happens to
    send is not smuggled into ``full`` where a consumer would read it as part of
    the surname.
    """
    first = _text(entity.get("firstName"))
    last = _text(entity.get("lastName"))
    parts = [part for part in (first, last) if part]
    return {"first": first, "last": last, "full": " ".join(parts) if parts else None}


def _country_code(value: Any) -> str | None:
    """``location.countryCode`` as ISO 3166-1 alpha-2, or ``None``.

    Validated rather than passed through. The OpenAPI document declares this
    field alpha-2, so publishing whatever string arrived would make the
    documentation a false statement about the response — and a consumer
    switching on a two-letter code would meet a sentence.

    Upper-cased: alpha-2 is defined in upper case and LinkedIn sends it lower.
    Case is not information here, and a consumer comparing against ``"IN"``
    should not fail on ``"in"``.
    """
    code = _text(value)
    if code is None:
        return None
    code = code.strip()
    if not _COUNTRY_CODE_RE.match(code):
        return None
    return code.upper()


def _geo_index(core: Any) -> dict[str, str]:
    """``{geo urn: readable place name}`` from the decorated core payload.

    Empty when the core request was not decorated, or when the decoration was
    rejected and the client fell back — see :meth:`app.linkedin.client
    .VoyagerClient._fetch_core`. An empty index means ``region`` is absent, and
    absent is a legitimate answer; it is never an error.
    """
    index: dict[str, str] = {}
    if not isinstance(core, Mapping):
        return index
    included = core.get("included")
    if not isinstance(included, list):
        return index
    for entity in included:
        if not isinstance(entity, Mapping):
            continue
        if not str(entity.get("$type", "")).endswith(GEO_TYPE_SUFFIX):
            continue
        urn = entity.get("entityUrn")
        if not isinstance(urn, str) or not urn:
            continue
        for key in _GEO_NAME_KEYS:
            name = _text(entity.get(key))
            if name is not None:
                index[urn] = name.strip()
                break
    return index


def _trim_redundant_country(region: str, names: Iterable[str]) -> str:
    """Drop a trailing ``", India"`` when ``"India"`` is itself a Geo in the payload.

    The decorated response carries the member's place *and* its country as
    separate ``Geo`` entities — measured: ``"Bengaluru, Karnataka, India"``
    alongside ``"India"``. ``response-schema.md`` gives ``country`` its own
    field, so repeating the country inside ``region`` is noise.

    Nothing is invented: the only suffix ever removed is one that another entity
    in this same payload states verbatim. And the whole string is never removed —
    a member whose only Geo *is* the country keeps ``region: "India"`` rather
    than being trimmed to nothing.
    """
    for name in names:
        if name == region:
            continue
        suffix = ", " + name
        if region.endswith(suffix):
            trimmed = region[: -len(suffix)].strip()
            if trimmed:
                return trimmed
    return region


def _region(entity: Mapping[str, Any], core: Any) -> str | None:
    """The readable place name for this member, or ``None``.

    Joined from ``Profile.geoLocation["*geo"]`` against the ``Geo`` entities the
    decorated core request delivers in ``included``. ``geoUrn`` is accepted as a
    fallback key because the dash shapes have moved this reference before.

    This costs **no extra live call** — it is a decoration on the core request
    the fetch already makes.
    """
    index = _geo_index(core)
    if not index:
        return None

    geo_location = entity.get("geoLocation")
    references: list[Any] = []
    if isinstance(geo_location, Mapping):
        references.extend(
            geo_location.get(key) for key in ("*geo", "geoUrn", "*geoUrn")
        )
    references.append(entity.get("*geo"))

    for reference in references:
        if isinstance(reference, str) and reference in index:
            return _trim_redundant_country(index[reference], index.values())
    return None


def _location(entity: Mapping[str, Any], core: Any) -> dict[str, Any] | None:
    """``{country, region}``, or ``None`` when the profile states no location.

    ``None`` is a positive claim — *this member stated no location* — so it is
    returned only when **neither** half is readable. An earlier version returned
    ``None`` whenever ``countryCode`` was unreadable, which asserted that about
    a member who had a perfectly good region.

    ``region`` is populated from the decorated core request (see :func:`_region`)
    and is ``None`` when that decoration is unavailable. That is absence, not
    unreadability: a nicety that costs no call must not put a field into
    ``partial[]`` and must not take the fetch down with it.
    """
    raw = entity.get("location")
    country = _country_code(raw.get("countryCode")) if isinstance(raw, Mapping) else None
    region = _region(entity, core)
    if country is None and region is None:
        return None
    return {"country": country, "region": region}


def _images(entity: Mapping[str, Any]) -> dict[str, Any]:
    """``{profile, background}`` as absolute URLs, or ``None`` each.

    Note the granularity: ``images`` itself is always present, and a picture
    that cannot be turned into a URL is ``null`` rather than putting the whole
    ``images`` key into ``partial[]``. Omitting the object would throw away a
    perfectly good background URL to report an unreadable avatar.
    """
    return {
        "profile": absolute_url(entity.get("profilePicture")),
        "background": absolute_url(entity.get("backgroundPicture")),
    }


# --- Section entries ---------------------------------------------------------
#
# Each returns the contract entry, or `None` meaning "this element carries no
# readable content at all" — which makes its whole section unreadable. `None` is
# reserved for exactly that: a field an otherwise-real entity simply lacks is
# `null` on the entry and says nothing about the section.


def _has_content(entry: Mapping[str, Any]) -> bool:
    """Whether an entry says anything at all about a real thing.

    An entity that maps to nothing but nulls is not a sparse career entry, it is
    a non-entry: publishing it inflates someone's history with a job that has no
    title, no employer, no dates and no description. An earlier version returned
    `None` only for a non-Mapping, so ``{}`` became a published position — and
    ``_skill`` alone applied the rule this now applies everywhere.
    """
    return any(value is not None for value in entry.values())


def _experience(entity: Any, ctx: _Context) -> dict[str, Any] | None:
    if not isinstance(entity, Mapping):
        return None
    start, end = date_range(entity)
    entry: dict[str, Any] = {
        "title": _text(entity.get("title")),
        "company": _text(entity.get("companyName")),
        "company_url": _urn_url(
            entity.get("companyUrn"), _COMPANY_URN_RE, COMPANY_URL_TEMPLATE
        ),
    }

    # The one sub-field with three states rather than two. See the module
    # docstring: absent (no URN at all) is `null`; readable is the name;
    # present-but-unresolvable is OMITTED and reported in `partial[]`, because a
    # raw URN published in a field a caller reads as a label is an unreadable
    # value wearing a readable value's clothes.
    urn = entity.get("employmentTypeUrn")
    if isinstance(urn, str) and urn.strip():
        readable = ctx.resolve(urn)
        if readable is None:
            ctx.degraded.add(EMPLOYMENT_TYPE_PATH)
        else:
            entry["employment_type"] = readable
    else:
        entry["employment_type"] = None

    # `locationName` is what the member typed; `geoLocationName` is LinkedIn's
    # normalised rendering of the same place. The second is a fallback rather
    # than a competitor.
    entry["location"] = _text(entity.get("locationName")) or _text(
        entity.get("geoLocationName")
    )
    # `month_or_year`, not `month_year`. A position LinkedIn dates as ending in
    # 2019 with no month must NOT render as `end: null` — the contract defines
    # that as "current", so it would republish a finished job as one the person
    # still holds.
    entry["start"] = month_or_year(start)
    entry["end"] = month_or_year(end)
    entry["description"] = _text(entity.get("description"))

    return entry if _has_content(entry) else None


def _education(entity: Any, ctx: _Context) -> dict[str, Any] | None:
    if not isinstance(entity, Mapping):
        return None
    start, end = date_range(entity)
    # Schools carry a `schoolUrn`; some also (or only) carry a `companyUrn`
    # pointing at the institution's company page. Try the school page first,
    # because that is the page the entry is actually about.
    school_url = _urn_url(entity.get("schoolUrn"), _SCHOOL_URN_RE, SCHOOL_URL_TEMPLATE)
    if school_url is None:
        school_url = _urn_url(
            entity.get("companyUrn"), _COMPANY_URN_RE, COMPANY_URL_TEMPLATE
        )
    entry = {
        "school": _text(entity.get("schoolName")),
        "school_url": school_url,
        "degree": _text(entity.get("degreeName")),
        "field_of_study": _text(entity.get("fieldOfStudy")),
        # `year`, not `month_or_year`: the contract fixes YYYY for education,
        # and education dateRanges were measured carrying no month at all.
        "start": year(start),
        "end": year(end),
    }
    return entry if _has_content(entry) else None


def _certification(entity: Any, ctx: _Context) -> dict[str, Any] | None:
    if not isinstance(entity, Mapping):
        return None
    # Measured: `Certification.dateRange` carries `start` only — there is no
    # expiry on the wire, which is why the contract gives certifications an
    # `issued` and no end date. The `end` half is read and ignored.
    start, _end = date_range(entity)
    entry = {
        "name": _text(entity.get("name")),
        "issuer": _text(entity.get("authority")),
        # Strict `YYYY-MM`, unlike experience. `issued` has no
        # null-means-current semantics, so a year-only value is a plain absence
        # rather than a false claim — and the approved contract amendment named
        # experience only.
        "issued": month_year(start),
        "credential_url": web_url(entity.get("url")),
    }
    return entry if _has_content(entry) else None


def _language(entity: Any, ctx: _Context) -> dict[str, Any] | None:
    if not isinstance(entity, Mapping):
        return None
    entry = {
        "name": _text(entity.get("name")),
        # LinkedIn's own enum (`NATIVE_OR_BILINGUAL`, `PROFESSIONAL_WORKING`,
        # ...), verbatim. Prettifying it into "Native or bilingual" would be
        # this service inventing a display string for a source value.
        "proficiency": _text(entity.get("proficiency")),
    }
    return entry if _has_content(entry) else None


def _skill(entity: Any, ctx: _Context) -> str | None:
    """A skill is a bare string in the contract, which removes the middle option.

    ``skills`` is ``array of string``. A skill entity with no readable name has
    no ``null``-shaped hole to sit in: including ``null`` breaks the declared
    type, and dropping it silently shortens the list. So it makes the section
    unreadable, which is the honest third answer.
    """
    if not isinstance(entity, Mapping):
        return None
    return _text(entity.get("name"))


#: One entry mapper per contract field.
_ENTRY_MAPPERS: Mapping[str, Callable[[Any, _Context], Any]] = {
    "experience": _experience,
    "education": _education,
    "skills": _skill,
    "certifications": _certification,
    "languages": _language,
}


# --- Reference resolution ----------------------------------------------------


def _reference_resolver(*payloads: Any) -> Callable[[Any], str | None]:
    """Turn a URN into a readable name using the payloads' own ``included``.

    Voyager's normalized envelope carries referenced entities flat in
    ``included``. ``Position.employmentTypeUrn`` is one such reference; when
    LinkedIn delivers the entity, its ``name`` is the readable value.

    **Both the section payload and the core payload are indexed.** A referenced
    entity does not have to arrive on the envelope that referenced it, and
    indexing only the section's own ``included`` missed a readable name sitting
    in ``RawProfile.core`` — which was available and unused.

    Returns ``None`` when nothing readable is found. It deliberately does NOT
    fall back to the URN: see the module docstring on ``employment_type``.
    """
    index: dict[str, Mapping[str, Any]] = {}
    for payload in payloads:
        if not isinstance(payload, Mapping):
            continue
        included = payload.get("included")
        if not isinstance(included, list):
            continue
        for entity in included:
            if isinstance(entity, Mapping):
                urn = entity.get("entityUrn")
                # First payload wins: the section's own envelope is passed first
                # and is the more specific source.
                if isinstance(urn, str) and urn not in index:
                    index[urn] = entity

    def resolve(urn: Any) -> str | None:
        if not isinstance(urn, str) or not urn.strip():
            return None
        entity = index.get(urn)
        if entity is None:
            return None
        for key in ("name", "localizedName", "localizedTitle"):
            readable = _text(entity.get(key))
            if readable is not None:
                return readable
        return None

    return resolve


# --- Sections ----------------------------------------------------------------


def _section_is_unreadable(
    raw: RawProfile, name: str, section: SectionFetch | None
) -> str | None:
    """Why ``name`` cannot be published, or ``None`` if it can.

    Returns the operator-facing reason rather than a bool, because "this
    section is in ``partial[]``" is a claim someone will eventually have to
    debug from a log line.
    """
    if section is None:
        return "the client recorded no fetch for this section"
    if not section.ok:
        return f"the fetch failed ({section.error_code or 'no code recorded'})"
    if section.payload is None:
        return "the fetch succeeded but carried no payload"
    if section.element_count is None:
        # `ok` without a count means completeness cannot be verified at all.
        # Publishing whatever joined would be a list of unknown shortfall.
        return "the fetch recorded no element count, so completeness is unverifiable"
    if name in raw.truncated_sections:
        # `paging.total` exceeded what came back, or the page came back exactly
        # full with no total stated. Either way the list may be missing entries.
        return "LinkedIn reported more entries than this page returned"
    return None


def _map_section(
    raw: RawProfile, name: str
) -> tuple[list[Any] | None, str | None, list[str]]:
    """``(entries, reason, degraded)`` for one section.

    ``entries`` is ``None`` exactly when ``reason`` is not — the section is then
    unreadable and belongs in ``partial[]``. ``degraded`` carries any dotted
    sub-field paths (see the module docstring), and is meaningful only when the
    section itself is publishable: an omitted section already says everything
    there is to say about it.

    Contained: any exception raised while mapping this section is caught here
    and becomes this section's ``partial[]`` entry. An earlier version let it
    reach the outer guard, which degraded **all ten** contract fields because
    one of five sections had a bad entity in it.
    """
    section = raw.sections.get(name) if isinstance(raw.sections, Mapping) else None

    reason = _section_is_unreadable(raw, name, section)
    if reason is not None:
        return None, reason, []

    payload = section.payload if section is not None else None
    expected = section.element_count if section is not None else None
    if payload is None or expected is None:  # pragma: no cover - proved above
        # Not an `assert`: `python -O` strips those, and under it a narrowing
        # that was merely asserted becomes an AttributeError — a 500 — instead
        # of the degraded answer this module promises.
        return None, "the fetch succeeded but carried no payload", []

    mapper = _ENTRY_MAPPERS[name]
    ctx = _Context(resolve=_reference_resolver(payload, raw.core))

    entries: list[Any] = []
    for entity in resolve_elements(payload):
        entry = mapper(entity, ctx)
        if entry is None:
            # One element with no readable content. Dropping it would shorten
            # the list without saying so, which is the thing this module exists
            # to prevent.
            return None, "an entry could not be mapped onto the contract shape", []
        entries.append(entry)

    # Counted here, from what this module actually produced, against the number
    # of elements LinkedIn's own response named. A join that silently dropped an
    # entity is a short list, and a short career is worse than a caveat.
    if len(entries) < expected:
        return (
            None,
            f"produced {len(entries)} entries for {expected} elements LinkedIn named",
            [],
        )

    return entries, None, sorted(ctx.degraded)


# --- The mapper --------------------------------------------------------------


def map_profile(raw: RawProfile) -> MappedProfile:
    """Map one :class:`~app.linkedin.client.RawProfile` onto the contract.

    Never raises. The outer guard is not decoration: this runs on payloads from
    an unversioned upstream verified against exactly one profile, and CAP-6
    forbids an unhandled exception reaching a caller. A bug here degrades the
    answer honestly instead of becoming a 500 that says nothing.

    Since failure is now contained per field, this guard is reached only if the
    containment itself fails. It is still reachable, and ``response-schema.md``
    documents the consequence: any contract field may appear in ``partial[]``.
    """
    try:
        return _map(raw)
    except Exception:  # pragma: no cover - reached only through a bug here
        logger.exception(
            "Mapping a profile raised; degrading to a fully-partial response"
        )
        # Everything is reported unreadable, which is exactly what happened.
        # An empty `profile` with a complete `partial[]` is a true statement;
        # a 500 is not a statement at all.
        return MappedProfile(profile={}, partial=list(CONTRACT_FIELDS))


def _map(raw: RawProfile) -> MappedProfile:
    entity = raw.profile if isinstance(raw.profile, Mapping) else {}
    core = raw.core

    profile: dict[str, Any] = {}
    partial: list[str] = []

    def field(name: str, produce: Callable[[], Any]) -> None:
        """Produce one contract field, or report it unreadable. Never raises.

        This is the per-field containment. A bug reading ``images`` costs
        ``images``, not the other nine fields.
        """
        try:
            profile[name] = produce()
        except Exception:
            logger.exception("Mapping field %s raised; reporting it partial", name)
            profile.pop(name, None)
            partial.append(name)

    field("name", lambda: _name(entity))
    field("headline", lambda: _text(entity.get("headline")))
    field("location", lambda: _location(entity, core))
    field("about", lambda: _text(entity.get("summary")))

    degraded: list[str] = []
    for name in SECTION_FIELDS:
        try:
            entries, reason, collected = _map_section(raw, name)
        except Exception:
            logger.exception("Mapping section %s raised; reporting it partial", name)
            entries, reason, collected = None, "mapping the section raised", []

        if entries is None:
            # The key is OMITTED, not set to null. `response-schema.md`: "omit
            # the key entirely and add its name to a top-level partial[]".
            partial.append(name)
            logger.info("Profile section %s is reported partial: %s", name, reason)
            continue
        profile[name] = entries
        degraded.extend(collected)

    field("images", lambda: _images(entity))

    # Rebuilt in the contract's own order. The section loop above interleaves
    # with the core fields, and a caller comparing our JSON against the schema
    # table should not have to hunt.
    ordered = {name: profile[name] for name in CONTRACT_FIELDS if name in profile}
    # Top-level names first, in contract order; dotted sub-field paths after.
    ordered_partial = [name for name in CONTRACT_FIELDS if name in partial]
    ordered_partial.extend(sorted(set(degraded)))
    return MappedProfile(profile=ordered, partial=ordered_partial)

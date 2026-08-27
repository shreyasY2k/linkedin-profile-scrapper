"""``dateRange`` to a string at the granularity LinkedIn actually stated.

===============================================================================
WHY THIS IS ITS OWN MODULE
===============================================================================

``response-schema.md`` fixes two different precisions for three sections, and
the reason is stated there: *"Dates are strings in the stated granularity, not
full timestamps — LinkedIn does not expose day precision, and inventing it would
misrepresent the source."*

===============  ============  =========================================
Section          Precision     Source
===============  ============  =========================================
experience       ``YYYY-MM``   ``Position.dateRange.{start,end}``
education        ``YYYY``      ``Education.dateRange.{start,end}``
certifications   ``YYYY-MM``   ``Certification.dateRange.start`` only
===============  ============  =========================================

A ``com.linkedin.common.Date`` carries ``year``, optionally ``month``, and
optionally ``day``. Nothing here ever reads ``day``: no field in the contract
has day precision, so reading it could only lead to publishing it.

===============================================================================
YEAR-ONLY EXPERIENCE DATES, AND THE BUG THAT MADE THIS MATTER
===============================================================================

LinkedIn lets a member give a position a **year with no month**. The first
draft rendered that as ``null``, because the contract said ``YYYY-MM`` and
``2020-01`` would be a claim the source never made.

``null`` turned out to be the far worse answer, and not symmetrically so.
``response-schema.md`` defines ``end: null`` as *"null for current"*. So a
position LinkedIn dates as ending in 2019 was republished as a job the person
**still holds** — an invented fact about someone's employment, produced by a
rule written to avoid inventing facts.

The contract was therefore amended (author-approved): experience ``start`` and
``end`` accept ``YYYY-MM`` **or** ``YYYY``. :func:`month_or_year` renders at
whichever precision the source actually stated, which is what the story's
boundary asked for all along — *"dates keep exactly the granularity LinkedIn
exposes"*. Nothing is widened and nothing is invented; a consumer distinguishes
the two by length.

Certifications keep ``YYYY-MM`` strictly. ``issued`` has no null-means-current
semantics, so a year-only certification date is a plain absence rather than a
false claim — and the approved amendment named experience only.
"""

from __future__ import annotations

from typing import Any, Mapping

#: A year outside this range is a shape the source should not be producing —
#: a millisecond timestamp landing in ``year``, a sentinel, a parse artefact.
#: Rendering one would put ``1970`` or ``1755635`` in someone's career history,
#: so it is treated as unreadable and becomes ``null``.
MIN_YEAR = 1900
MAX_YEAR = 2100


def _whole_number(source: Mapping[str, Any], key: str) -> int | None:
    """``source[key]`` when it is a plain integer, else ``None``.

    ``bool`` is excluded explicitly: it is a subclass of ``int`` in Python, so
    ``{"month": True}`` would otherwise render as month 1.
    """
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def year(date: Any) -> str | None:
    """``YYYY`` from a ``com.linkedin.common.Date``, or ``None``.

    The education precision. A month present on an education date is read and
    discarded rather than surfaced — the contract says ``YYYY`` for education,
    and a schema that sometimes returns ``2016`` and sometimes ``2016-09`` for
    the same field is not a schema.
    """
    if not isinstance(date, Mapping):
        return None
    value = _whole_number(date, "year")
    if value is None or not MIN_YEAR <= value <= MAX_YEAR:
        return None
    return f"{value:04d}"


def month_year(date: Any) -> str | None:
    """``YYYY-MM`` from a ``com.linkedin.common.Date``, or ``None``.

    The experience and certification precision. ``None`` when the year is
    unreadable **and** when the month is absent — see the module docstring: a
    year-only date has no ``YYYY-MM`` rendering that is not an invention.
    """
    rendered_year = year(date)
    if rendered_year is None:
        return None
    month = _whole_number(date, "month")
    if month is None or not 1 <= month <= 12:
        return None
    return f"{rendered_year}-{month:02d}"


def month_or_year(date: Any) -> str | None:
    """``YYYY-MM`` when LinkedIn stated a month, ``YYYY`` when it stated only a year.

    The experience precision, after the amendment described in the module
    docstring. ``None`` only when there is no readable year at all.

    This is the function that stops a finished job being republished as a
    current one: a position ending in 2019 renders ``"2019"``, and ``end`` stays
    ``null`` for the case the contract reserves it for — a role still held.
    """
    return month_year(date) or year(date)


def date_range(entity: Any) -> tuple[Any, Any]:
    """``(start, end)`` out of an entity's ``dateRange``, unrendered.

    Returns the raw ``Date`` objects — or ``None`` for either half — and leaves
    the precision decision to the caller, which is the only code that knows
    which section it is mapping.

    A **missing** ``end`` is the ordinary shape of a current role, not a
    failure: ``Position.dateRange`` simply has no ``end`` key while the position
    is held. The contract wants ``end: null`` for exactly that case, and it must
    not reach ``partial[]`` — an ongoing job is not an unreadable one.
    """
    if not isinstance(entity, Mapping):
        return None, None
    raw = entity.get("dateRange")
    if not isinstance(raw, Mapping):
        return None, None
    return raw.get("start"), raw.get("end")

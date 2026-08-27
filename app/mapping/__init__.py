"""Raw Voyager entities in, ``response-schema.md`` shapes out.

Three modules, split along the three things that can independently be wrong:

* :mod:`app.mapping.dates` — granularity. ``YYYY-MM`` for experience and
  certifications, ``YYYY`` for education, and never a widened timestamp.
* :mod:`app.mapping.images` — a ``vectorImage`` joined into an absolute URL.
* :mod:`app.mapping.profile` — the mapper proper, and the absent-versus-
  unreadable decision that the whole story turns on.

Everything here is **pure and total**. No module in this package performs I/O,
reads configuration, or raises on unexpected input: a field it cannot read
becomes ``null``, and a *section* it cannot read becomes an omitted key plus an
entry in ``partial[]``. That is not defensive style for its own sake — it is the
contract. ``response-schema.md`` has a place to say "this could not be
retrieved" and no place to say "the request died because one entity was an
integer".
"""

from __future__ import annotations

from app.mapping.profile import MappedProfile, map_profile

__all__ = ["MappedProfile", "map_profile"]

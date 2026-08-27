"""The one place the mapping package decides what text and URLs are publishable.

===============================================================================
WHY THIS IS SHARED RATHER THAN COPIED
===============================================================================

``images.py`` and ``profile.py`` both build URLs that this API publishes and a
consumer's browser may follow. They each carried their own ``MAX_URL_LENGTH``
and their own "unsafe characters" pattern - two independent copies of a
security-relevant rule, free to drift the moment one is tightened and the other
is not. There is one copy now, and both import it.

===============================================================================
WHAT IS REFUSED, AND WHY EACH ONE
===============================================================================

**Control characters and ASCII whitespace** (:data:`URL_UNSAFE_RE`) - the
classic way a value breaks out of whatever context a consumer puts it in.

**Deceptive characters** (:data:`DECEPTIVE_RE`) - bidirectional overrides,
zero-width characters, and non-ASCII whitespace. A member types their own
certification URL, so it is the one string in a profile payload that a person
chose freely. A right-to-left override turns a path ending in ``exe.gnp`` into
one that renders as ``png.exe``; a zero-width space inside a hostname makes two
different domains look identical. This API publishes that string as a clickable
link, so it refuses the characters whose entire purpose is to make a link read
as something it is not.

**Lone surrogates** (:data:`SURROGATE_RE`) - not a security question but an
availability one. A lone surrogate in a mapped string raises
``UnicodeEncodeError`` inside the JSON encoder, *after* all six LinkedIn calls
have been spent, turning a successful retrieval into a 500. It is replaced with
U+FFFD, the standard "unrepresentable input" character, which states the fact
rather than hiding it.
"""

from __future__ import annotations

import re

#: Generous: a signed LinkedIn image URL carries a long query string.
MAX_URL_LENGTH = 2048

#: Control characters and ASCII whitespace. Never legal in a URL this publishes.
URL_UNSAFE_RE = re.compile(r"[\x00-\x20\x7f]")

#: Characters whose purpose is to make text read as something it is not.
#: The class itself is unreadable by construction - every member is invisible or
#: reverses what follows it - so the codepoints are enumerated in this comment.
#: Change the pattern and this list together, or the rule becomes unauditable.
#:
#: * U+200B..U+200D, U+FEFF - zero width; invisible inside a hostname.
#: * U+200E, U+200F, U+202A..U+202E, U+2066..U+2069 - bidi controls; they
#:   reverse how a path renders without changing what it fetches.
#: * U+00A0, U+1680, U+2000..U+200A, U+2028, U+2029, U+202F, U+205F, U+3000 -
#:   whitespace that ``URL_UNSAFE_RE`` does not match, because it is not ASCII.
DECEPTIVE_RE = re.compile(
    "[​-‏‪-‮⁦-⁩﻿"
    "   -     　]"
)

#: The UTF-16 surrogate range. A *lone* one cannot be encoded as UTF-8.
SURROGATE_RE = re.compile("[\ud800-\udfff]")

#: What a lone surrogate becomes. U+FFFD REPLACEMENT CHARACTER is the standard
#: marker for input that could not be represented - a statement, not a guess.
REPLACEMENT = "�"


def renderable(value: str) -> str:
    """``value`` with anything the JSON encoder would choke on replaced.

    Called on every string this package publishes. The scan is a single regex
    search that almost always fails, and the substitution runs only when a
    surrogate is actually present - so the cost on a real payload is one pass
    and no allocation.
    """
    if SURROGATE_RE.search(value):
        return SURROGATE_RE.sub(REPLACEMENT, value)
    return value


def web_url(value: object) -> str | None:
    """A publishable ``http(s)`` URL, or ``None``.

    Total: every branch that cannot produce a URL this API is willing to hand a
    consumer returns ``None``, never a partially-cleaned string. Sanitising a
    deceptive URL would be worse than dropping it - it would publish a link the
    member never supplied.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate or len(candidate) > MAX_URL_LENGTH:
        return None
    if not candidate.lower().startswith(("https://", "http://")):
        return None
    if URL_UNSAFE_RE.search(candidate) or DECEPTIVE_RE.search(candidate):
        return None
    if SURROGATE_RE.search(candidate):
        return None
    return candidate

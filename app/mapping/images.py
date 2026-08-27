"""A Voyager ``vectorImage`` joined into one absolute URL.

===============================================================================
WHY A JOIN IS NEEDED AT ALL
===============================================================================

``response-schema.md`` asks for ``images.profile`` and ``images.background`` as
**absolute URLs**. LinkedIn does not send one. It sends a root and a list of
size variants, and the URL only exists once the two are concatenated
(measured 2026-08-27)::

    "profilePicture": {
      "displayImage": {
        "vectorImage": {
          "rootUrl": "https://media.example.invalid/dms/image/.../photo-shrink_",
          "artifacts": [
            {"width": 100, "fileIdentifyingUrlPathSegment": "100_100/0/1699..."},
            {"width": 400, "fileIdentifyingUrlPathSegment": "400_400/0/1699..."}
          ]
        }
      }
    }

The concatenation is **verbatim**, with no separator inserted. A real
``rootUrl`` ends mid-path (``...photo-shrink_``) and the artifact segment
completes it (``100_100/0/...``); helpfully adding a ``/`` between them
produces a URL that 404s. :func:`absolute_url` therefore joins with ``+`` and
validates the two halves separately instead.

===============================================================================
WHICH VARIANT, AND WHY IT IS THE LARGEST
===============================================================================

The artifacts are the same image at several sizes. One has to be chosen, and
the largest is chosen because a consumer can always scale down and can never
scale up. Ties and missing widths fall back to the payload's own order, so the
choice is deterministic rather than dependent on dict iteration.

===============================================================================
WHAT IS REFUSED
===============================================================================

The payload comes from LinkedIn under the caller's own session, so this is not
a hostile-input boundary — but the output is a URL this API publishes and a
consumer may fetch. So the root must be ``http(s)`` and the artifact segment
must be a *relative* fragment: a segment carrying its own scheme, or a
protocol-relative ``//host/...``, would silently replace the host and turn our
response into a pointer at somewhere else entirely. Anything that fails becomes
``None``, never a half-built URL.
"""

from __future__ import annotations

from typing import Any, Mapping

from app.mapping.text import MAX_URL_LENGTH, URL_UNSAFE_RE

#: Where a ``vectorImage`` has been observed to sit under a ``Photo``, in the
#: order tried. The dash shapes moved it once already (``displayImage`` ->
#: ``displayImageReference``), so all the known spellings are tried rather than
#: pinning one and returning ``null`` for a picture that is right there.
_VECTOR_PATHS: tuple[tuple[str, ...], ...] = (
    ("displayImage", "vectorImage"),
    ("displayImageReference", "vectorImage"),
    ("vectorImage",),
    (),  # the photo IS the vectorImage
)

#: Path components a segment may never contain. ``..`` is the only one: the
#: host cannot change (that is checked separately), so traversal is bounded to
#: LinkedIn's own media origin — but :func:`_is_relative_segment` claims the
#: segment "can only extend a URL", and ``a/../../b`` does not extend it. The
#: docstring and the code agree now rather than nearly agreeing.
_TRAVERSAL = ".."


def _vector_image(photo: Any) -> Mapping[str, Any] | None:
    """The ``vectorImage`` inside a ``Photo``, wherever it is sitting."""
    if not isinstance(photo, Mapping):
        return None
    for path in _VECTOR_PATHS:
        node: Any = photo
        for key in path:
            if not isinstance(node, Mapping):
                node = None
                break
            node = node.get(key)
        if isinstance(node, Mapping) and "artifacts" in node and "rootUrl" in node:
            return node
    return None


def _best_segment(artifacts: Any) -> str | None:
    """The largest variant's path segment, deterministically.

    ``max`` over ``(width, -index)`` rather than a sort: the negative index
    makes the FIRST artifact win a tie on width, so two equally-sized variants
    always resolve to the same URL across runs.
    """
    if not isinstance(artifacts, list):
        return None

    best: tuple[int, int, str] | None = None
    for index, artifact in enumerate(artifacts):
        if not isinstance(artifact, Mapping):
            continue
        segment = artifact.get("fileIdentifyingUrlPathSegment")
        if not isinstance(segment, str) or not segment:
            continue
        width = artifact.get("width")
        # An unstated or nonsensical width sorts below every real one rather
        # than disqualifying the artifact — a single unlabelled variant is
        # still the best available.
        rank = width if isinstance(width, int) and not isinstance(width, bool) else -1
        candidate = (rank, -index, segment)
        if best is None or candidate > best:
            best = candidate
    return best[2] if best is not None else None


def _is_relative_segment(segment: str) -> bool:
    """Whether ``segment`` can only *extend* a URL rather than replace part of it.

    Four refusals, and each names a different thing the segment could replace:

    * a leading ``/`` or ``\\`` replaces the **path**;
    * ``://`` or a bare ``scheme:`` replaces the **host** (``javascript:`` never
      appears in a real segment and is exactly what that looks like);
    * a leading ``?`` or ``#`` replaces the **query or fragment** rather than
      completing the one a real ``rootUrl`` already carries;
    * a ``..`` path component walks **back up** the path.

    The last two were permitted by an earlier version whose docstring already
    claimed they were not.
    """
    if segment.startswith(("/", "\\", "?", "#")):
        return False
    if "://" in segment:
        return False
    scheme, colon, _ = segment.partition(":")
    if colon and scheme.isalpha():
        return False
    # Split on the query first: `?e=...&v=..` is ordinary in a signed LinkedIn
    # URL and its contents are not path components.
    path = segment.split("?", 1)[0].split("#", 1)[0]
    return _TRAVERSAL not in path.replace("\\", "/").split("/")


def absolute_url(photo: Any) -> str | None:
    """One absolute image URL from a ``Photo``, or ``None``.

    Total by construction: every branch that cannot produce a URL it is willing
    to publish returns ``None``. It never raises, because the mapper that calls
    it never raises.
    """
    vector = _vector_image(photo)
    if vector is None:
        return None

    root = vector.get("rootUrl")
    if not isinstance(root, str) or not root:
        return None
    if not root.startswith(("https://", "http://")):
        return None
    if URL_UNSAFE_RE.search(root):
        return None

    segment = _best_segment(vector.get("artifacts"))
    if segment is None or URL_UNSAFE_RE.search(segment):
        return None
    if not _is_relative_segment(segment):
        return None

    # Verbatim concatenation. See the module docstring: a real `rootUrl` ends
    # mid-path and inserting a separator breaks the URL.
    url = root + segment
    if len(url) > MAX_URL_LENGTH:
        return None
    return url

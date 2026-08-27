"""The ``/api/v1`` seam.

Empty on purpose. The prefix is fixed by ``response-schema.md``
(``GET /api/v1/profile``, ``PUT|GET /api/v1/session``); carving it out now
means stories 5-8 attach routers here instead of renegotiating the layout
under deadline.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(prefix="/api/v1")

# Stories 5-8 attach their routers here, e.g.:
#     from app.api.v1 import profile
#     router.include_router(profile.router)

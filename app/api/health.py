"""Liveness probe.

Deliberately unauthenticated and deliberately outside ``/api/v1``: the
container healthcheck, ``docker compose up --wait`` and story 2's nginx
walking skeleton all call it before any identity exists.

It reports only that the process is up and its configuration validated. It
performs no dependency check — a Postgres or Keycloak outage is not a reason
for the API container to be restarted.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter
from pydantic import BaseModel

router = APIRouter(tags=["operations"])


class HealthResponse(BaseModel):
    """Body of a successful liveness probe.

    ``status`` is a Literal, not an open string, so the OpenAPI document story 9
    ships states the only legal value rather than promising "some string".
    """

    status: Literal["ok"]


@router.get("/health", response_model=HealthResponse, summary="Liveness probe")
async def health() -> HealthResponse:
    return HealthResponse(status="ok")

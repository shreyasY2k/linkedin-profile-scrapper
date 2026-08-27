"""The one live check. Skipped by default, and it must stay that way.

Everything in `tests/test_linkedin_client.py` runs offline against synthetic
fixtures, which proves the client's *logic*. It cannot prove the thing this
story actually turns on: that the endpoint map is still real. LinkedIn's
Voyager API is unversioned and undocumented, `identity/profiles/{id}/profileView`
is already 410 Gone, and the dash endpoints below can follow at any time
without notice.

So there is exactly one test that talks to LinkedIn, and three constraints
govern it:

**It never runs by accident.** `docker build --target test && docker run` — the
command CI and a grader use — collects this file and skips it. Running it takes
a deliberate `LINKEDIN_LIVE_CHECK=1` *and* a configured developer session. A
grader who runs the suite must never spend the author's LinkedIn quota, and a
CI runner reaching LinkedIn from a datacenter IP is how an account draws a
challenge.

**It only ever fetches the session owner's own profile.** The public id comes
from `me`, not from a constant, so the fetch cannot be pointed at a third party
by editing a string. Every fetch spends real quota against a real account, and
fetching someone else's profile to test software is not the author's data to
spend.

**It asserts the map, not the mapping.** Six resources answered; that is the
whole claim. What the payloads contain is story 6's problem and is asserted
offline against fixtures.

Run it, at most once per change to `app/linkedin/client.py`::

    LINKEDIN_LIVE_CHECK=1 pytest -q -m live

Fill `LINKEDIN_DEV_COOKIE` in your local `.env` first; see `.env.example`.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from app.config import Settings
from app.linkedin.client import SECTION_RESOURCES, VoyagerClient

#: Both gates must open. The environment variable alone is not enough — a
#: developer with a session configured for other reasons must not have a live
#: call fire because they typed `pytest` — and neither is the session alone.
LIVE_ENABLED = os.environ.get("LINKEDIN_LIVE_CHECK") == "1"


def _developer_session() -> str | None:
    """The developer's own cookie, read from the environment contract.

    Read here rather than through the module-level `app.config.settings`,
    because `tests/conftest.py` deliberately overwrites the environment before
    that import and reading a fresh `Settings` keeps this honest about what a
    real run would see.
    """
    cookie = Settings().linkedin_dev_cookie
    return cookie.get_secret_value() if cookie is not None else None


pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(
        not LIVE_ENABLED,
        reason="live check is opt-in: set LINKEDIN_LIVE_CHECK=1",
    ),
]


@pytest.fixture(name="client")
def _client() -> VoyagerClient:
    cookie = _developer_session()
    if not cookie:
        pytest.skip("LINKEDIN_DEV_COOKIE is not set; see .env.example")
    return VoyagerClient(cookie)


def test_all_six_resources_still_answer(client: VoyagerClient) -> None:
    """The endpoint map, verified against the live API.

    This is the only assertion that can fail for a reason no offline test can
    predict, and the failure it exists to catch is a 410 on a dash endpoint —
    at which point the map in `app/linkedin/client.py` is wrong and the story's
    Code Map has to be re-verified before anything else is worth debugging.
    """
    # `me` first, so the profile fetched below is provably the session owner's.
    public_id = asyncio.run(client.check_session())
    assert public_id

    profile = asyncio.run(
        client.fetch_profile(f"https://www.linkedin.com/in/{public_id}")
    )

    assert profile.public_id == public_id
    assert profile.profile_urn.startswith("urn:li:fsd_profile:")
    assert set(profile.sections) == set(SECTION_RESOURCES)

    unreadable = {
        name: section.error_code
        for name, section in profile.sections.items()
        if not section.ok
    }
    assert not unreadable, f"sub-resources failed: {unreadable}"

    # The default page size of 20 truncated this profile's 33 skills to 20,
    # with a 200 and no error — a complete-looking lie. `count=100` fixed it.
    # Asserted live because no offline fixture can notice the page size
    # regressing; the symptom is a shorter list, not a failure.
    assert not profile.truncated_sections, profile.truncated_sections

    # Six for the profile, plus the one `me` call that identified it. If this
    # ever reads higher, a live call was added somewhere — which the story puts
    # behind Ask First.
    assert profile.call_count == 6
    assert client.call_count == 7

    # Nothing is written anywhere. The tempting next step when a mapping fails
    # is to dump the live payload into `tests/fixtures/` and iterate against
    # it — that payload is a real person's personal data and this repository is
    # public. Capture it outside the working tree instead.

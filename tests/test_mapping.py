"""The story-6 mapping matrix, as tests. Pure functions, no network, no clock.

| Scenario           | Expected                                                    |
|--------------------|-------------------------------------------------------------|
| Complete profile   | every populated field mapped; only unreadable sub-fields cited|
| Sparse profile     | `null` scalars and `[]` arrays — and **not** `partial[]`     |
| Section unreadable | key omitted, name in `partial[]`, the rest still returned    |
| Truncated section  | treated as unreadable, never a silently short list           |
| Short section      | fewer entries produced than elements named → unreadable      |
| Current role       | `end: null`, and NOT in `partial[]`                          |
| Finished role      | `end: "2019"` — never `null`, which would mean "current"     |
| Unreadable label   | sub-field omitted, dotted name in `partial[]`, never a URN   |
| Unexpected shape   | a response is still produced; nothing raises                 |

The pairing this file exists for is the one on rows two and three: **absent and
unreadable are different claims**, and every section is asserted both ways.
"""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

from app.linkedin.client import SECTION_PAGE_SIZE, SECTION_RESOURCES, SectionFetch
from app.mapping import map_profile
from app.mapping.dates import date_range, month_or_year, month_year, year
from app.mapping.images import absolute_url
from app.mapping.profile import (
    CONTRACT_FIELDS,
    EMPLOYMENT_TYPE_PATH,
    SECTION_FIELDS,
    _country_code,
    _urn_url,
)
from app.mapping.text import MAX_URL_LENGTH, web_url
from tests.support import (
    FULL_SECTIONS,
    PUBLIC_ID,
    SPARSE_SECTIONS,
    failed_section,
    load,
    ok_section,
    raw_profile,
)

#: `YYYY-MM` **or** `YYYY`, per the amended contract. See `app/mapping/dates.py`:
#: rendering a year-only end date as `null` republished a finished job as a
#: current one, because the contract defines `end: null` as "still held".
EXPERIENCE_PRECISION = re.compile(r"^\d{4}(-\d{2})?$")
YEAR_PRECISION = re.compile(r"^\d{4}$")

#: Every key that carries a date, by the field that holds it. Used to sweep the
#: date assertions over the real fields instead of over the serialised blob —
#: an earlier version regex-swept the whole profile and would have failed on a
#: legitimate date inside someone's `about` or a signed image URL.
DATE_KEYS = {
    "experience": ("start", "end"),
    "education": ("start", "end"),
    "certifications": ("issued",),
}


#: What `partial` contains for the complete fixture even on a perfect run.
#:
#: This is not test noise, it is measured reality. LinkedIn references
#: `employmentTypeUrn` on every position and delivers no entity naming it, so
#: `employment_type` is unreadable on real profiles too — and A1 is explicit
#: that an unreadable value is omitted and reported, never dressed up as a
#: readable one by publishing the URN. The fixture mirrors that rather than
#: inventing a friendlier payload than LinkedIn sends.
BASELINE_PARTIAL = [EMPLOYMENT_TYPE_PATH]


@pytest.fixture(name="full")
def _full():
    return raw_profile()


@pytest.fixture(name="sparse")
def _sparse():
    return raw_profile(
        core_fixture="voyager_sparse_core.json", sections=SPARSE_SECTIONS
    )


# --- Matrix: complete profile -------------------------------------------------


def test_a_complete_profile_maps_every_contract_field(full) -> None:
    mapped = map_profile(full)

    # Every top-level field is present; only the one sub-field LinkedIn
    # references without naming is reported. See BASELINE_PARTIAL.
    assert mapped.partial == BASELINE_PARTIAL
    assert set(mapped.profile) == set(CONTRACT_FIELDS)


def test_the_object_is_built_in_the_schema_tables_own_order(full) -> None:
    """A caller reads `response-schema.md` top to bottom; so should the JSON."""
    assert list(map_profile(full).profile) == list(CONTRACT_FIELDS)


def test_the_core_scalars_come_through_verbatim(full) -> None:
    profile = map_profile(full).profile

    assert profile["name"] == {
        "first": "Ada",
        "last": "Placeholder",
        "full": "Ada Placeholder",
    }
    assert profile["headline"] == "Synthetic Fixture Engineer at Example Robotics"


def test_about_preserves_newlines(full) -> None:
    """`response-schema.md`: "Full summary text, newlines preserved"."""
    about = map_profile(full).profile["about"]

    assert "\n\n" in about
    assert about == full.profile["summary"]


def test_long_text_is_published_intact_rather_than_truncated() -> None:
    """The mapper never edits a person's own words.

    An earlier version cut every string at 20 000 characters while its docstring
    promised it did not, which would publish a cut-off summary as somebody's
    whole `about`. The size bound belongs where the bytes arrive —
    `app.linkedin.client.MAX_BODY_BYTES` — not here.
    """
    summary = "word " * 20_000
    raw = raw_profile(profile={"firstName": "Ada", "summary": summary})

    assert map_profile(raw).profile["about"] == summary


def test_experience_is_ordered_as_linkedin_ordered_it(full) -> None:
    """`*elements` order is authoritative and is not recoverable by sorting.

    The current role has no end date, so a sort on `dateRange` cannot place it;
    the fixture's `included` is deliberately in the opposite order to
    `*elements` so a mapper reading `included` directly fails here.
    """
    experience = map_profile(full).profile["experience"]

    assert [entry["title"] for entry in experience] == [
        "Synthetic Fixture Engineer",
        "Placeholder Developer",
    ]


def test_an_experience_entry_carries_every_contract_key(full) -> None:
    entry = map_profile(full).profile["experience"][0]

    # `employment_type` is ABSENT here, not null: LinkedIn referenced a type and
    # delivered no readable name for it. See BASELINE_PARTIAL and A1.
    assert list(entry) == [
        "title", "company", "company_url",
        "location", "start", "end", "description",
    ]
    assert entry["company"] == "Example Robotics"
    assert entry["company_url"] == "https://www.linkedin.com/company/900001"
    assert entry["location"] == "Placeholder City, Testland"
    assert entry["description"] == "Writes fixtures that are not real people."


def test_an_education_entry_carries_every_contract_key(full) -> None:
    entry = map_profile(full).profile["education"][0]

    assert list(entry) == [
        "school", "school_url", "degree", "field_of_study", "start", "end",
    ]
    assert entry["school"] == "Placeholder Institute of Technology"
    assert entry["school_url"] == "https://www.linkedin.com/school/900003"
    assert entry["degree"] == "Bachelor of Synthetic Data"
    assert entry["field_of_study"] == "Fixture Engineering"


def test_a_school_without_a_school_urn_falls_back_to_the_company_page() -> None:
    """Some institutions carry only a `companyUrn`. It still names a real page."""
    payload = load("voyager_education.json")
    payload["included"][0].pop("schoolUrn")

    raw = raw_profile(overrides={"education": ok_section("education", payload)})

    assert (
        map_profile(raw).profile["education"][0]["school_url"]
        == "https://www.linkedin.com/company/900003"
    )


def test_a_certification_entry_carries_every_contract_key(full) -> None:
    entry = map_profile(full).profile["certifications"][0]

    assert list(entry) == ["name", "issuer", "issued", "credential_url"]
    assert entry["issuer"] == "Example Standards Body"
    assert entry["issued"] == "2022-11"
    assert entry["credential_url"] == "https://certs.example.invalid/synthetic/1"


def test_a_year_only_certification_date_stays_null() -> None:
    """`issued` is strictly `YYYY-MM`, unlike experience — and deliberately so.

    `issued` has no null-means-current meaning, so a year-only value is a plain
    absence rather than the false claim a year-only experience `end` would be.
    The approved contract amendment named experience only.
    """
    payload = load("voyager_certifications.json")
    payload["included"][0]["dateRange"]["start"] = {"year": 2021}

    raw = raw_profile(overrides={"certifications": ok_section("certifications", payload)})
    mapped = map_profile(raw)

    assert mapped.profile["certifications"][0]["issued"] is None
    assert mapped.partial == BASELINE_PARTIAL, "no SECTION is reported unreadable"


def test_skills_are_bare_strings_and_languages_are_objects(full) -> None:
    profile = map_profile(full).profile

    assert profile["skills"] == [
        "Synthetic Testing", "Placeholder Design", "Fixture Review",
    ]
    assert profile["languages"][0] == {
        "name": "Testish", "proficiency": "NATIVE_OR_BILINGUAL"
    }


def test_images_are_absolute_urls_at_the_largest_available_size(full) -> None:
    images = map_profile(full).profile["images"]

    assert images == {
        "profile": "https://images.example.invalid/synthetic/400_400/profile-picture.png",
        "background": "https://images.example.invalid/synthetic/400_400/background-picture.png",
    }


# --- A1: employment_type is never a URN --------------------------------------


def test_an_unresolvable_employment_type_is_omitted_and_reported_partial(
    full,
) -> None:
    """A raw URN is an unreadable value dressed as a readable one.

    Nothing in the fixture names `urn:li:fsd_employmentType:1`, so there is no
    readable label to publish — and publishing the URN would put an internal
    identifier in a field a caller reads as "Full-time".
    """
    mapped = map_profile(full)
    entry = mapped.profile["experience"][0]

    assert "employment_type" not in entry
    assert EMPLOYMENT_TYPE_PATH in mapped.partial
    assert "urn:li:fsd_employmentType" not in json.dumps(mapped.profile)


def test_employment_type_uses_the_readable_name_when_linkedin_delivers_one() -> None:
    payload = load("voyager_experience.json")
    payload["included"].append(
        {
            "entityUrn": "urn:li:fsd_employmentType:1",
            "name": "Full-time",
            "$type": "com.linkedin.voyager.dash.identity.profile.EmploymentType",
        }
    )
    raw = raw_profile(overrides={"experience": ok_section("experience", payload)})
    mapped = map_profile(raw)

    assert mapped.profile["experience"][0]["employment_type"] == "Full-time"
    # Only entry 0 was given a resolvable type; entry 1 still cannot be read.
    assert EMPLOYMENT_TYPE_PATH in mapped.partial


def test_a_readable_name_delivered_on_the_CORE_envelope_is_still_found() -> None:
    """A referenced entity need not arrive on the envelope that referenced it.

    Indexing only the section's own `included` missed a readable name sitting in
    `RawProfile.core`, which was available and unused.
    """
    core = load("voyager_core.json")
    for urn in ("urn:li:fsd_employmentType:1", "urn:li:fsd_employmentType:2"):
        core["included"].append(
            {"entityUrn": urn, "name": "Full-time", "$type": "…EmploymentType"}
        )

    mapped = map_profile(raw_profile(core=core))

    assert mapped.profile["experience"][0]["employment_type"] == "Full-time"
    assert mapped.partial == []


def test_a_position_stating_no_employment_type_keeps_null_and_stays_out_of_partial() -> None:
    """Absence is not unreadability, even for the sub-field that has both."""
    payload = load("voyager_experience.json")
    for entity in payload["included"]:
        entity.pop("employmentTypeUrn")

    raw = raw_profile(overrides={"experience": ok_section("experience", payload)})
    mapped = map_profile(raw)

    assert mapped.profile["experience"][0]["employment_type"] is None
    assert mapped.partial == []


def test_a_dotted_partial_entry_sorts_after_the_top_level_names() -> None:
    raw = raw_profile(overrides={"languages": failed_section("languages")})

    assert map_profile(raw).partial == ["languages", EMPLOYMENT_TYPE_PATH]


def test_a_sub_field_degradation_is_dropped_when_its_section_is_unreadable() -> None:
    """An omitted section already says everything there is to say about it."""
    raw = raw_profile(overrides={"experience": failed_section("experience")})

    mapped = map_profile(raw)

    assert mapped.partial == ["experience"]
    assert EMPLOYMENT_TYPE_PATH not in mapped.partial


# --- A2: region, from the decorated core request, at no extra call ------------


def test_location_carries_the_country_and_the_resolved_region(full) -> None:
    """Joined from `Profile.geoLocation["*geo"]` against the core `included`."""
    mapped = map_profile(full)

    assert mapped.profile["location"] == {
        "country": "ZZ",
        "region": "Placeholder City",
    }
    assert "location" not in mapped.partial


def test_a_redundant_trailing_country_name_is_trimmed_but_never_invented() -> None:
    """`country` has its own field, so repeating it inside `region` is noise.

    The only suffix ever removed is one another Geo entity in the same payload
    states verbatim — nothing is guessed at.
    """
    core = load("voyager_core.json")
    for entity in core["included"]:
        if entity.get("entityUrn") == "urn:li:fsd_geo:100000000":
            entity["defaultLocalizedName"] = "Placeholder City, Somewhere Else"

    assert map_profile(raw_profile(core=core)).profile["location"]["region"] == (
        "Placeholder City, Somewhere Else"
    ), "a suffix no other Geo names must be left alone"


def test_a_country_only_geo_is_not_trimmed_to_nothing() -> None:
    core = load("voyager_core.json")
    core["included"] = [
        entity
        for entity in core["included"]
        if entity.get("entityUrn") != "urn:li:fsd_geo:100000001"
    ]
    for entity in core["included"]:
        if entity.get("entityUrn") == "urn:li:fsd_geo:100000000":
            entity["defaultLocalizedName"] = "Testland"

    assert map_profile(raw_profile(core=core)).profile["location"]["region"] == "Testland"


def test_an_undecorated_core_leaves_region_absent_rather_than_partial() -> None:
    """The decoration is brittle by construction; a fallback must cost nothing.

    `app/linkedin/client.py` retries undecorated when the decoration is refused,
    and the profile then has no Geo entities at all. That is an absence.
    """
    core = load("voyager_core.json")
    core["included"] = [
        entity
        for entity in core["included"]
        if not str(entity.get("$type", "")).endswith("common.Geo")
    ]

    mapped = map_profile(raw_profile(core=core))

    assert mapped.profile["location"] == {"country": "ZZ", "region": None}
    assert mapped.partial == BASELINE_PARTIAL, "an absent region is not a partial one"


def test_a_member_with_a_region_and_no_country_still_gets_a_location() -> None:
    """`location: null` asserts the member stated no location. That must be true.

    An earlier version returned `null` for the whole object whenever
    `countryCode` was unreadable, which said that about a member who had a
    perfectly good region.
    """
    core = load("voyager_core.json")
    core["included"][0].pop("location")

    assert map_profile(raw_profile(core=core, profile=core["included"][0])).profile[
        "location"
    ] == {"country": None, "region": "Placeholder City"}


@pytest.mark.parametrize(
    "code", ["Testland", "z", "zzz", "1n", "", "  ", 42, None, True]
)
def test_a_country_code_that_is_not_alpha_2_is_absent(code: Any) -> None:
    """The OpenAPI document declares this field ISO 3166-1 alpha-2.

    Publishing arbitrary text there would make the documentation a false
    statement about the response.
    """
    assert _country_code(code) is None


def test_a_country_code_is_upper_cased() -> None:
    assert _country_code("in") == "IN"


# --- Matrix: sparse profile — absent is NOT partial ---------------------------


def test_a_sparse_profile_reports_nothing_partial(sparse) -> None:
    """The single most important negative in this file.

    A member who genuinely has no education, no skills and no languages must
    not be described as a profile we failed to read.
    """
    assert map_profile(sparse).partial == []


def test_genuinely_empty_sections_are_empty_arrays(sparse) -> None:
    profile = map_profile(sparse).profile

    assert profile["education"] == []
    assert profile["skills"] == []
    assert profile["languages"] == []
    # And the keys are PRESENT — an empty section is an answer, not a gap.
    assert {"education", "skills", "languages"} <= set(profile)


def test_absent_scalars_are_null_not_empty_strings(sparse) -> None:
    profile = map_profile(sparse).profile

    # `headline` is `""` on the wire for a member who cleared it. `""` would
    # say "their headline is the empty string"; `null` says they have none.
    assert profile["headline"] is None
    assert profile["about"] is None
    assert profile["location"] is None
    assert profile["images"] == {"profile": None, "background": None}


def test_a_current_role_ends_with_null_and_never_reaches_partial(sparse) -> None:
    """Absent-not-unreadable, in the case a caller meets on almost every profile."""
    mapped = map_profile(sparse)
    entry = mapped.profile["experience"][0]

    assert entry["start"] == "2024-05"
    assert entry["end"] is None
    assert mapped.partial == []


def test_a_certification_with_no_expiry_and_no_issuer_still_maps(sparse) -> None:
    entry = map_profile(sparse).profile["certifications"][0]

    assert entry["issued"] == "2021-01"
    assert entry["issuer"] is None
    assert entry["credential_url"] is None


def test_a_sparse_entry_keeps_every_key_with_null_values(sparse) -> None:
    """A field the entity lacks is `null` ON the entry — the entry is not shortened."""
    entry = map_profile(sparse).profile["experience"][0]

    assert list(entry) == [
        "title", "company", "company_url", "employment_type",
        "location", "start", "end", "description",
    ]
    assert entry["description"] is None
    assert entry["location"] is None
    assert entry["employment_type"] is None
    assert entry["company_url"] is None


# --- Matrix: unreadable — the other half of every pairing ----------------------


@pytest.mark.parametrize("name", SECTION_FIELDS)
def test_a_failed_section_is_omitted_and_named_in_partial(name: str) -> None:
    raw = raw_profile(overrides={name: failed_section(name)})

    mapped = map_profile(raw)

    assert name in mapped.partial
    assert name not in mapped.profile, "an unreadable field must be OMITTED, not null"


@pytest.mark.parametrize("name", SECTION_FIELDS)
def test_an_empty_section_and_a_failed_section_differ(name: str) -> None:
    """The pairing, section by section. Same field, two states, two answers."""
    empty = map_profile(
        raw_profile(overrides={name: ok_section(name, load("voyager_empty_section.json"))})
    )
    failed = map_profile(raw_profile(overrides={name: failed_section(name)}))

    assert empty.profile[name] == [] and name not in empty.partial
    assert name not in failed.profile and name in failed.partial


def test_one_failed_section_does_not_cost_the_others() -> None:
    """Degraded honestly: the rest of the profile still comes back."""
    raw = raw_profile(overrides={"languages": failed_section("languages")})

    mapped = map_profile(raw)

    assert "languages" in mapped.partial
    assert mapped.profile["skills"]
    assert mapped.profile["experience"]
    assert mapped.profile["name"]["full"] == "Ada Placeholder"


def test_a_section_missing_from_the_fetch_entirely_is_partial() -> None:
    raw = raw_profile(
        sections={k: v for k, v in FULL_SECTIONS.items() if k != "skills"}
    )

    mapped = map_profile(raw)

    assert "skills" in mapped.partial
    assert "skills" not in mapped.profile


def test_a_truncated_section_is_unreadable_not_short() -> None:
    """`paging.total` says there are more than came back. Never a silently short list."""
    payload = load("voyager_skills.json")
    payload["data"]["paging"]["total"] = 33

    raw = raw_profile(overrides={"skills": ok_section("skills", payload)})
    mapped = map_profile(raw)

    assert "skills" in mapped.partial
    assert "skills" not in mapped.profile


def test_a_precisely_full_page_with_no_total_is_treated_as_truncated() -> None:
    """The false positive is the cheap error; the false negative publishes a lie."""
    payload = load("voyager_skills.json")
    payload["data"]["paging"].pop("total")
    template = payload["included"][0]
    payload["data"]["*elements"] = []
    payload["included"] = []
    for index in range(SECTION_PAGE_SIZE):
        urn = f"urn:li:fsd_skill:(SYNTHETIC,{index})"
        payload["data"]["*elements"].append(urn)
        payload["included"].append({**template, "entityUrn": urn})

    raw = raw_profile(overrides={"skills": ok_section("skills", payload)})

    assert "skills" in map_profile(raw).partial


def test_a_section_whose_entries_were_not_delivered_is_unreadable() -> None:
    """`*elements` referenced three skills and `included` carried two.

    Counted from what the mapper PRODUCED against LinkedIn's own element count,
    not from a `resolved_count` computed elsewhere.
    """
    payload = load("voyager_skills.json")
    payload["included"].pop()

    raw = raw_profile(overrides={"skills": ok_section("skills", payload)})
    mapped = map_profile(raw)

    assert "skills" in mapped.partial
    assert "skills" not in mapped.profile


def test_a_shortfall_is_caught_even_when_resolved_count_was_never_recorded() -> None:
    """The mapper verifies completeness itself rather than trusting another module.

    A `SectionFetch` with `ok=True` and `resolved_count=None` used to skip the
    check entirely and publish a silently short list.
    """
    payload = load("voyager_skills.json")
    payload["included"].pop()
    section = SectionFetch(
        name="skills",
        resource=SECTION_RESOURCES["skills"],
        ok=True,
        payload=payload,
        element_count=3,
        reported_total=3,
        resolved_count=None,
    )

    assert "skills" in map_profile(raw_profile(overrides={"skills": section})).partial


def test_a_section_with_no_element_count_recorded_is_unverifiable() -> None:
    """`ok` without a count means completeness cannot be checked at all."""
    section = SectionFetch(
        name="skills",
        resource=SECTION_RESOURCES["skills"],
        ok=True,
        payload=load("voyager_skills.json"),
        element_count=None,
    )

    assert "skills" in map_profile(raw_profile(overrides={"skills": section})).partial


def test_a_skill_with_no_readable_name_makes_the_section_unreadable() -> None:
    """`skills` is `array of string`, which removes the middle option."""
    payload = load("voyager_skills.json")
    payload["included"][1].pop("name")

    raw = raw_profile(overrides={"skills": ok_section("skills", payload)})

    assert "skills" in map_profile(raw).partial


def test_an_element_that_is_not_an_object_makes_the_section_unreadable() -> None:
    payload = load("voyager_experience.json")
    payload["included"][0] = "urn:li:fsd_profilePosition:(SYNTHETIC,2)"

    raw = raw_profile(overrides={"experience": ok_section("experience", payload)})

    assert "experience" in map_profile(raw).partial


@pytest.mark.parametrize("name", ["experience", "education", "certifications", "languages"])
def test_an_entity_with_no_readable_content_is_not_a_published_entry(name: str) -> None:
    """`{}` is not a sparse career entry, it is a non-entry.

    Publishing it inflates someone's history with a job that has no title, no
    employer, no dates and no description. `_skill` alone used to apply this
    rule; it applies everywhere now.
    """
    payload = load({
        "experience": "voyager_experience.json",
        "education": "voyager_education.json",
        "certifications": "voyager_certifications.json",
        "languages": "voyager_languages.json",
    }[name])
    urn = "urn:li:fsd_empty:(SYNTHETIC,0)"
    payload["data"]["*elements"].append(urn)
    payload["included"].append({"entityUrn": urn})
    payload["data"]["paging"]["total"] = len(payload["data"]["*elements"])

    raw = raw_profile(overrides={name: ok_section(name, payload)})
    mapped = map_profile(raw)

    assert name in mapped.partial
    assert name not in mapped.profile


def test_partial_names_the_contract_field_never_the_voyager_resource() -> None:
    """A caller reads `response-schema.md`, not our endpoint map."""
    raw = raw_profile(
        overrides={name: failed_section(name) for name in SECTION_FIELDS}
    )

    mapped = map_profile(raw)

    assert set(mapped.partial) == set(SECTION_FIELDS)
    assert not any(name.startswith("profile") for name in mapped.partial)


@pytest.mark.parametrize("name", SECTION_FIELDS)
def test_every_top_level_name_in_partial_is_absent_from_the_profile(name: str) -> None:
    """The invariant the whole envelope rests on, asserted rather than intended."""
    raw = raw_profile(overrides={name: failed_section(name)})

    mapped = map_profile(raw)

    for field in mapped.partial:
        if "." in field:
            continue  # a dotted path names a key inside entries, not a top-level one
        assert field not in mapped.profile


# --- Matrix: date precision ---------------------------------------------------


def test_experience_dates_are_month_or_year_precision_or_null(full) -> None:
    for entry in map_profile(full).profile["experience"]:
        for key in ("start", "end"):
            value = entry[key]
            assert value is None or EXPERIENCE_PRECISION.match(value), (key, value)


def test_education_dates_are_year_precision_or_null(full) -> None:
    for entry in map_profile(full).profile["education"]:
        for key in ("start", "end"):
            value = entry[key]
            assert value is None or YEAR_PRECISION.match(value), (key, value)


def test_no_date_field_anywhere_carries_day_or_time_precision(full) -> None:
    """Swept over the date FIELDS, not the serialised blob.

    An earlier version regex-swept the whole profile object, so it would have
    failed on a legitimate `2024-01-01` inside someone's `about` or on the
    epoch in a signed image URL. It passed only because the fixtures are clean.
    """
    profile = map_profile(full).profile
    checked = 0

    for field, keys in DATE_KEYS.items():
        for entry in profile[field]:
            for key in keys:
                value = entry[key]
                if value is None:
                    continue
                checked += 1
                assert not re.search(r"\d{4}-\d{2}-\d{2}", value), (field, key, value)
                assert "T" not in value and ":" not in value, (field, key, value)

    assert checked, "swept no dates — the sweep is broken, not the fixtures"


def test_an_education_month_is_discarded_rather_than_surfaced() -> None:
    """A field that is sometimes `YYYY` and sometimes `YYYY-MM` is not a field."""
    payload = load("voyager_education.json")
    payload["included"][0]["dateRange"]["start"]["month"] = 9

    raw = raw_profile(overrides={"education": ok_section("education", payload)})

    assert map_profile(raw).profile["education"][0]["start"] == "2016"


def test_a_year_only_experience_start_renders_as_a_year() -> None:
    payload = load("voyager_experience.json")
    # `included` is deliberately in the opposite order to `*elements`, so this
    # is the SECOND entry a caller sees — the earlier, bounded role.
    payload["included"][0]["dateRange"]["start"] = {"year": 2020}

    raw = raw_profile(overrides={"experience": ok_section("experience", payload)})
    mapped = map_profile(raw)

    assert mapped.profile["experience"][1]["start"] == "2020"
    assert mapped.profile["experience"][1]["end"] == "2023-02"
    assert "experience" not in mapped.partial


def test_a_year_only_END_date_never_becomes_null() -> None:
    """The bug this amendment exists for, stated as directly as it can be.

    `end: null` is defined by the contract as "the person still holds this
    role". A position LinkedIn dates as ending in 2019 with no month must not
    be republished as their current job.
    """
    payload = load("voyager_experience.json")
    payload["included"][0]["dateRange"]["end"] = {"year": 2019}

    raw = raw_profile(overrides={"experience": ok_section("experience", payload)})
    finished = map_profile(raw).profile["experience"][1]

    assert finished["end"] == "2019"
    assert finished["end"] is not None, "a finished job must never look current"


def test_month_or_year_prefers_the_month_when_there_is_one() -> None:
    assert month_or_year({"year": 2020, "month": 3}) == "2020-03"
    assert month_or_year({"year": 2020}) == "2020"
    assert month_or_year({"month": 3}) is None


@pytest.mark.parametrize(
    "date",
    [
        None,
        {},
        {"year": "2020", "month": 3},
        {"year": 2020, "month": 13},
        {"year": 2020, "month": 0},
        {"year": 1799, "month": 3},
        {"year": 2400, "month": 3},
        {"year": True, "month": True},
        {"year": 1735689600000, "month": 1},
        "2020-03",
        [2020, 3],
    ],
)
def test_an_unrenderable_date_is_null_and_never_raises(date: Any) -> None:
    assert month_year(date) is None
    assert year(date) is None or YEAR_PRECISION.match(year(date))


def test_date_range_reads_a_missing_end_as_absent_not_as_an_error() -> None:
    entity = {"dateRange": {"start": {"year": 2024, "month": 5}}}

    start, end = date_range(entity)

    assert month_or_year(start) == "2024-05"
    assert end is None and month_or_year(end) is None


@pytest.mark.parametrize("entity", [None, "", 3, [], {"dateRange": "nope"}])
def test_date_range_is_total(entity: Any) -> None:
    assert date_range(entity) == (None, None)


def test_year_ignores_a_month_it_is_given() -> None:
    assert year({"year": 2016, "month": 9}) == "2016"


# --- URNs and URLs ------------------------------------------------------------


@pytest.mark.parametrize(
    "urn",
    [
        None,
        7,
        "",
        "urn:li:fsd_company:",
        "urn:li:fsd_company:abc",
        "urn:li:fsd_school:900003",           # right shape, WRONG kind
        "urn:li:fsd_company:900003/../evil",
        "urn:li:fsd_company:900003 ",         # inner space after strip is fine, this is trailing
        "https://evil.invalid/company/1",
    ],
)
def test_a_malformed_or_foreign_urn_yields_no_url(urn: Any) -> None:
    from app.mapping.profile import _COMPANY_URN_RE, COMPANY_URL_TEMPLATE

    result = _urn_url(urn, _COMPANY_URN_RE, COMPANY_URL_TEMPLATE)
    assert result is None or result == "https://www.linkedin.com/company/900003"


def test_a_credential_url_over_the_length_cap_is_dropped() -> None:
    payload = load("voyager_certifications.json")
    payload["included"][0]["url"] = "https://certs.example.invalid/" + "a" * MAX_URL_LENGTH

    raw = raw_profile(overrides={"certifications": ok_section("certifications", payload)})

    assert map_profile(raw).profile["certifications"][0]["credential_url"] is None


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(document.cookie)",
        "data:text/html,<script>alert(1)</script>",
        "file:///etc/passwd",
        # Bidi override: renders as though it ends in `.png`.
        "https://example.invalid/‮gnp.exe",
        # Zero-width space inside the hostname.
        "https://exam​ple.invalid/x",
        # Non-ASCII whitespace, which the ASCII pattern does not match.
        "https://example.invalid/a b",
        "https://example.invalid/⁦spoof⁩",
    ],
)
def test_a_deceptive_or_non_web_credential_url_is_dropped(url: str) -> None:
    """`Certification.url` is the one string in a payload a member chose freely.

    This API publishes it as a clickable link, so it refuses the characters
    whose entire purpose is to make a link read as something it is not.
    """
    payload = load("voyager_certifications.json")
    payload["included"][0]["url"] = url

    raw = raw_profile(overrides={"certifications": ok_section("certifications", payload)})
    entry = map_profile(raw).profile["certifications"][0]

    assert entry["credential_url"] is None
    assert entry["name"] == "Certified Synthetic Fixture Author"


def test_web_url_accepts_an_ordinary_link() -> None:
    """Guards every rejection above from passing because everything is rejected."""
    assert web_url("https://certs.example.invalid/x?a=1&b=2#frag") == (
        "https://certs.example.invalid/x?a=1&b=2#frag"
    )


# --- Images -------------------------------------------------------------------


def test_an_image_url_is_joined_verbatim_with_no_separator_inserted() -> None:
    """A real `rootUrl` ends mid-path; a helpfully-added `/` produces a 404."""
    photo = {
        "displayImage": {
            "vectorImage": {
                "rootUrl": "https://images.example.invalid/photo-shrink_",
                "artifacts": [
                    {"width": 200, "fileIdentifyingUrlPathSegment": "200_200/x.png"}
                ],
            }
        }
    }

    assert absolute_url(photo) == "https://images.example.invalid/photo-shrink_200_200/x.png"


def test_a_signed_query_string_survives_the_join() -> None:
    """Real segments carry `?e=...&v=beta&t=...`; refusing those would drop every image."""
    photo = {
        "vectorImage": {
            "rootUrl": "https://images.example.invalid/shrink_",
            "artifacts": [
                {"width": 400, "fileIdentifyingUrlPathSegment": "400_400/0/17?e=1&v=beta"}
            ],
        }
    }

    assert absolute_url(photo) == "https://images.example.invalid/shrink_400_400/0/17?e=1&v=beta"


def test_the_largest_variant_wins_deterministically() -> None:
    photo = {
        "vectorImage": {
            "rootUrl": "https://images.example.invalid/",
            "artifacts": [
                {"width": 800, "fileIdentifyingUrlPathSegment": "big.png"},
                {"width": 100, "fileIdentifyingUrlPathSegment": "small.png"},
                {"width": 800, "fileIdentifyingUrlPathSegment": "also-big.png"},
            ],
        }
    }

    assert absolute_url(photo) == "https://images.example.invalid/big.png"


@pytest.mark.parametrize(
    "segment",
    [
        "//evil.invalid/x.png",
        "https://evil.invalid/x",
        "javascript:alert(1)",
        "/absolute/path.png",
        "\\windows\\path.png",
        # Path traversal: the host cannot change, but this does not EXTEND the
        # URL, which is what `_is_relative_segment` claims to guarantee.
        "../../../secret.png",
        "a/../../b.png",
        # These replace the query or fragment rather than completing the one a
        # real `rootUrl` already carries.
        "?e=evil",
        "#fragment",
    ],
)
def test_a_segment_that_would_replace_rather_than_extend_is_refused(segment: str) -> None:
    photo = {
        "vectorImage": {
            "rootUrl": "https://images.example.invalid/",
            "artifacts": [{"width": 100, "fileIdentifyingUrlPathSegment": segment}],
        }
    }

    assert absolute_url(photo) is None


@pytest.mark.parametrize(
    "vector",
    [
        {"rootUrl": "https://images.example.invalid/", "artifacts": []},
        {"rootUrl": "", "artifacts": [{"fileIdentifyingUrlPathSegment": "x.png"}]},
        {"rootUrl": "file:///etc/passwd", "artifacts": [{"fileIdentifyingUrlPathSegment": "x"}]},
        {"rootUrl": "https://images.example.invalid/", "artifacts": "nope"},
        {
            "rootUrl": "https://images.example.invalid/",
            "artifacts": [{"fileIdentifyingUrlPathSegment": "a" * (MAX_URL_LENGTH + 1)}],
        },
    ],
)
def test_an_unbuildable_image_is_null_never_a_half_built_url(vector: Any) -> None:
    assert absolute_url({"vectorImage": vector}) is None


@pytest.mark.parametrize("photo", [None, "", 7, [], {}, {"displayImage": None}])
def test_absolute_url_is_total(photo: Any) -> None:
    assert absolute_url(photo) is None


# --- Matrix: unexpected shape — mapping never raises --------------------------


HOSTILE_ENTITIES: list[Any] = [
    None,
    "",
    0,
    [],
    {},
    {"firstName": 7, "lastName": None, "headline": [], "summary": {}},
    {"location": "Placeholder City"},
    {"location": {"countryCode": 42}},
    {"profilePicture": "not-a-photo", "backgroundPicture": 9},
    {"geoLocation": "not-a-mapping"},
    {"geoLocation": {"*geo": 7}},
]


@pytest.mark.parametrize("entity", HOSTILE_ENTITIES, ids=range(len(HOSTILE_ENTITIES)))
def test_an_unexpected_core_entity_still_produces_a_response(entity: Any) -> None:
    """CAP-6: no unhandled exception reaches a caller. Ever, for any input."""
    mapped = map_profile(raw_profile(profile=entity))

    assert isinstance(mapped.profile, dict)
    assert isinstance(mapped.partial, list)
    assert "name" in mapped.profile


def test_a_field_the_entity_lacks_is_null_rather_than_partial() -> None:
    """The matrix row: "that field absent or partial[]; never a 500"."""
    core = load("voyager_core.json")
    core["included"] = []
    mapped = map_profile(
        raw_profile(core=core, profile={"publicIdentifier": PUBLIC_ID})
    )

    assert mapped.profile["name"] == {"first": None, "last": None, "full": None}
    assert mapped.profile["headline"] is None
    assert mapped.profile["location"] is None
    assert "experience" in mapped.profile


def test_a_hostile_section_payload_degrades_rather_than_raising() -> None:
    hostile = SectionFetch(
        name="experience",
        resource=SECTION_RESOURCES["experience"],
        ok=True,
        payload={"data": {"*elements": ["a", "b"]}, "included": "not-a-list"},
        element_count=2,
        reported_total=None,
        resolved_count=0,
    )
    raw = raw_profile(overrides={"experience": hostile})

    mapped = map_profile(raw)

    assert "experience" in mapped.partial
    assert mapped.profile["skills"]


def test_a_lone_surrogate_is_replaced_rather_than_becoming_a_500() -> None:
    """It would raise UnicodeEncodeError in the JSON encoder — after six calls.

    A successful retrieval must not become a 500 because one character cannot
    be encoded.
    """
    raw = raw_profile(profile={"firstName": "Ada", "summary": "before \ud800 after"})

    about = map_profile(raw).profile["about"]

    assert "�" in about
    json.dumps(about)  # the assertion: this used to raise


def test_one_broken_field_costs_that_field_and_not_the_other_nine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failure is contained per field.

    An earlier version let a bug reading `images` degrade all ten contract
    fields to `partial[]`, which reported nine fields unreadable that had been
    read perfectly.
    """
    from app.mapping import profile as mapper

    def explode(entity: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(mapper, "_images", explode)

    mapped = map_profile(raw_profile())

    assert mapped.partial == ["images", EMPLOYMENT_TYPE_PATH]
    assert "images" not in mapped.profile
    assert mapped.profile["name"]["full"] == "Ada Placeholder"
    assert mapped.profile["skills"]


def test_one_broken_section_costs_that_section_and_not_the_other_nine(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.mapping import profile as mapper

    def explode(entity: Any, ctx: Any) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setitem(mapper._ENTRY_MAPPERS, "skills", explode)

    mapped = map_profile(raw_profile())

    assert "skills" in mapped.partial
    assert mapped.profile["experience"], "the other sections are untouched"
    assert mapped.profile["languages"]


def test_a_completely_broken_rawprofile_still_answers() -> None:
    """The outer guard. A bug in the mapper degrades honestly, never 500s."""

    class Exploding:
        @property
        def profile(self) -> Any:
            raise RuntimeError("boom")

        core: dict[str, Any] = {}
        sections: dict[str, SectionFetch] = {}
        truncated_sections: list[str] = []
        unresolved_sections: list[str] = []

    mapped = map_profile(Exploding())  # type: ignore[arg-type]

    assert mapped.profile == {}
    assert set(mapped.partial) == set(CONTRACT_FIELDS)


def test_the_mapper_never_mutates_what_it_was_given(full) -> None:
    """`resolve_elements` hands back the payload's OWN objects, not copies."""
    before = json.dumps(full.core, sort_keys=True)
    sections_before = {
        name: json.dumps(section.payload, sort_keys=True)
        for name, section in full.sections.items()
    }

    map_profile(full)

    assert json.dumps(full.core, sort_keys=True) == before
    for name, section in full.sections.items():
        assert json.dumps(section.payload, sort_keys=True) == sections_before[name]


# --- Structural agreement with the client -------------------------------------


def test_the_section_fields_are_exactly_the_clients_section_names() -> None:
    """`partial[]` naming is fixed upstream so the two cannot drift apart."""
    assert set(SECTION_FIELDS) == set(SECTION_RESOURCES)


def test_every_section_field_is_a_contract_field() -> None:
    assert set(SECTION_FIELDS) <= set(CONTRACT_FIELDS)


def test_the_dotted_path_names_a_real_field_and_a_real_sub_field() -> None:
    section, _, sub = EMPLOYMENT_TYPE_PATH.partition(".")

    assert section in SECTION_FIELDS
    assert sub == "employment_type"

"""Unit tests for the pure half of the Phase 5.4A Gmail digest.

Nothing here touches SQLite: these are the assembly ordering, the rendering,
the two identities, and the timezone boundary, tested as the pure functions
they are.
"""

from datetime import datetime, timedelta, timezone

import pytest

from services.eligibility import GlobalStatus
from services.gmail_digest import (
    DIGEST_VERSION,
    UNKNOWN_LOCATION_LABEL,
    DigestItem,
    GmailDigestError,
    digest_content_fingerprint,
    digest_sort_key,
    normalize_recipient,
    now_timestamp,
    recipient_fingerprint,
    render_digest,
    render_html,
    render_subject,
    render_text,
    resolve_local_day,
    resolve_timezone,
    validate_digest_url,
)
from services.portfolio import PortfolioBucket
from services.priority import PriorityCategory

TARGET, SAFE, AMBITIOUS = (
    PortfolioBucket.TARGET,
    PortfolioBucket.SAFE,
    PortfolioBucket.AMBITIOUS,
)
URGENT, HIGH, MEDIUM, LOW = (
    PriorityCategory.URGENT,
    PriorityCategory.HIGH,
    PriorityCategory.MEDIUM,
    PriorityCategory.LOW,
)


def item(opportunity_id=1, **changes):
    values = dict(
        title="Data Engineer",
        organization="Org",
        location="Casablanca",
        url=f"https://example.invalid/{opportunity_id}",
        bucket=TARGET,
        priority_category=MEDIUM,
        eligibility_status=GlobalStatus.UNKNOWN,
    )
    return DigestItem(opportunity_id, **(values | changes))


def test_ordering_is_bucket_then_priority_then_opportunity_id():
    items = [
        item(9, bucket=AMBITIOUS, priority_category=URGENT),
        item(3, bucket=TARGET, priority_category=MEDIUM),
        item(2, bucket=TARGET, priority_category=MEDIUM),
        item(7, bucket=SAFE, priority_category=LOW),
        item(1, bucket=TARGET, priority_category=HIGH),
        item(8, bucket=SAFE, priority_category=HIGH),
    ]

    ordered = sorted(items, key=digest_sort_key)

    assert [element.opportunity_id for element in ordered] == [1, 2, 3, 8, 7, 9]


def test_the_same_visible_content_always_fingerprints_the_same():
    items = (item(2, bucket=SAFE), item(1))

    assert digest_content_fingerprint(items) == digest_content_fingerprint(
        (item(2, bucket=SAFE), item(1))
    )


def test_reordering_the_same_opportunities_changes_the_fingerprint():
    """Order is content: a re-ranked digest is a different digest to read."""
    assert digest_content_fingerprint((item(1), item(2))) != (
        digest_content_fingerprint((item(2), item(1)))
    )


@pytest.mark.parametrize(
    "change",
    [
        {"title": "Senior Data Engineer"},
        {"organization": "Other Org"},
        {"location": "Rabat"},
        {"location": None},
        {"url": "https://example.invalid/other"},
        {"bucket": AMBITIOUS},
        {"priority_category": URGENT},
        {"eligibility_status": GlobalStatus.ELIGIBLE},
    ],
)
def test_every_visible_field_participates_in_the_fingerprint(change):
    assert digest_content_fingerprint((item(1),)) != digest_content_fingerprint(
        (item(1, **change),)
    )


def test_a_different_digest_version_is_a_different_digest():
    assert digest_content_fingerprint((item(1),)) != digest_content_fingerprint(
        (item(1),), digest_version="gmail-digest-v2"
    )


def test_the_subject_states_the_count_and_agrees_with_the_bodies():
    items = (item(1), item(2, bucket=SAFE))

    subject, text, html = render_digest(items)

    assert subject == "Opportunity Radar — 2 opportunités à examiner"
    assert render_subject((item(1),)) == "Opportunity Radar — 1 opportunité à examiner"
    assert text.startswith(subject)
    assert f"<h1>{subject}</h1>" in html


def test_text_and_html_carry_the_same_items_and_the_same_links():
    items = (
        item(1, title="A", organization="OrgA", location="Rabat"),
        item(2, title="B", organization="OrgB", location=None, bucket=SAFE),
    )

    text, html = render_text(items), render_html(items)

    for element in items:
        assert element.title in text and element.title in html
        assert element.organization in text and element.organization in html
        assert element.url in text and f'href="{element.url}"' in html
        assert element.bucket.value in text and element.bucket.value in html
        assert element.priority_category.value in text
        assert element.priority_category.value in html
        assert element.eligibility_status.value in text
        assert element.eligibility_status.value in html
    assert text.count(UNKNOWN_LOCATION_LABEL) == 1
    assert html.count(UNKNOWN_LOCATION_LABEL) == 1
    assert "Rabat" in text and "Rabat" in html


def test_unknown_eligibility_is_repeated_not_resolved():
    text = render_text((item(1, eligibility_status=GlobalStatus.UNKNOWN),))

    assert "UNKNOWN" in text
    assert "ELIGIBLE" not in text.replace("Éligibilité", "")


def test_html_escapes_every_dynamic_value_including_the_href():
    injected = item(
        1,
        title='<script>alert("x")</script>',
        organization="Org & Co",
        location='" onmouseover="x',
        url="https://example.invalid/a?b=1&c=2",
    )

    html = render_html((injected,))

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "Org &amp; Co" in html
    assert 'onmouseover="x"' not in html
    assert 'href="https://example.invalid/a?b=1&amp;c=2"' in html


def test_the_html_body_is_inert():
    html = render_html((item(1), item(2, bucket=AMBITIOUS)))

    lowered = html.lower()
    assert "<script" not in lowered
    assert "<img" not in lowered
    assert "onerror" not in lowered and "onload" not in lowered
    assert "javascript:" not in lowered
    assert "http://" not in lowered.replace("https://", "")


def test_an_empty_digest_has_no_subject_and_no_body():
    for renderer in (render_subject, render_text, render_html):
        with pytest.raises(GmailDigestError):
            renderer(())


def test_rendering_refuses_an_unknown_digest_version():
    with pytest.raises(GmailDigestError, match="cannot render digest version"):
        render_digest((item(1),), digest_version="gmail-digest-v2")


def test_rendering_is_byte_deterministic_across_repeated_calls():
    items = (item(3, bucket=AMBITIOUS), item(1), item(2, bucket=SAFE))

    assert render_digest(items) == render_digest(items)


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "mailto:someone@example.invalid",
        "/portfolio",
        "example.invalid/job",
        "ftp://example.invalid/job",
        "https://",
        "  https://example.invalid/job  ",
        "https://example.invalid/a b",
        "",
        None,
        42,
    ],
)
def test_only_a_real_absolute_http_link_is_accepted(url):
    with pytest.raises(GmailDigestError):
        validate_digest_url(url, 1)


@pytest.mark.parametrize(
    "url", ["https://example.invalid/job", "http://example.invalid/job?a=1#b"]
)
def test_a_real_absolute_http_link_is_returned_unchanged(url):
    assert validate_digest_url(url, 1) == url


def test_a_recipient_is_normalized_before_it_is_fingerprinted():
    assert recipient_fingerprint("  Owner@Example.Invalid ") == recipient_fingerprint(
        "owner@example.invalid"
    )
    assert recipient_fingerprint("other@example.invalid") != recipient_fingerprint(
        "owner@example.invalid"
    )
    assert normalize_recipient(" Owner@Example.Invalid ") == "owner@example.invalid"


def test_the_recipient_fingerprint_never_contains_the_address():
    fingerprint = recipient_fingerprint("owner@example.invalid")

    assert len(fingerprint) == 64 and set(fingerprint) <= set("0123456789abcdef")
    assert "owner" not in fingerprint and "example" not in fingerprint


@pytest.mark.parametrize(
    "recipient",
    ["", "owner", "owner@", "@example.invalid", "a@b@c.invalid", "owner@localhost", 7],
)
def test_a_recipient_that_is_not_one_address_is_refused(recipient):
    with pytest.raises(GmailDigestError):
        recipient_fingerprint(recipient)


def test_a_content_fingerprint_and_a_recipient_fingerprint_never_collide():
    """Both are SHA-256, so the recipient one is domain-separated on purpose."""
    assert recipient_fingerprint("owner@example.invalid") != (
        digest_content_fingerprint((item(1),))
    )


@pytest.mark.parametrize(
    "name", [None, "", "   ", " Africa/Casablanca", "Mars/Olympus_Mons", "UTC+1", 5]
)
def test_an_invalid_or_missing_timezone_fails_closed(name):
    with pytest.raises(GmailDigestError):
        resolve_timezone(name)
    with pytest.raises(GmailDigestError):
        resolve_local_day(name, datetime(2026, 9, 3, tzinfo=timezone.utc))


def test_the_day_boundary_is_decided_by_the_named_timezone():
    """23:30 UTC is already tomorrow in Casablanca and still yesterday in NY."""
    instant = datetime(2026, 9, 3, 23, 30, tzinfo=timezone.utc)

    assert resolve_local_day("Africa/Casablanca", instant).date == "2026-09-04"
    assert resolve_local_day("America/New_York", instant).date == "2026-09-03"
    assert resolve_local_day("Pacific/Kiritimati", instant).date == "2026-09-04"
    assert resolve_local_day("Pacific/Niue", instant).date == "2026-09-03"


def test_the_boundary_is_exact_to_the_second():
    midnight = datetime(2026, 9, 3, 23, 0, tzinfo=timezone.utc)

    assert resolve_local_day("Africa/Casablanca", midnight).date == "2026-09-04"
    assert (
        resolve_local_day("Africa/Casablanca", midnight - timedelta(seconds=1)).date
        == "2026-09-03"
    )


def test_the_resolved_day_records_the_timezone_that_decided_it():
    day = resolve_local_day(
        "Africa/Casablanca", datetime(2026, 9, 3, 12, tzinfo=timezone.utc)
    )

    assert (day.date, day.timezone) == ("2026-09-03", "Africa/Casablanca")


def test_a_naive_instant_is_refused_rather_than_assumed_to_be_utc():
    with pytest.raises(GmailDigestError, match="timezone-aware"):
        resolve_local_day("Africa/Casablanca", datetime(2026, 9, 3, 23, 30))
    with pytest.raises(GmailDigestError, match="timezone-aware"):
        now_timestamp(datetime(2026, 9, 3, 23, 30))


def test_row_timestamps_are_written_in_utc_whatever_day_was_decided():
    instant = datetime(2026, 9, 3, 23, 30, tzinfo=timezone.utc)

    assert now_timestamp(instant) == "2026-09-03 23:30:00"
    assert now_timestamp(instant.astimezone(timezone(timedelta(hours=5)))) == (
        "2026-09-03 23:30:00"
    )


def test_the_digest_version_is_the_one_this_phase_ships():
    assert DIGEST_VERSION == "gmail-digest-v1"

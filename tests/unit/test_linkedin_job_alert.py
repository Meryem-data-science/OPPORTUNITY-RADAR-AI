from services.collector.models.gmail_message import GmailMessageCandidate
from services.collector.parsers.linkedin_job_alert import (
    LINKEDIN_JOB_ALERT_SOURCE_ID,
    parse_linkedin_job_alert,
)


def message(*, sender="LinkedIn <alerts@linkedin.com>", subject="Jobs for you", html=None, text=None):
    return GmailMessageCandidate("m1", None, sender, subject, None, None, text, html)


def job(job_id="123456", title="Data Analyst", company="Example Labs", location="Rabat"):
    return f'''<div><a href="https://www.linkedin.com/jobs/view/{job_id}?trk=email">{title}</a>
    <div>{company}</div><div>{location}</div></div>'''


def test_html_single_job_has_normalized_candidate():
    candidate = parse_linkedin_job_alert(message(html=job()))[0]
    assert candidate.source_id == LINKEDIN_JOB_ALERT_SOURCE_ID
    assert candidate.source_external_id == "123456"
    assert candidate.canonical_title == "Data Analyst"
    assert candidate.organization == "Example Labs"
    assert candidate.location == "Rabat"
    assert candidate.source_url == "https://www.linkedin.com/jobs/view/123456"
    assert candidate.canonical_url == "https://www.linkedin.com/jobs/view/123456"
    assert candidate.application_url == candidate.canonical_url
    assert candidate.description is None
    assert candidate.published_at is None


def test_multiple_jobs_and_non_job_links():
    html = job("111", "First role", "Alpha", "Paris") + job("222", "Second role", "Beta", "Remote")
    html += '<a href="https://www.linkedin.com/settings">Settings</a><a href="https://www.linkedin.com/company/alpha">Alpha</a>'
    assert [c.source_external_id for c in parse_linkedin_job_alert(message(html=html))] == ["111", "222"]


def test_duplicate_job_is_stably_deduplicated():
    html = job("111", "First", "Alpha", "Paris") + job("111", "Duplicate", "Alpha", "Paris")
    candidates = parse_linkedin_job_alert(message(html=html))
    assert len(candidates) == 1
    assert candidates[0].canonical_title == "First"


def test_comm_url_and_tracking_are_canonicalized():
    html = '''<div><a href="http://www.linkedin.com/comm/jobs/view/987?savedSearchAuthToken=fake-auth&amp;otpToken=fake-otp&amp;midToken=fake-mid&amp;midSig=fake-sig#top">Engineer</a><div>Widgets</div></div>'''
    candidate = parse_linkedin_job_alert(message(html=html))[0]
    assert candidate.source_external_id == "987"
    assert candidate.source_url == "https://www.linkedin.com/jobs/view/987"
    assert candidate.canonical_url == "https://www.linkedin.com/jobs/view/987"
    assert candidate.application_url == "https://www.linkedin.com/jobs/view/987"
    assert all("?" not in url for url in (
        candidate.source_url, candidate.canonical_url, candidate.application_url
    ))


def test_combined_company_and_location_metadata_is_split_conservatively():
    cases = (
        ("UM6P - University Mohammed VI Polytechnic · Maroc", "UM6P - University Mohammed VI Polytechnic", "Maroc"),
        ("LabelVie · Casablanca et périphérie", "LabelVie", "Casablanca et périphérie"),
        ("Entreprise · Casablanca, Casablanca-Settat, Maroc", "Entreprise", "Casablanca, Casablanca-Settat, Maroc"),
    )
    for index, (metadata, organization, location) in enumerate(cases, start=300):
        candidate = parse_linkedin_job_alert(message(html=job(str(index), company=metadata, location="2 relations")))[0]
        assert (candidate.organization, candidate.location) == (organization, location)


def test_linkedin_ui_labels_are_never_locations():
    for index, label in enumerate(
        ("2 relations", "1 relation", "Recrutement actif", "Actively recruiting", "3 connections"),
        start=400,
    ):
        candidate = parse_linkedin_job_alert(message(html=job(str(index), location=label)))[0]
        assert candidate.organization == "Example Labs"
        assert candidate.location is None

    combined = parse_linkedin_job_alert(message(
        html=job("499", company="Example Labs · Recrutement actif", location="2 relations")
    ))[0]
    assert combined.organization == "Example Labs"
    assert combined.location is None


def test_multiple_blocks_do_not_cross_contaminate_metadata():
    html = job("501", company="Alpha · Maroc", location="2 relations") + job(
        "502", company="Beta · Paris", location="Recrutement actif"
    )
    candidates = parse_linkedin_job_alert(message(html=html))
    assert [(item.organization, item.location) for item in candidates] == [
        ("Alpha", "Maroc"), ("Beta", "Paris")
    ]


def test_sender_address_not_display_name_controls_acceptance():
    assert parse_linkedin_job_alert(message(sender="LinkedIn <alerts@evil.example>", html=job())) == []
    assert len(parse_linkedin_job_alert(message(sender="Name <alerts@linkedin.com>", html=job()))) == 1


def test_subject_wording_and_language_are_irrelevant():
    for subject in ("New jobs", "Nouvelles offres", "An unusual digest subject"):
        assert len(parse_linkedin_job_alert(message(subject=subject, html=job()))) == 1


def test_title_whitespace_and_entity_are_cleaned_only():
    html = job(title="  Senior\n Data &amp; Insights Analyst  ")
    assert parse_linkedin_job_alert(message(html=html))[0].canonical_title == "Senior Data & Insights Analyst"


def test_missing_location_is_none():
    html = '<div><a href="https://linkedin.com/jobs/view/12">Engineer</a><div>Widgets</div></div>'
    assert parse_linkedin_job_alert(message(html=html))[0].location is None


def test_missing_organization_or_title_is_skipped():
    no_company = '<div><a href="https://linkedin.com/jobs/view/12">Engineer</a></div>'
    no_title = '<div><a href="https://linkedin.com/jobs/view/13"></a><div>Widgets</div></div>'
    assert parse_linkedin_job_alert(message(html=no_company + no_title)) == []


def test_plain_text_fallback_requires_explicit_three_line_sequence():
    text = "Platform Engineer\nExample Systems\nhttps://www.linkedin.com/jobs/view/765?trk=mail\n"
    candidate = parse_linkedin_job_alert(message(html=None, text=text))[0]
    assert (candidate.canonical_title, candidate.organization, candidate.location) == (
        "Platform Engineer", "Example Systems", None)


def test_non_linkedin_message_no_job_and_unsupported_urls_are_empty():
    assert parse_linkedin_job_alert(message(sender="news@example.test", html=job())) == []
    assert parse_linkedin_job_alert(message(html='<a href="https://www.linkedin.com/feed/">Feed</a>')) == []
    assert parse_linkedin_job_alert(message(html='<div><a href="https://jobs.linkedin.example/jobs/view/1">Role</a><div>Company</div></div>')) == []
    assert parse_linkedin_job_alert(message(html='<div><a href="javascript:void(0)">Role</a><div>Company</div></div>')) == []

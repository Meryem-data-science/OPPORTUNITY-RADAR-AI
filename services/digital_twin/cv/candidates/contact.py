"""Conservative contact rules: email addresses, phone numbers and URLs.

Every rule here is a regular expression plus a closed dictionary, so what was
matched and why is readable in this file. The rules are deliberately narrow:
they would rather miss a contact written in an unusual way than assert one that
is not there, and none of them ever adds information the CV did not write —
no country code is inferred for a phone number, no scheme or host is completed
for a URL, and no link is called a portfolio because it looks like one.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass

from services.digital_twin.cv.candidates.models import CandidateType, ExtractionRule
from services.digital_twin.cv.candidates.text import fold_words, holds_phrase

#: A local part, an "@", and a host with at least one dot and a letters-only
#: last label. Narrow on purpose: it will not match a bare "@handle", and the
#: required dotted host keeps it from firing inside a URL path.
EMAIL_PATTERN = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9][A-Za-z0-9.\-]*\.[A-Za-z]{2,}"
)

#: A URL written with its scheme. Everything up to the first whitespace or
#: quote belongs to it; trailing sentence punctuation is trimmed afterwards.
_SCHEME_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
#: A URL written without a scheme but opened by "www.".
_WWW_URL = re.compile(
    r"(?<![\w@.\-/])www\.[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)

#: Hosts a CV writes bare, with neither scheme nor "www.". Only these are
#: recognised without a scheme: matching any dotted token would turn ordinary
#: prose and file names into links. Extending this tuple is the one way to
#: recognise another bare host, and it is a change of the extractor rules.
KNOWN_BARE_HOSTS: tuple[str, ...] = (
    "github.com",
    "github.io",
    "gitlab.com",
    "bitbucket.org",
    "linkedin.com",
    "kaggle.com",
    "huggingface.co",
    "stackoverflow.com",
    "medium.com",
    "orcid.org",
    "behance.net",
    "dribbble.com",
)
_BARE_HOST_URL = re.compile(
    r"(?<![\w@.\-/])(?:[A-Za-z0-9\-]+\.)*(?:"
    + "|".join(re.escape(host) for host in KNOWN_BARE_HOSTS)
    + r")(?:/[^\s<>\"']*)?",
    re.IGNORECASE,
)

#: Trailing characters a sentence leaves stuck to a URL.
_URL_TRAILING = ".,;:!?)]}>\"'»"

#: A run of digits and phone separators. "/" is not a separator, so a date such
#: as "01/2020" is never read as a phone number, and the run must both start
#: and end on a digit.
_PHONE_PATTERN = re.compile(r"(?<![\w+])\+?\d[\d .\-–—()]{7,20}\d(?![\w])")
#: E.164 allows at most 15 digits; below 9 a run is a year, a postcode or an
#: identifier far more often than it is a phone number.
_PHONE_MIN_DIGITS = 9
_PHONE_MAX_DIGITS = 15
#: Closed dictionary of words a CV puts in front of a phone number.
PHONE_LABELS: tuple[str, ...] = (
    "tel",
    "telephone",
    "phone",
    "mobile",
    "gsm",
    "portable",
    "cell",
    "cellphone",
)
#: Closed dictionary of words a CV puts in front of a personal site. A URL is
#: only ever called a portfolio when one of these introduces it on its line.
PORTFOLIO_LABELS: tuple[str, ...] = (
    "portfolio",
    "site",
    "site web",
    "site perso",
    "site personnel",
    "website",
    "web site",
    "personal site",
    "personal website",
    "mon site",
)


@dataclass(frozen=True)
class ContactMatch:
    """One contact value found on one line, with the rule that found it."""

    candidate_type: CandidateType
    rule_id: ExtractionRule
    #: Exactly the characters the CV wrote, with nothing added or completed.
    raw_text: str
    #: A technical normal form, or `None` where none is unambiguous.
    normalized_value: str | None
    start: int
    end: int


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(
        start < taken_end and taken_start < end for taken_start, taken_end in spans
    )


def hostname_of(url: str) -> str:
    """Return the lowercased host of a URL, without scheme, userinfo or port."""
    without_scheme = re.sub(r"^https?://", "", url, flags=re.IGNORECASE)
    authority = re.split(r"[/?#]", without_scheme, maxsplit=1)[0]
    host = authority.rsplit("@", 1)[-1].split(":", 1)[0]
    return host.casefold()


def _host_is(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def classify_url(url: str, *, line_prefix: str) -> tuple[CandidateType, ExtractionRule]:
    """Decide what kind of link this is, from its host and its label only.

    GitHub and LinkedIn are decided by hostname, which is a fact about the URL.
    Anything else is a portfolio only when a label of `PORTFOLIO_LABELS`
    introduces it earlier on the same line; otherwise it stays
    `PROFESSIONAL_URL`, which claims nothing beyond "this is a link the CV
    carries". Guessing "portfolio" from a personal-looking domain is exactly
    the invention this slice refuses.
    """
    host = hostname_of(url)
    if _host_is(host, "github.com") or _host_is(host, "github.io"):
        return CandidateType.GITHUB_URL, ExtractionRule.URL_GITHUB_HOST
    if _host_is(host, "linkedin.com"):
        return CandidateType.LINKEDIN_URL, ExtractionRule.URL_LINKEDIN_HOST
    folded_prefix = fold_words(line_prefix)
    if any(holds_phrase(folded_prefix, label) for label in PORTFOLIO_LABELS):
        return CandidateType.PORTFOLIO_URL, ExtractionRule.URL_PORTFOLIO_LABELLED_LINE
    return CandidateType.PROFESSIONAL_URL, ExtractionRule.URL_UNLABELLED_PROFESSIONAL


def _phone_rule(
    matched: str, *, line_prefix: str, in_header: bool
) -> ExtractionRule | None:
    """Return the rule that justifies reading this run as a phone number.

    An explicit "+" prefix is unambiguous anywhere. A phone label announces one
    anywhere. A leading trunk zero is only trusted inside the header block,
    where a CV writes its contact details: elsewhere a run opening on a zero is
    as likely to be a date or a reference, and a wrong phone number is worse
    than a missing one.
    """
    compact = matched.strip()
    if compact.startswith("+"):
        return ExtractionRule.PHONE_INTERNATIONAL_PREFIX
    folded_prefix = fold_words(line_prefix)
    if any(holds_phrase(folded_prefix, label) for label in PHONE_LABELS):
        return ExtractionRule.PHONE_LABELLED_LINE
    if in_header and compact.startswith("0"):
        return ExtractionRule.PHONE_HEADER_TRUNK_ZERO
    return None


def _compact_phone(matched: str) -> str:
    """Return the digits of the run, keeping a "+" only if the CV wrote one.

    No country code, area code or leading zero is ever added: a number the CV
    wrote without a prefix stays without one, so a reader can see that the
    country is unknown rather than being told a wrong one.
    """
    digits = "".join(character for character in matched if character.isdigit())
    return f"+{digits}" if matched.strip().startswith("+") else digits


def find_contacts(line_text: str, *, in_header: bool) -> Iterator[ContactMatch]:
    """Yield every contact value on one line, emails first, then URLs, then phones.

    The order is also the precedence: a span already claimed by an email is not
    re-read as a URL, and a span claimed by either is not re-read as a phone
    number, so one piece of text never becomes two contradictory candidates.
    """
    taken: list[tuple[int, int]] = []

    for match in EMAIL_PATTERN.finditer(line_text):
        taken.append(match.span())
        yield ContactMatch(
            candidate_type=CandidateType.EMAIL,
            rule_id=ExtractionRule.EMAIL_PATTERN,
            raw_text=match.group(),
            # Lowercasing is the one normalization applied, and it is kept in
            # its own field: `raw_text` still holds what the CV wrote.
            normalized_value=match.group().casefold(),
            start=match.start(),
            end=match.end(),
        )

    for pattern in (_SCHEME_URL, _WWW_URL, _BARE_HOST_URL):
        for match in pattern.finditer(line_text):
            url = match.group().rstrip(_URL_TRAILING)
            if not url:
                continue
            start, end = match.start(), match.start() + len(url)
            if _overlaps(start, end, taken):
                continue
            taken.append((start, end))
            candidate_type, rule_id = classify_url(
                url, line_prefix=line_text[:start]
            )
            yield ContactMatch(
                candidate_type=candidate_type,
                rule_id=rule_id,
                raw_text=url,
                # No canonical URL form: dropping a scheme, a "www." or a
                # trailing slash would already be a decision about the link.
                normalized_value=None,
                start=start,
                end=end,
            )

    for match in _PHONE_PATTERN.finditer(line_text):
        if _overlaps(match.start(), match.end(), taken):
            continue
        digit_count = sum(character.isdigit() for character in match.group())
        if not _PHONE_MIN_DIGITS <= digit_count <= _PHONE_MAX_DIGITS:
            continue
        rule_id = _phone_rule(
            match.group(), line_prefix=line_text[: match.start()], in_header=in_header
        )
        if rule_id is None:
            continue
        taken.append(match.span())
        yield ContactMatch(
            candidate_type=CandidateType.PHONE,
            rule_id=rule_id,
            raw_text=match.group().strip(),
            normalized_value=_compact_phone(match.group()),
            start=match.start(),
            end=match.end(),
        )

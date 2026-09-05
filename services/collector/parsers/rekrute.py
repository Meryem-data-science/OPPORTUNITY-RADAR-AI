"""Network-free ReKrute parsing foundation (Phase 7C.3A).

WHAT THIS MODULE IS, AND WHAT IT DELIBERATELY IS NOT
====================================================

7C.3A was asked to verify ReKrute's public access **before** writing a parser
against it. That verification could not be completed: outbound HTTPS from the
Claude Code Cloud session is filtered by an egress policy that answers 403 to
`CONNECT www.rekrute.com:443` (and to `rekrute.com:443`). DNS resolves and
unrelated hosts return 200, so the denial is this sandbox's allow-list, **not**
a ReKrute-side block — no CAPTCHA, bot challenge or site 403 was ever observed,
because no byte of ReKrute was ever received.

Consequently this module contains **no listing-page parser and no detail-page
parser**. Extracting offer links or offer fields requires knowing ReKrute's
real markup, and inventing that markup is precisely what the phase forbids.
Those two functions are 7C.3B work, once
`evaluation.morocco_pfe.cli.rekrute_access_audit` has been run from a network
that may reach the site.

What *is* here is the part that owes nothing to ReKrute's HTML:

* `RekruteOfferRecord` — the source-shaped record a future detail parser will
  populate. Its field list is the architect's specification, not observed
  evidence, and every field except identity is optional precisely so that the
  record asserts the existence of nothing;
* `canonical_offer_url` — host/scheme policy and a deterministic canonical
  form, written to be safe under ignorance of ReKrute's URL grammar;
* `assess_target_evidence` — the locked PFE/stage targeting rule;
* `to_opportunity_candidate` — the bridge to the shared, already-shipped model.

SOURCE PARSING vs SHARED OPPORTUNITY-TYPE CLASSIFICATION
========================================================

The division this phase is asked to document:

* **source parsing** (this module) reports *facts ReKrute states*: the contract
  field it publishes, the title it publishes, the text it publishes. It decides
  one narrow question — whether those facts are explicit PFE/stage evidence —
  and it answers it from the source's own structured contract field first.
* **shared classification** (`services.collector.qualification.classifier`)
  decides the cross-source questions: `OpportunityType`, `Domain`, Data/AI
  qualification. It is geography- and source-neutral and stays that way.

So this module does not re-declare a taxonomy: it *imports* `normalize_text`
and the PFE vocabulary from the Phase 7B taxonomy, and adds only the one thing
7B has no notion of — a source-published contract field. Data & AI filtering is
Phase 8 and appears nowhere here.
"""

from __future__ import annotations

from dataclasses import dataclass
from html import unescape
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from services.collector.models.opportunity import OpportunityCandidate
from services.collector.qualification.classifier import normalize_text
from services.collector.qualification.taxonomy import (
    DESCRIPTION_PFE_SIGNALS,
    PFE_SIGNALS,
)

#: The identifier a future `config/sources.yaml` row would use. Declaring the
#: string here activates nothing: ReKrute is absent from the production
#: registry, absent from `SourceConfig`'s accepted types and absent from the
#: collector factory in 7C.3A, by design.
REKRUTE_SOURCE_ID = "rekrute"

#: Taken from the source map's `homepage_url`, whose status there is
#: WELL_KNOWN_UNVERIFIED and stays that way: we never reached the host.
REKRUTE_HOSTS = frozenset({"rekrute.com", "www.rekrute.com"})
_CANONICAL_HOST = "www.rekrute.com"

#: Query parameters that are tracking noise on *every* site, so removing them
#: needs no knowledge of ReKrute's URL grammar. Nothing else is stripped: until
#: the audit shows where the offer id lives, discarding an unrecognised
#: parameter could silently destroy offer identity.
_TRACKING_PARAMS = frozenset(
    {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "utm_id", "gclid", "fbclid", "msclkid", "mc_cid", "mc_eid",
    }
)

#: Values a *structured contract field* may carry that are explicit stage
#: evidence. This vocabulary is applied to the contract field ONLY, never to
#: free text, which is what keeps "une première expérience ou un stage" in a
#: CDI description from creating a stage opportunity.
_STAGE_CONTRACT_SIGNALS = ("stage", "stagiaire", "internship", "pfe")

#: Characters RFC 3986 forbids in an unencoded URL, plus C0/C1 controls.
_UNSAFE_URL_CHARACTERS = re.compile(r"""[<>"`{}|\\^\[\]\x00-\x20\x7f-\x9f]""")


class RekruteParseError(ValueError):
    """Raised when ReKrute content cannot yield a usable, non-fabricated offer."""


def _clean(value: str | None) -> str | None:
    """Collapse whitespace; an empty or whitespace-only value becomes ``None``."""
    if value is None:
        return None
    collapsed = " ".join(unescape(value).split())
    return collapsed or None


def canonical_offer_url(url: str | None) -> str:
    """Return a deterministic canonical ReKrute URL, or raise.

    Deliberately conservative about what it removes. It normalizes the scheme
    to https and the host to ``www.rekrute.com``, drops the fragment (never
    identity), drops universally-tracking query parameters, and sorts whatever
    query remains so the same URL always canonicalizes to the same string.

    It does **not** rewrite, shorten or reinterpret the path, and it does not
    discard unrecognised query parameters, because 7C.3A never observed where
    ReKrute puts an offer id. Non-http(s) URLs and non-ReKrute hosts are
    rejected rather than coerced.
    """
    candidate = _clean(url)
    if candidate is None:
        raise RekruteParseError("ReKrute URL is missing")
    # RFC 3986 excludes these unencoded; a real link never carries them, and
    # tolerating them lets malformed markup produce plausible-looking garbage.
    if _UNSAFE_URL_CHARACTERS.search(candidate):
        raise RekruteParseError(f"ReKrute URL contains invalid characters: {candidate!r}")
    try:
        parsed = urlsplit(candidate)
    except ValueError as error:
        raise RekruteParseError(f"ReKrute URL is unparseable: {candidate!r}") from error
    if parsed.scheme.lower() not in {"http", "https"}:
        raise RekruteParseError(f"ReKrute URL must be http(s): {candidate!r}")
    host = (parsed.hostname or "").lower().rstrip(".")
    if host not in REKRUTE_HOSTS:
        raise RekruteParseError(f"URL is not a ReKrute host: {candidate!r}")
    query = urlencode(
        sorted(
            (name, value)
            for name, value in parse_qsl(parsed.query, keep_blank_values=True)
            if name.lower() not in _TRACKING_PARAMS
        )
    )
    return urlunsplit(("https", _CANONICAL_HOST, parsed.path, query, ""))


def is_rekrute_url(url: str | None) -> bool:
    """Return whether ``url`` is an acceptable public ReKrute http(s) URL."""
    try:
        canonical_offer_url(url)
    except RekruteParseError:
        return False
    return True


@dataclass(frozen=True)
class RekruteOfferRecord:
    """One ReKrute offer as the *source* states it, before any shared model.

    Exists because ReKrute is expected to publish `deadline` and a contract
    type, which `OpportunityCandidate` has no column for. Rather than widen the
    shared model across the whole application in 7C.3A, the extra evidence is
    kept here and `to_opportunity_candidate` bridges only what the shared model
    already supports. 7C.3B decides whether the shared model should grow.

    Identity, title and organization are required: a record that cannot name
    those is a parse failure, never a row with invented values. Every other
    field is `None` when the source did not state it.
    """

    external_id: str
    title: str
    organization: str
    source_url: str
    location: str | None = None
    description: str | None = None
    published_at: str | None = None
    deadline: str | None = None
    contract_type: str | None = None
    application_url: str | None = None

    @classmethod
    def from_fields(
        cls,
        *,
        external_id: str | None,
        title: str | None,
        organization: str | None,
        source_url: str | None,
        location: str | None = None,
        description: str | None = None,
        published_at: str | None = None,
        deadline: str | None = None,
        contract_type: str | None = None,
        application_url: str | None = None,
    ) -> "RekruteOfferRecord":
        """Normalize whitespace and refuse to build a record missing identity."""
        required = {
            "external_id": _clean(external_id),
            "title": _clean(title),
            "organization": _clean(organization),
        }
        for name, value in required.items():
            if value is None:
                raise RekruteParseError(f"ReKrute offer is missing {name}")
        canonical = canonical_offer_url(source_url)
        # An application URL is carried only when it is genuinely a different
        # public page; echoing the source URL would invent a distinction.
        application = _clean(application_url)
        if application is not None:
            application = canonical_offer_url(application)
            if application == canonical:
                application = None
        return cls(
            external_id=required["external_id"],
            title=required["title"],
            organization=required["organization"],
            source_url=canonical,
            location=_clean(location),
            description=_clean(description),
            published_at=_clean(published_at),
            deadline=_clean(deadline),
            contract_type=_clean(contract_type),
            application_url=application,
        )

    def with_fields(self, **changes: object) -> "RekruteOfferRecord":
        """Return a copy with ``changes`` applied, re-running normalization."""
        merged = {
            "external_id": self.external_id,
            "title": self.title,
            "organization": self.organization,
            "source_url": self.source_url,
            "location": self.location,
            "description": self.description,
            "published_at": self.published_at,
            "deadline": self.deadline,
            "contract_type": self.contract_type,
            "application_url": self.application_url,
        }
        merged.update(changes)  # type: ignore[arg-type]
        return type(self).from_fields(**merged)  # type: ignore[arg-type]


@dataclass(frozen=True)
class TargetEvidence:
    """Why one offer is, or is not, PFE/stage evidence — always explainable."""

    is_target: bool
    contract_signals: tuple[str, ...]
    pfe_signals: tuple[str, ...]
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "is_target": self.is_target,
            "contract_signals": list(self.contract_signals),
            "pfe_signals": list(self.pfe_signals),
            "reasons": list(self.reasons),
        }


def _matched(text: str, signals: tuple[str, ...]) -> tuple[str, ...]:
    """Word-boundary phrase matching over already-normalized text."""
    words = text.split()
    return tuple(
        signal
        for signal in signals
        if (parts := signal.split())
        and any(
            words[index : index + len(parts)] == parts
            for index in range(len(words) - len(parts) + 1)
        )
    )


def assess_target_evidence(record: RekruteOfferRecord) -> TargetEvidence:
    """Apply the locked PFE/stage rule to one parsed ReKrute record.

    An offer is target evidence when **either**:

    A. the source's own structured contract field explicitly says stage — this
       is a field ReKrute publishes, not a phrase found in prose; **or**
    B. the title or the offer text carries explicit PFE evidence ("PFE",
       "projet de fin d'études" and the Phase 7B spellings of it).

    Everything else is rejected. In particular a CDI whose description happens
    to read "une première expérience ou un stage..." matches neither branch:
    the contract field says CDI, and an incidental "stage" in prose is not PFE
    evidence. Generic internship vocabulary in free text is deliberately never
    sufficient on its own.
    """
    contract = normalize_text(record.contract_type)
    title = normalize_text(record.title)
    description = normalize_text(record.description)

    contract_signals = _matched(contract, _STAGE_CONTRACT_SIGNALS)
    title_pfe = _matched(title, PFE_SIGNALS)
    description_pfe = _matched(description, DESCRIPTION_PFE_SIGNALS)
    pfe_signals = tuple(dict.fromkeys(title_pfe + description_pfe))

    reasons: list[str] = []
    if contract_signals:
        reasons.append(
            "source contract field explicitly states a stage contract: "
            + ", ".join(contract_signals)
        )
    if title_pfe:
        reasons.append("explicit PFE evidence in title: " + ", ".join(title_pfe))
    if description_pfe:
        reasons.append(
            "explicit PFE evidence in offer text: " + ", ".join(description_pfe)
        )
    if not reasons:
        reasons.append(
            "no explicit stage contract field and no explicit PFE evidence; "
            "incidental internship vocabulary in free text is not sufficient"
        )
    return TargetEvidence(
        is_target=bool(contract_signals or pfe_signals),
        contract_signals=contract_signals,
        pfe_signals=pfe_signals,
        reasons=tuple(reasons),
    )


def to_opportunity_candidate(record: RekruteOfferRecord) -> OpportunityCandidate:
    """Bridge a ReKrute record onto the shared, unchanged candidate model.

    `deadline` and `contract_type` have no column in `OpportunityCandidate` and
    are **not** smuggled into another field. They stay on the record, where
    7C.3B can decide whether the shared model should carry them. Nothing here
    persists anything: `OpportunityCandidate` is the pre-database shape.
    """
    return OpportunityCandidate(
        source_id=REKRUTE_SOURCE_ID,
        source_external_id=record.external_id,
        canonical_title=record.title,
        organization=record.organization,
        location=record.location,
        description=record.description,
        published_at=record.published_at,
        source_url=record.source_url,
        application_url=record.application_url or record.source_url,
        canonical_url=record.source_url,
    )


#: Fields the record carries that `to_opportunity_candidate` cannot express.
#: Named explicitly so the loss is documented rather than discovered later.
UNBRIDGED_RECORD_FIELDS = ("deadline", "contract_type")

__all__ = [
    "REKRUTE_SOURCE_ID",
    "REKRUTE_HOSTS",
    "UNBRIDGED_RECORD_FIELDS",
    "RekruteOfferRecord",
    "RekruteParseError",
    "TargetEvidence",
    "assess_target_evidence",
    "canonical_offer_url",
    "is_rekrute_url",
    "to_opportunity_candidate",
]

"""The blind evidence view: what the annotator is allowed to see, and only that.

The methodological centre of Phase 10.2. A human judgement is worth measuring
against only if it was made **independently of the thing being measured**. Show
an annotator that the Recommendation Engine ranked a posting first and their
grade stops being an opinion about the posting; it becomes an opinion about the
ranking. The measurement then agrees with itself and says nothing.

So the view is built as an **allowlist**, never as a filter. Filtering says
"remove the fields we know are dangerous", which is a promise about today's
schema: the day Phase 10.1 gains a `posting_score` field, a filter lets it
through and nobody notices until the numbers are already published. An allowlist
says "show these fields", and a new field is invisible by default. The cost of
the allowlist being wrong is a missing piece of evidence, which an annotator
will complain about. The cost of a filter being wrong is a silently
contaminated benchmark, which nobody will.

Two further guards sit on top of the allowlist:

* every field of the Phase 10.1 record contract is classified — `VISIBLE_*` or
  `WITHHELD_*`, with nothing left over. A field that belongs to neither is a
  field somebody added without deciding, and a test refuses that state, so the
  decision cannot be deferred past the commit that introduces it;
* the payload this module returns is scanned for forbidden key tokens *at
  runtime*, not only in tests. A view that somehow acquired a `score` key raises
  instead of being shown.

**What the annotator sees** is the frozen evidence: the title, the organization,
the location text, the posting's own description verbatim, the dates, the URLs
and where the posting was collected from. Nothing is trimmed, summarised or
cleaned — edited evidence is not evidence — and the description reaches the view
as exactly the string the dataset froze.

**What is withheld before judgement** is every verdict and every score the
system produced about this posting: the qualification and its fine
classification, the geography resolution, the eligibility decision, the matching
lane and its quality, the recommendation rank, disposition and score, and every
assessment fingerprint. The same rule binds any future UI or CLI: they render
this payload, they do not assemble their own.

Two of the withheld fields deserve their names said out loud, because they do
not look like verdicts. `status` and `is_active` are the cohort's own selection
criteria, identical across every record in a Phase 10.1 dataset, so they carry
no evidence and are omitted rather than defended. `absorbed_duplicate_ids` is
the deduplicator's output — a system judgement about identity — and while it is
harmless to a relevance grade it is still a machine's conclusion, so it stays on
the withheld side where the burden of proof belongs.

`opportunity_type` and `employment_type`, by contrast, *are* shown: those are
the nullable columns of the `opportunities` table itself, written when the
posting was collected. The classifier's own opinion about the type of the
opportunity lives inside the withheld `qualification` block and does not reach
the annotator.

Selection may look at everything. `selection.py` stratifies on the system's
outputs on purpose — that is how a small lot ends up containing a top-ranked
posting *and* one the engine never ranked. Judgement is what must stay blind,
and the two concerns are in two modules for exactly that reason.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .frozen import (
    EVALUATION_RECORD_CONTRACT_FIELDS,
    FrozenEvaluationDataset,
)
from .schema import HumanLabelError

__all__ = [
    "BLIND_VIEW_VERSION",
    "FORBIDDEN_VIEW_KEY_TOKENS",
    "VISIBLE_RECORD_FIELDS",
    "VISIBLE_SOURCE_FIELDS",
    "WITHHELD_RECORD_FIELDS",
    "BlindnessViolation",
    "blind_evidence_payload",
]

#: The version of the annotator's view. It moves whenever the evidence shown
#: changes, because two judgements made on two different views were made on two
#: different questions.
BLIND_VIEW_VERSION = "blind-evidence-v1"


class BlindnessViolation(HumanLabelError):
    """Raised when a view about to be shown carries a system verdict.

    A programming error, not a user error: it means the allowlist and the
    payload builder disagree. It is raised rather than logged because the only
    safe thing to do with a contaminated view is to not show it.
    """


#: The evidence. Every entry is a field of the Phase 10.1 record contract, and
#: every entry is something a person reads off the posting itself.
VISIBLE_RECORD_FIELDS: tuple[str, ...] = (
    "opportunity_id",
    "canonical_title",
    "organization",
    "opportunity_type",
    "employment_type",
    "location",
    "country",
    "remote_type",
    "description",
    "source_url",
    "application_url",
    "canonical_url",
    "published_at",
    "deadline",
    "discovered_at",
    "first_seen_at",
    "last_seen_at",
    "sources",
)

#: Everything else in the record contract, named explicitly so that the two
#: lists together account for the whole contract and a newly added field lands
#: in neither until somebody decides.
WITHHELD_RECORD_FIELDS: tuple[str, ...] = (
    # System verdicts and scores — the things being measured.
    "qualification",
    "geography_segments",
    "eligibility",
    "matching",
    "recommendation",
    # Cohort criteria: constant across the dataset, so evidence-free.
    "status",
    "is_active",
    # The deduplicator's conclusion about identity.
    "absorbed_duplicate_ids",
)

#: Collection provenance, which is evidence: an annotator judging a posting is
#: entitled to know it came from a company careers page rather than an
#: aggregator, and when it was seen there.
VISIBLE_SOURCE_FIELDS: tuple[str, ...] = (
    "source_id",
    "source_type",
    "source_url",
    "application_url",
    "canonical_url",
    "discovered_at",
)

#: Substrings that must not appear in any key of a blind view. Not the
#: mechanism that keeps the view clean — the allowlist is — but the alarm that
#: goes off if the mechanism is ever bypassed, including by a future field whose
#: name nobody here has seen.
FORBIDDEN_VIEW_KEY_TOKENS: tuple[str, ...] = (
    "qualification",
    "eligib",
    "matching",
    "match_quality",
    "recommend",
    "rank",
    "disposition",
    "score",
    "lane",
    "coverage",
    "verdict",
    "assessment",
    "reason",
    "classifier",
    "resolver",
    "geography_segments",
)


def _assert_no_forbidden_key(payload: Any, path: str = "") -> None:
    """Walk the payload and refuse any key naming a system output."""
    if isinstance(payload, Mapping):
        for key, value in payload.items():
            lowered = str(key).lower()
            for token in FORBIDDEN_VIEW_KEY_TOKENS:
                if token in lowered:
                    raise BlindnessViolation(
                        f"the blind evidence view carries key "
                        f"{path}{key!r}, which names a system output "
                        f"({token!r}); the annotator must judge the evidence, "
                        "not the pipeline's verdict about it"
                    )
            _assert_no_forbidden_key(value, f"{path}{key}.")
    elif isinstance(payload, (list, tuple)):
        for index, item in enumerate(payload):
            _assert_no_forbidden_key(item, f"{path}{index}.")


def _sources_view(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    sources = record.get("sources") or ()
    view: list[dict[str, Any]] = []
    for source in sources:
        if not isinstance(source, Mapping):
            raise BlindnessViolation("a source entry is not an object")
        view.append({key: source.get(key) for key in VISIBLE_SOURCE_FIELDS})
    return view


def blind_evidence_payload(
    dataset: FrozenEvaluationDataset, opportunity_id: int
) -> dict[str, Any]:
    """The exact payload an annotator may see before judging one opportunity.

    A pure function of a verified dataset and an id: it reads no file, consults
    no label, and returns the same structure every time. Any interface — this
    slice's CLI, a later UI — renders *this*; none of them assembles evidence of
    its own, which is what keeps one allowlist authoritative instead of one per
    front end.

    The envelope carries the dataset and profile identity so that a judgement
    can be traced to what it was made against. Those are identifiers, not
    verdicts: they say *which* frozen artefact was on screen and say nothing
    about what the pipeline concluded about this posting.
    """
    record = dataset.record(opportunity_id)
    evidence: dict[str, Any] = {}
    for field in VISIBLE_RECORD_FIELDS:
        if field == "sources":
            evidence[field] = _sources_view(record)
            continue
        evidence[field] = record.get(field)

    payload = {
        "blind_view_version": BLIND_VIEW_VERSION,
        "dataset_id": dataset.dataset_id,
        "dataset_content_fingerprint": dataset.content_fingerprint,
        "profile_id": dataset.profile_id,
        "profile_context_fingerprint": dataset.profile_context_fingerprint,
        "opportunity": evidence,
        "withheld_until_judged": list(WITHHELD_RECORD_FIELDS),
    }
    # Belt and braces, at runtime and not only under test: a view that acquired
    # a forbidden key is never returned.
    _assert_no_forbidden_key(payload["opportunity"])
    return payload


def unclassified_record_fields() -> tuple[str, ...]:
    """Contract fields that are neither shown nor explicitly withheld.

    Empty is the only acceptable answer. Exposed as a function so the guard can
    be asserted from a test *and* read by a person wondering what the current
    state of the contract is.
    """
    classified = set(VISIBLE_RECORD_FIELDS) | set(WITHHELD_RECORD_FIELDS)
    return tuple(
        field
        for field in EVALUATION_RECORD_CONTRACT_FIELDS
        if field not in classified
    )

"""Drawing a small, varied calibration lot from a frozen dataset, deterministically.

**What this is for, and what it is not.** Phase 10.2 does not fix the size of
the Phase 10 benchmark, and nothing here hardcodes one: the caller states `N`
and gets `N`. The lot this module draws exists to *exercise the rubric* — to put
a person in front of cases varied enough that the four grades, the three
diagnostics and their unknown members are all tested against reality before any
of them is declared final. It is a calibration instrument, not a statistical
sample, and it makes no claim to be representative of the 394.

**Selection may read the system's verdicts; judgement may not.** That asymmetry
is the design. A lot drawn blind would, with high probability, be 20 mid-ranked
postings that all look alike, and the rubric's edges would never be touched. So
the selector stratifies on exactly the outputs the annotator is forbidden to
see — the recommendation rank, the qualification, the geography resolution — and
`blind.py` then removes every one of them from what reaches the annotator. The
two modules are separate for this reason and no other.

**The stratification, in full.** Each record is assigned a three-part stratum:

* *recommendation band* — `RECOMMENDED_HIGH`, `RECOMMENDED_MID`,
  `RECOMMENDED_LOW` for postings the current Recommendation run ranked, split
  into thirds by their position among the ranked postings of this dataset, and
  `NOT_RECOMMENDED` for the postings it never ranked at all. That last group is
  the one no recommendation output can show you, and the one a false-negative
  analysis lives on;
* *qualification bucket* — the persisted Data/AI verdict, carried verbatim
  (`CORE_TARGET`, `ADJACENT_TARGET`, `UNCERTAIN`, `OUT_OF_SCOPE`), plus
  `QUALIFICATION_ABSENT` for a posting the classifier has never read. Absent is
  its own bucket, never folded into `OUT_OF_SCOPE`;
* *geography bucket* — `GEOGRAPHY_RESOLVED` when at least one segment of the
  location text resolved to a country, `GEOGRAPHY_UNRESOLVED` when segments
  exist and none did, `GEOGRAPHY_UNSEGMENTED` when the resolver produced
  nothing. A coarse read of Phase 7A's output, used only to spread the lot.

Within that, a *diversity key* — the collection source type and the posting's
own opportunity type — spreads the picks across sources and kinds of posting
without becoming a stratum of its own (which would multiply the strata past the
size of any calibration lot).

**The algorithm**, stated so it can be criticised rather than reverse-engineered:

1. every record is assigned its stratum and its diversity key;
2. strata are ordered by `SHA-256(selector_version, dataset_content_fingerprint,
   stratum)`. Deterministic, dataset-bound, and deliberately not alphabetical:
   an alphabetical order would systematically hand the first picks of every
   small lot to the same stratum name;
3. the lot is drawn round-robin over that order — one posting from each stratum
   before any stratum gives a second — so a lot smaller than the number of
   strata still spans as many distinct kinds of case as it has slots;
4. within a stratum, the pick is the candidate whose diversity key has been used
   least so far in the whole lot, ties broken by
   `SHA-256(selector_version, dataset_content_fingerprint, opportunity_id)`;
5. the draw stops at `N`, or when every record has been drawn.

Every tie is broken by a canonicalised hash rather than by file order, SQL order
or Python's set iteration. **Same dataset + same selector version + same N
produces the same ids in the same order**, on any machine, in any process.

Nothing here writes a file, and nothing here computes a metric.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from services.collector.matching.fingerprint import canonical_json

from .fingerprint import calibration_selection_fingerprint
from .frozen import FrozenEvaluationDataset
from .schema import (
    HUMAN_LABEL_PROTOCOL_VERSION,
    HumanLabelError,
    require_supported_protocol_version,
)

__all__ = [
    "CALIBRATION_SELECTION_SCHEMA_VERSION",
    "CALIBRATION_SELECTOR_VERSION",
    "SUPPORTED_SELECTOR_VERSIONS",
    "assert_selection_bindings",
    "CalibrationSelection",
    "CalibrationSelectionItem",
    "select_calibration_sample",
    "stratum_of",
]

#: The selector's algorithm version. It moves whenever the *rule* that draws a
#: lot changes — a stratum added, a tie-break changed, the round-robin replaced.
#: Two lots drawn under different selector versions are not the same instrument
#: however similar they look, so the version is inside the selection digest.
CALIBRATION_SELECTOR_VERSION = "calibration-selector-v0"

#: The shape of the selection artefact on disk.
CALIBRATION_SELECTION_SCHEMA_VERSION = "evaluation-calibration-selection-v0"

#: The selector versions this build can work with. Exactly one, deliberately:
#: a lot drawn by a different algorithm is a different instrument, and reading
#: one back here would mean reporting a sample this code cannot reproduce. There
#: is no compatibility policy in this slice, and inventing one silently — by
#: accepting any version whose file happens to parse — would be the expensive
#: kind of convenience.
SUPPORTED_SELECTOR_VERSIONS: tuple[str, ...] = (CALIBRATION_SELECTOR_VERSION,)

_RECOMMENDED_HIGH = "RECOMMENDED_HIGH"
_RECOMMENDED_MID = "RECOMMENDED_MID"
_RECOMMENDED_LOW = "RECOMMENDED_LOW"
_NOT_RECOMMENDED = "NOT_RECOMMENDED"

_QUALIFICATION_ABSENT = "QUALIFICATION_ABSENT"

_GEOGRAPHY_RESOLVED = "GEOGRAPHY_RESOLVED"
_GEOGRAPHY_UNRESOLVED = "GEOGRAPHY_UNRESOLVED"
_GEOGRAPHY_UNSEGMENTED = "GEOGRAPHY_UNSEGMENTED"

#: Phase 7A's own value for a segment that resolved to a country.
_RESOLVED_SEGMENT_STATUS = "RESOLVED"


@dataclass(frozen=True)
class CalibrationSelectionItem:
    """One drawn opportunity, its position in the lot, and why it was drawn.

    `stratum` records the system outputs that put this posting in the lot. It is
    provenance for whoever reviews the draw afterwards — and it is exactly what
    must not reach the annotator beforehand, which is why it never appears in a
    blind view and why the CLI does not print it unless explicitly asked.
    """

    position: int
    opportunity_id: int
    stratum: Mapping[str, str]
    diversity_key: Mapping[str, str]


@dataclass(frozen=True)
class CalibrationSelection:
    """A drawn lot, bound to the dataset it came from and digested."""

    selection_schema_version: str
    selector_version: str
    protocol_version: str
    dataset_id: str
    dataset_content_fingerprint: str
    profile_id: int
    profile_context_fingerprint: str
    requested_sample_size: int
    effective_sample_size: int
    items: tuple[CalibrationSelectionItem, ...]
    selection_fingerprint: str

    @property
    def opportunity_ids(self) -> tuple[int, ...]:
        return tuple(item.opportunity_id for item in self.items)


def _digest(*parts: Any) -> str:
    return hashlib.sha256(canonical_json(list(parts)).encode("utf-8")).hexdigest()


def _rank_position(recommendation: Mapping[str, Any], opportunity_id: int) -> int:
    """The posting's rank in the current Recommendation run, validated as a number.

    Phase 9A's own contract, from migration `0026`:

        rank_position INTEGER NOT NULL CHECK (rank_position > 0)
        UNIQUE (run_id, rank_position)

    so a *present* recommendation block always carries a positive integer rank,
    and no two postings in one run share one. Anything else — a null, a string,
    a float, a zero, a boolean — is not a rank this build can place in a band,
    and it is refused rather than coerced: a stratum silently built from a
    mis-read rank would put a posting in the wrong third of the lot and nobody
    would ever see it happen.

    `bool` is refused before `int` because `isinstance(True, int)` is true in
    Python and `True` would otherwise pass as rank 1 — the top of the ranking.
    """
    rank = recommendation.get("rank_position")
    if isinstance(rank, bool) or not isinstance(rank, int):
        raise HumanLabelError(
            f"opportunity {opportunity_id} carries a recommendation whose "
            f"rank_position is {rank!r} ({type(rank).__name__}); the "
            "Recommendation contract stores a positive integer and this build "
            "refuses to guess what a different value meant"
        )
    if rank <= 0:
        raise HumanLabelError(
            f"opportunity {opportunity_id} carries rank_position {rank}; the "
            "Recommendation contract stores 1-based ranks"
        )
    return rank


def _recommendation_bands(
    records: Sequence[Mapping[str, Any]],
) -> dict[int, str]:
    """Map every opportunity id to its band among the ranked postings.

    The split is by *position among the ranked records of this dataset*, not by
    the raw `rank_position` value: a run that ranked only part of the cohort
    still splits into three even thirds.

    The order is **numeric**, on `(rank_position, opportunity_id)`. It has to be
    said explicitly because the obvious shortcut is wrong in a way that is
    invisible on a small fixture: ordering ranks by any textual form of the
    number — `str`, `repr` or a canonical JSON encoding — sorts them
    lexically, so a real run of 394 postings comes out `1, 10, 100, 11, 2, ...`
    and the HIGH / MID / LOW thirds stop meaning what they say. Every rank is
    validated as a positive integer first, and duplicates are refused, so the
    pair is already a total order and no tie-break over a textual key is needed.
    """
    ranked: list[tuple[int, int]] = []
    bands: dict[int, str] = {}
    seen: dict[int, int] = {}
    for record in records:
        opportunity_id = int(record["opportunity_id"])
        recommendation = record.get("recommendation")
        if not isinstance(recommendation, Mapping):
            bands[opportunity_id] = _NOT_RECOMMENDED
            continue
        rank = _rank_position(recommendation, opportunity_id)
        if rank in seen:
            # `UNIQUE (run_id, rank_position)` says this cannot happen inside
            # one run, so a repeat means the dataset mixes runs or was edited.
            raise HumanLabelError(
                f"opportunities {seen[rank]} and {opportunity_id} both claim "
                f"rank_position {rank}; a Recommendation run ranks each "
                "position once"
            )
        seen[rank] = opportunity_id
        ranked.append((rank, opportunity_id))

    ranked.sort()
    total = len(ranked)
    if total:
        first_cut = -(-total // 3)  # ceil(total / 3)
        second_cut = -(-(2 * total) // 3)
        for index, (_, opportunity_id) in enumerate(ranked):
            if index < first_cut:
                bands[opportunity_id] = _RECOMMENDED_HIGH
            elif index < second_cut:
                bands[opportunity_id] = _RECOMMENDED_MID
            else:
                bands[opportunity_id] = _RECOMMENDED_LOW
    return bands


def _qualification_bucket(record: Mapping[str, Any]) -> str:
    qualification = record.get("qualification")
    if not isinstance(qualification, Mapping):
        # Never `OUT_OF_SCOPE`: an unread posting has stated nothing about
        # itself, and a calibration lot wants those cases most of all.
        return _QUALIFICATION_ABSENT
    value = qualification.get("qualification")
    # Carried verbatim rather than checked against a closed list: a verdict this
    # build has not heard of is still a distinct stratum, and refusing it here
    # would make the selector the thing that breaks when the taxonomy grows.
    return str(value) if isinstance(value, str) and value else _QUALIFICATION_ABSENT


def _geography_bucket(record: Mapping[str, Any]) -> str:
    segments = record.get("geography_segments")
    if not isinstance(segments, Sequence) or not segments:
        return _GEOGRAPHY_UNSEGMENTED
    for segment in segments:
        if not isinstance(segment, Mapping):
            continue
        if (
            segment.get("status") == _RESOLVED_SEGMENT_STATUS
            and segment.get("country_code") is not None
        ):
            return _GEOGRAPHY_RESOLVED
    return _GEOGRAPHY_UNRESOLVED


def _diversity_key(record: Mapping[str, Any]) -> dict[str, str]:
    """Where the posting was collected from, and what kind of posting it is.

    The first source is used because Phase 10.1 already froze the sources in a
    canonical order; picking "the first" is therefore a stable choice and not a
    coin toss over a set.
    """
    sources = record.get("sources")
    source_type = "SOURCE_UNSTATED"
    if isinstance(sources, Sequence):
        for source in sources:
            if isinstance(source, Mapping) and source.get("source_type"):
                source_type = str(source["source_type"])
                break
    opportunity_type = record.get("opportunity_type")
    return {
        "source_type": source_type,
        "opportunity_type": (
            str(opportunity_type) if opportunity_type else "TYPE_UNSTATED"
        ),
    }


def stratum_of(
    record: Mapping[str, Any], recommendation_band: str
) -> dict[str, str]:
    """The three-part stratum of one record, given its recommendation band."""
    return {
        "recommendation_band": recommendation_band,
        "qualification": _qualification_bucket(record),
        "geography": _geography_bucket(record),
    }


def select_calibration_sample(
    dataset: FrozenEvaluationDataset, sample_size: int
) -> CalibrationSelection:
    """Draw a calibration lot of at most `sample_size` postings, deterministically.

    Returns fewer than requested only when the dataset holds fewer records, and
    says so through `requested_sample_size` / `effective_sample_size` rather than
    quietly. The digest covers the effective size, so a lot is identified by what
    it is and not by what was asked for.
    """
    if isinstance(sample_size, bool) or not isinstance(sample_size, int):
        raise HumanLabelError(
            f"sample size must be an integer, not {sample_size!r}"
        )
    if sample_size <= 0:
        raise HumanLabelError(
            f"sample size must be a positive integer, not {sample_size}"
        )

    records = dataset.records
    bands = _recommendation_bands(records)

    strata: dict[str, list[int]] = {}
    stratum_of_id: dict[int, dict[str, str]] = {}
    diversity_of_id: dict[int, dict[str, str]] = {}
    for record in records:
        opportunity_id = int(record["opportunity_id"])
        stratum = stratum_of(record, bands[opportunity_id])
        key = canonical_json(stratum)
        strata.setdefault(key, []).append(opportunity_id)
        stratum_of_id[opportunity_id] = stratum
        diversity_of_id[opportunity_id] = _diversity_key(record)

    tie_break = {
        opportunity_id: _digest(
            CALIBRATION_SELECTOR_VERSION,
            dataset.content_fingerprint,
            opportunity_id,
        )
        for opportunity_id in stratum_of_id
    }
    for members in strata.values():
        members.sort(key=lambda value: (tie_break[value], value))

    stratum_order = sorted(
        strata,
        key=lambda key: (
            _digest(CALIBRATION_SELECTOR_VERSION, dataset.content_fingerprint, key),
            key,
        ),
    )

    wanted = min(sample_size, len(records))
    used_diversity: dict[str, int] = {}
    remaining = {key: list(strata[key]) for key in stratum_order}
    selected: list[int] = []

    while len(selected) < wanted:
        progressed = False
        for key in stratum_order:
            if len(selected) >= wanted:
                break
            candidates = remaining[key]
            if not candidates:
                continue
            progressed = True
            # Least-used (source_type, opportunity_type) pair first; the
            # candidate list is already in tie-break order, so `min` over a
            # stable key is a total, reproducible choice.
            chosen = min(
                candidates,
                key=lambda value: (
                    used_diversity.get(
                        canonical_json(diversity_of_id[value]), 0
                    ),
                    tie_break[value],
                    value,
                ),
            )
            candidates.remove(chosen)
            selected.append(chosen)
            diversity_key = canonical_json(diversity_of_id[chosen])
            used_diversity[diversity_key] = used_diversity.get(diversity_key, 0) + 1
        if not progressed:  # pragma: no cover - `wanted` bounds the loop
            break

    items = tuple(
        CalibrationSelectionItem(
            position=position,
            opportunity_id=opportunity_id,
            stratum=dict(stratum_of_id[opportunity_id]),
            diversity_key=dict(diversity_of_id[opportunity_id]),
        )
        for position, opportunity_id in enumerate(selected, start=1)
    )
    fingerprint = calibration_selection_fingerprint(
        selector_version=CALIBRATION_SELECTOR_VERSION,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        sample_size=len(items),
        selected_opportunity_ids=[item.opportunity_id for item in items],
    )
    return CalibrationSelection(
        selection_schema_version=CALIBRATION_SELECTION_SCHEMA_VERSION,
        selector_version=CALIBRATION_SELECTOR_VERSION,
        protocol_version=HUMAN_LABEL_PROTOCOL_VERSION,
        dataset_id=dataset.dataset_id,
        dataset_content_fingerprint=dataset.content_fingerprint,
        profile_id=dataset.profile_id,
        profile_context_fingerprint=dataset.profile_context_fingerprint,
        requested_sample_size=sample_size,
        effective_sample_size=len(items),
        items=items,
        selection_fingerprint=fingerprint,
    )


def assert_selection_bindings(
    selection: CalibrationSelection, dataset: FrozenEvaluationDataset
) -> None:
    """Refuse a lot that was not drawn from this dataset, for this person, now.

    The **one** place this is decided, called on every path that uses a stored
    selection — resolving one on the command line, walking to the next unjudged
    posting, reporting progress, digesting a labelset. Before anything is shown
    and before anything is written, not afterwards: a `--selection` pointing at
    another dataset's lot must be refused while it is still an argument, not
    discovered three commands later when a digest comes out wrong.

    What is checked, and each for its own reason:

    * the **dataset id and content fingerprint** — a lot names postings by id,
      and ids only mean something inside the snapshot they were drawn from;
    * the **profile id and profile context fingerprint** — the strata were
      computed from personalised signals, so a lot drawn for one profile state
      is not a lot for another even over identical postings;
    * the **protocol and selector versions** — a lot drawn by an algorithm this
      build does not implement cannot be reproduced or reasoned about here;
    * the **internal consistency** of the lot itself — declared size, unique
      ids, positions `1..N` in order;
    * that every selected id is **actually in the dataset** — the one check that
      would otherwise fail much later, as a blind view of a posting that does
      not exist.
    """
    if selection.dataset_id != dataset.dataset_id:
        raise HumanLabelError(
            f"the calibration selection was drawn from dataset "
            f"{selection.dataset_id}, not {dataset.dataset_id}"
        )
    if selection.dataset_content_fingerprint != dataset.content_fingerprint:
        raise HumanLabelError(
            "the calibration selection was drawn from content fingerprint "
            f"{selection.dataset_content_fingerprint}, not "
            f"{dataset.content_fingerprint}"
        )
    if selection.profile_id != dataset.profile_id:
        raise HumanLabelError(
            f"the calibration selection was drawn for profile "
            f"{selection.profile_id}, not {dataset.profile_id}"
        )
    if selection.profile_context_fingerprint != dataset.profile_context_fingerprint:
        raise HumanLabelError(
            "the calibration selection was drawn against profile context "
            f"fingerprint {selection.profile_context_fingerprint}, not "
            f"{dataset.profile_context_fingerprint}"
        )
    require_supported_protocol_version(
        selection.protocol_version, subject="the calibration selection"
    )
    if selection.selector_version not in SUPPORTED_SELECTOR_VERSIONS:
        raise HumanLabelError(
            f"the calibration selection was drawn by selector "
            f"{selection.selector_version!r}; this build implements "
            f"{list(SUPPORTED_SELECTOR_VERSIONS)} and has no compatibility "
            "policy for another"
        )
    if selection.selection_schema_version != CALIBRATION_SELECTION_SCHEMA_VERSION:
        raise HumanLabelError(
            f"the calibration selection states schema "
            f"{selection.selection_schema_version!r}; this build reads "
            f"{CALIBRATION_SELECTION_SCHEMA_VERSION!r}"
        )

    ids = selection.opportunity_ids
    if selection.effective_sample_size != len(ids):
        raise HumanLabelError(
            f"the calibration selection declares {selection.effective_sample_size} "
            f"items and holds {len(ids)}"
        )
    if len(set(ids)) != len(ids):
        duplicates = sorted({value for value in ids if ids.count(value) > 1})
        raise HumanLabelError(
            f"the calibration selection repeats opportunity ids: {duplicates}"
        )
    positions = [item.position for item in selection.items]
    if positions != list(range(1, len(ids) + 1)):
        raise HumanLabelError(
            "the calibration selection states positions "
            f"{positions}, not 1..{len(ids)} in order"
        )
    missing = [value for value in ids if not dataset.contains(value)]
    if missing:
        raise HumanLabelError(
            f"the calibration selection names opportunities {missing}, which "
            f"are not in dataset {dataset.dataset_id}"
        )

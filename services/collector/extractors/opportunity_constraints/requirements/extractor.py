"""The pure reading: one description in, one set of requirements out.

Nothing in this module opens a database, issues a query, reads a clock or
touches the network. It is handed a `RequirementSource` — the description, and
nothing else — and returns an `ExtractedRequirements`. That is what makes the
reading testable in memory against real postings without writing anything
anywhere, and what makes the fingerprint below meaningful.

**The fingerprint covers exactly what is read, which in v1 is the description.**
Not the title: a title names a role, and a name is not a demand — "Python
Developer" states no requirement, and reading it as one would make every
posting require whatever its title mentions. Not the location, the country or
the remote type: none of them says anything about a skill or a language, and a
posting in Paris has not required French. Putting an unread field in the
fingerprint would make an edit nobody's rules looked at recompute every posting;
leaving a read field out would let a changed source look unchanged. So the
fingerprint is over `RequirementSource`'s fields, a test pins the two together,
and 3.5B's digest is deliberately **not** 3.5A's: the two phases read different
inputs, so they answer "did the source change?" differently and correctly.

Ambiguities from the skill rules and the language rules are concatenated,
**deduplicated**, and renumbered here, so one posting has one ambiguity sequence
and `UNIQUE (opportunity_id, position)` in `0013` means what it says.

The deduplication is worth stating plainly, because it is the kind of thing that
looks like hiding evidence and is not. An ambiguity row holds the refusal's
kind, reason, rule, fragment and heading — and **not** the terms of the group it
refused, deliberately, since storing them would be storing half a requirement.
So when one sentence produces two refusals that agree on every one of those five
fields, the second row carries nothing the first does not: it says "and this
happened again, over the same words, for the same reason". The real corpus
produced exactly that. Two refusals differing in any field — a different
fragment, a different reason, a different heading — are two different facts and
both survive.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

from services.collector.extractors.opportunity_constraints.requirements.languages import (
    read_language_requirements,
)
from services.collector.extractors.opportunity_constraints.requirements.models import (
    REQUIREMENT_EXTRACTOR_VERSION,
    ExtractedRequirements,
    RequirementAmbiguity,
    RequirementSource,
)
from services.collector.extractors.opportunity_constraints.requirements.sections import (
    parse_requirement_segments,
)
from services.collector.extractors.opportunity_constraints.requirements.skills import (
    read_skill_requirements,
)

__all__ = [
    "REQUIREMENT_FINGERPRINT_FIELDS",
    "extract_opportunity_requirements",
    "requirement_source_fingerprint",
]

#: Exactly the fields 3.5B reads. A test compares this tuple to
#: `RequirementSource`'s own fields, so an input added to the extractor without
#: being added here fails loudly instead of silently breaking idempotence.
REQUIREMENT_FINGERPRINT_FIELDS: tuple[str, ...] = ("description",)


def requirement_source_fingerprint(source: RequirementSource) -> str:
    """The SHA-256 of the canonical JSON of everything 3.5B reads.

    Canonical means sorted keys and no insignificant whitespace, so the digest
    depends on the values and not on how a dict happened to be built. It
    excludes `opportunity_id`, like 3.5A's: the identity of the row is not part
    of what was read.
    """
    payload = {name: getattr(source, name) for name in REQUIREMENT_FINGERPRINT_FIELDS}
    serialized = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _deduplicated(
    ambiguities: tuple[RequirementAmbiguity, ...]
) -> tuple[RequirementAmbiguity, ...]:
    """Drop refusals identical to one already recorded, keeping the first.

    The key is everything a row actually stores. Order is preserved, so the
    surviving row is the one the posting produced first and the sequence stays
    the reading order — nothing is sorted and nothing is merged.
    """
    seen: set[tuple[str, str, str, str, str | None]] = set()
    kept: list[RequirementAmbiguity] = []
    for ambiguity in ambiguities:
        key = (
            ambiguity.kind.value,
            ambiguity.reason.value,
            ambiguity.rule_id,
            ambiguity.text,
            ambiguity.context_heading_text,
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(ambiguity)
    return tuple(kept)


def extract_opportunity_requirements(
    source: RequirementSource,
) -> ExtractedRequirements:
    """Read one description. Deterministic, offline, and silent where unsure.

    A posting that names no technology and no language comes back with three
    empty tuples, which is a real answer rather than a failure — see
    `repository.py` for why that answer still writes a state row.
    """
    if not isinstance(source, RequirementSource):
        raise TypeError("source must be a RequirementSource")

    segments = parse_requirement_segments(source.description)
    skills, skill_ambiguities = read_skill_requirements(segments)
    languages, language_ambiguities = read_language_requirements(segments)
    ambiguities = tuple(
        replace(ambiguity, position=position)
        for position, ambiguity in enumerate(
            _deduplicated(skill_ambiguities + language_ambiguities)
        )
    )
    return ExtractedRequirements(
        opportunity_id=source.opportunity_id,
        source_fingerprint=requirement_source_fingerprint(source),
        extractor_version=REQUIREMENT_EXTRACTOR_VERSION,
        skills=skills,
        languages=languages,
        ambiguities=ambiguities,
    )

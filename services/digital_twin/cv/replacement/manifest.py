"""The candidate manifest: written once, whole, and provable after a restart.

A replacement review outlives the process that opened it. The PDF is not kept,
the extraction is not kept in memory, and a counter in a column proves nothing:
a row silently edited afterwards leaves the count exactly where it was. So the
manifest carries a **hash chain**. Each row's `chain_digest` is SHA-256 over the
previous row's digest and this row's canonical payload, and the chain is seeded
from the campaign's own identity — document digest, parser version, extractor
version — so a manifest cannot be mistaken for another document's.

That gives three answers a count cannot:

* a **missing** row breaks the chain from its own ordinal onwards, and the gap
  in `ordinal` says exactly where;
* a **duplicated** row is refused by `UNIQUE (extraction_id, ordinal)` and
  `UNIQUE (extraction_id, candidate_fingerprint)` before it is ever written;
* an **altered** row changes its own payload, so the chain diverges at its
  position and every later digest with it.

The write is a single transaction: the extraction row and every candidate row
land together or not at all. A partially populated manifest is therefore not a
state this code can produce, and `verify_manifest` is the check against anything
that did not come from this code.

No value, no normalized value and no raw text leaves this module. The payload
that feeds the chain includes them — it must, or an edit would be invisible —
but only its digest is ever returned, stored in `chain_digest`, or printed.
"""

from __future__ import annotations

import hashlib
import sqlite3

from services.digital_twin.cv.candidates.models import (
    ExtractedCandidate,
    StructuredCvExtraction,
)
from services.digital_twin.cv.fact_bridge import (
    fact_type_for_candidate_type,
    provenance_for_candidate,
)
from services.digital_twin.cv.replacement.models import (
    CvDocumentNotFoundError,
    CvExtraction,
    ExtractionMismatchError,
    ManifestEntry,
    ManifestState,
    ManifestVerification,
)
from services.digital_twin.facts.models import encode_page_numbers

__all__ = [
    "MANIFEST_CHAIN_VERSION",
    "build_manifest_entries",
    "chain_digest_of",
    "read_manifest",
    "validate_extraction_for_document",
    "verify_manifest",
    "write_extraction_with_manifest",
]

#: Named so that a future change to the payload is a visible change of contract
#: rather than a silent mismatch against manifests already written.
MANIFEST_CHAIN_VERSION = "cv-manifest-chain-v1"

#: Field and record separators, the pair `ProvenanceInput` already uses.
_FIELD = "\x1f"
_RECORD = "\x1e"


def _seed(extraction: StructuredCvExtraction) -> str:
    """The chain's first link: which document, read by which versions."""
    payload = _FIELD.join(
        (
            MANIFEST_CHAIN_VERSION,
            extraction.cv_sha256,
            extraction.parser_version,
            extraction.extractor_version,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _payload(entry: ManifestEntry) -> str:
    """Everything that makes this reading what it is, in a fixed order."""
    return _FIELD.join(
        (
            str(entry.ordinal),
            entry.candidate_type,
            entry.fact_type,
            entry.candidate_fingerprint,
            entry.provenance_key,
            entry.value,
            entry.normalized_value or "",
            entry.rule_id,
            entry.page_numbers or "",
            entry.section_type or "",
            "" if entry.section_index is None else str(entry.section_index),
        )
    )


def _link(previous: str, entry: ManifestEntry) -> str:
    return hashlib.sha256(
        (previous + _RECORD + _payload(entry)).encode("utf-8")
    ).hexdigest()


def _entry_without_digest(ordinal: int, candidate: ExtractedCandidate) -> ManifestEntry:
    """Translate one candidate, deriving nothing this project does not already.

    `fact_type` comes from the one bridge mapping, and `provenance_key` from the
    candidate's own evidence through `provenance_for_candidate` — the same key
    the fact would carry if it were imported. It does **not** include the
    replacement attempt: a second attempt over the same reading is the same
    proof, and pretending otherwise would let one document propose one reading
    twice.
    """
    return ManifestEntry(
        ordinal=ordinal,
        candidate_type=candidate.candidate_type.value,
        fact_type=fact_type_for_candidate_type(candidate.candidate_type).value,
        candidate_fingerprint=candidate.fingerprint,
        provenance_key=provenance_for_candidate(candidate).resolved_provenance_key(),
        value=candidate.raw_text,
        normalized_value=candidate.normalized_value,
        rule_id=candidate.rule_id.value,
        page_numbers=encode_page_numbers(candidate.page_numbers),
        section_type=(
            None if candidate.section_type is None else candidate.section_type.value
        ),
        section_index=candidate.section_index,
        chain_digest="",
    )


def build_manifest_entries(
    extraction: StructuredCvExtraction,
) -> tuple[ManifestEntry, ...]:
    """Every candidate, in the extraction's own order, with its chain digest."""
    previous = _seed(extraction)
    entries: list[ManifestEntry] = []
    for ordinal, candidate in enumerate(extraction.candidates):
        draft = _entry_without_digest(ordinal, candidate)
        previous = _link(previous, draft)
        entries.append(
            ManifestEntry(
                ordinal=draft.ordinal,
                candidate_type=draft.candidate_type,
                fact_type=draft.fact_type,
                candidate_fingerprint=draft.candidate_fingerprint,
                provenance_key=draft.provenance_key,
                value=draft.value,
                normalized_value=draft.normalized_value,
                rule_id=draft.rule_id,
                page_numbers=draft.page_numbers,
                section_type=draft.section_type,
                section_index=draft.section_index,
                chain_digest=previous,
            )
        )
    return tuple(entries)


def chain_digest_of(extraction: StructuredCvExtraction) -> str:
    """The digest a complete manifest of this extraction must reproduce.

    An extraction with no candidate has the seed itself as its digest, which is
    still a statement about *which* document was read empty.
    """
    entries = build_manifest_entries(extraction)
    return entries[-1].chain_digest if entries else _seed(extraction)


def validate_extraction_for_document(
    extraction: StructuredCvExtraction, *, content_sha256: str
) -> None:
    """Refuse an extraction that does not describe this document. Writes nothing.

    Three disagreements are possible and all three are caller mistakes, not
    storage problems, so they are raised here — before a campaign is written
    and before an existing one is judged:

    * the extraction names another document than the one it is filed under;
    * a candidate carries another document digest than the extraction does;
    * a candidate was read by other parser or extractor versions than the
      campaign claims, which would file one reading under another campaign's
      identity.

    The message names the position and the versions. It never quotes a reading.
    """
    if extraction.cv_sha256 != content_sha256:
        raise ExtractionMismatchError(
            "the extraction names another document than the one it is filed under"
        )
    for ordinal, candidate in enumerate(extraction.candidates):
        if candidate.cv_sha256 != content_sha256:
            raise ExtractionMismatchError(
                f"candidate {ordinal} carries another document digest than the "
                f"extraction"
            )
        if (candidate.parser_version, candidate.extractor_version) != (
            extraction.parser_version,
            extraction.extractor_version,
        ):
            raise ExtractionMismatchError(
                f"candidate {ordinal} was read by "
                f"{candidate.parser_version}/{candidate.extractor_version}, not by "
                f"{extraction.parser_version}/{extraction.extractor_version}"
            )


_EXTRACTION_COLUMNS = (
    "id, document_id, profile_id, parser_version, extractor_version, attempt_no, "
    "candidate_count, manifest_chain_digest, manifest_state"
)


def _extraction_from_row(row: tuple) -> CvExtraction:
    return CvExtraction(
        id=row[0],
        document_id=row[1],
        profile_id=row[2],
        parser_version=row[3],
        extractor_version=row[4],
        attempt_no=row[5],
        candidate_count=row[6],
        manifest_chain_digest=row[7],
        manifest_state=ManifestState(row[8]),
    )


def write_extraction_with_manifest(
    connection: sqlite3.Connection,
    *,
    profile_id: int,
    document_id: int,
    extraction: StructuredCvExtraction,
    attempt_no: int,
) -> CvExtraction:
    """Record one campaign and its whole manifest, in one transaction.

    Writes to `profile_cv_extractions` and `profile_cv_candidates` and to
    nothing else. No `profile_facts` row and no `profile_fact_provenance` row is
    created, read for a decision, or touched: preparing a replacement proposes
    nothing to the Digital Twin, which is what makes cancelling one free.

    It opens its own `BEGIN IMMEDIATE` and refuses a borrowed transaction: the
    guarantee that a manifest is whole is this function's to make, not its
    caller's to remember.

    It is also the writing entry point, so it is where the identity checks
    live, inside that same transaction and before the first insert: the
    document has to belong to this profile, the extraction has to name that
    document, and every candidate has to carry the same digest and the same
    parser and extractor versions. `ensure_extraction_with_manifest` runs the
    same `validate_extraction_for_document` earlier — to decide what to do
    about an existing campaign without writing — but calling this function
    directly cannot skip the checks, because they are here.
    """
    if connection.in_transaction:
        raise sqlite3.ProgrammingError(
            "write_extraction_with_manifest owns its transaction and borrows none"
        )
    entries = build_manifest_entries(extraction)
    digest = entries[-1].chain_digest if entries else _seed(extraction)
    connection.execute("BEGIN IMMEDIATE")
    try:
        document = connection.execute(
            "SELECT content_sha256 FROM profile_cv_documents "
            "WHERE id = ? AND profile_id = ?",
            (document_id, profile_id),
        ).fetchone()
        if document is None:
            raise CvDocumentNotFoundError(
                f"document {document_id} does not belong to profile {profile_id}"
            )
        validate_extraction_for_document(extraction, content_sha256=document[0])
        row = connection.execute(
            f"""INSERT INTO profile_cv_extractions (
                    document_id, profile_id, parser_version, extractor_version,
                    attempt_no, candidate_count, manifest_chain_digest, manifest_state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'COMPLETE')
                RETURNING {_EXTRACTION_COLUMNS}""",
            (
                document_id,
                profile_id,
                extraction.parser_version,
                extraction.extractor_version,
                attempt_no,
                len(entries),
                digest,
            ),
        ).fetchone()
        if row is None:
            raise sqlite3.ProgrammingError("extraction insert returned no row")
        stored = _extraction_from_row(row)
        connection.executemany(
            """INSERT INTO profile_cv_candidates (
                   extraction_id, profile_id, ordinal, candidate_type, fact_type,
                   candidate_fingerprint, provenance_key, value, normalized_value,
                   rule_id, page_numbers, section_type, section_index, chain_digest
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                (
                    stored.id,
                    profile_id,
                    entry.ordinal,
                    entry.candidate_type,
                    entry.fact_type,
                    entry.candidate_fingerprint,
                    entry.provenance_key,
                    entry.value,
                    entry.normalized_value,
                    entry.rule_id,
                    entry.page_numbers,
                    entry.section_type,
                    entry.section_index,
                    entry.chain_digest,
                )
                for entry in entries
            ],
        )
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    return stored


_CANDIDATE_COLUMNS = (
    "id, ordinal, candidate_type, fact_type, candidate_fingerprint, provenance_key, "
    "value, normalized_value, rule_id, page_numbers, section_type, section_index, "
    "chain_digest"
)


def read_manifest(
    connection: sqlite3.Connection, *, profile_id: int, extraction_id: int
) -> tuple[ManifestEntry, ...]:
    """The persisted manifest of this profile's campaign, in document order."""
    rows = connection.execute(
        f"""SELECT {_CANDIDATE_COLUMNS} FROM profile_cv_candidates
             WHERE extraction_id = ? AND profile_id = ?
             ORDER BY ordinal""",
        (extraction_id, profile_id),
    ).fetchall()
    return tuple(
        ManifestEntry(
            id=row[0],
            ordinal=row[1],
            candidate_type=row[2],
            fact_type=row[3],
            candidate_fingerprint=row[4],
            provenance_key=row[5],
            value=row[6],
            normalized_value=row[7],
            rule_id=row[8],
            page_numbers=row[9],
            section_type=row[10],
            section_index=row[11],
            chain_digest=row[12],
        )
        for row in rows
    )


def verify_manifest(
    connection: sqlite3.Connection, *, profile_id: int, extraction_id: int
) -> ManifestVerification:
    """Re-derive the chain from what is stored and say whether it holds.

    Reads rows and writes none, so it is safe on a `mode=ro` connection and safe
    to run before deciding anything. The reason it returns names the failure in
    canonical terms — a count, an ordinal, a digest — and never quotes a reading.
    """
    extraction_row = connection.execute(
        f"SELECT {_EXTRACTION_COLUMNS} FROM profile_cv_extractions "
        "WHERE id = ? AND profile_id = ?",
        (extraction_id, profile_id),
    ).fetchone()
    if extraction_row is None:
        raise LookupError(
            f"extraction {extraction_id} does not belong to profile {profile_id}"
        )
    extraction = _extraction_from_row(extraction_row)
    document_row = connection.execute(
        "SELECT content_sha256 FROM profile_cv_documents WHERE id = ? AND profile_id = ?",
        (extraction.document_id, profile_id),
    ).fetchone()
    if document_row is None:
        raise LookupError(
            f"document {extraction.document_id} does not belong to profile {profile_id}"
        )
    seed = hashlib.sha256(
        _FIELD.join(
            (
                MANIFEST_CHAIN_VERSION,
                document_row[0],
                extraction.parser_version,
                extraction.extractor_version,
            )
        ).encode("utf-8")
    ).hexdigest()

    stored = read_manifest(
        connection, profile_id=profile_id, extraction_id=extraction_id
    )

    def _fail(reason: str, ordinal: int | None) -> ManifestVerification:
        return ManifestVerification(
            extraction_id=extraction_id,
            expected_count=extraction.candidate_count,
            stored_count=len(stored),
            expected_chain_digest=extraction.manifest_chain_digest,
            stored_chain_digest=stored[-1].chain_digest if stored else None,
            first_divergent_ordinal=ordinal,
            reason=reason,
        )

    previous = seed
    for position, entry in enumerate(stored):
        if entry.ordinal != position:
            # A gap says a row is gone; the chain below would diverge anyway,
            # but naming the position is what makes the loss diagnosable.
            return _fail("ORDINAL_GAP", position)
        previous = _link(previous, entry)
        if previous != entry.chain_digest:
            return _fail("CHAIN_DIVERGES", entry.ordinal)
    if len(stored) != extraction.candidate_count:
        return _fail("COUNT_MISMATCH", len(stored))
    final = stored[-1].chain_digest if stored else seed
    if final != extraction.manifest_chain_digest:
        return _fail("FINAL_DIGEST_MISMATCH", None)
    return ManifestVerification(
        extraction_id=extraction_id,
        expected_count=extraction.candidate_count,
        stored_count=len(stored),
        expected_chain_digest=extraction.manifest_chain_digest,
        stored_chain_digest=final,
        first_divergent_ordinal=None,
        reason=None,
    )

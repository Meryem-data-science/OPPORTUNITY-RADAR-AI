"""Phase 8C: `GET /api/opportunities` exposes the *persisted* fine classification.

Every fixture here writes `opportunity_qualifications` by hand, with fine values
that the real classifiers would never derive from the titles and descriptions
used. That is the point: if the API recomputed anything, these assertions would
return the classifier's answer instead of the stored one and fail.

The three persisted states migration 0025 keeps apart are covered separately,
because collapsing any two of them is the failure this phase exists to prevent:

    * a classified row with a category (including `OTHER`, a value);
    * a classified row with no category (`OUT_OF_SCOPE`/`UNCERTAIN`), whose
      collections are `[]` and whose reasons say why;
    * a row that was never fine-classified, whose public fields are all `null`.
"""

import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import pytest

from services.api.fine_classification import (
    UNCLASSIFIED,
    FineClassificationDecodeError,
    decode_fine_classification,
)
from services.api.main import app
from services.collector.database.migrations import apply_migrations
from services.collector.qualification.fine_classifier import FINE_ELIGIBLE_QUALIFICATIONS
from services.collector.qualification.taxonomy import Qualification


FINE_VERSION = "fine-data-ai-rules-v2"

#: Deliberately unrelated to every fine signal in the taxonomy. A classifier run
#: over this text could not produce the categories the fixtures persist.
NEUTRAL_DESCRIPTION = "TEST ONLY body with no signal of any kind."


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "fine-api.db"
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        apply_migrations(connection)
        connection.commit()
    finally:
        connection.close()
    return path


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _insert_opportunity(
    connection: sqlite3.Connection,
    suffix: str,
    *,
    last_seen_at: str,
    status: str = "visible",
    is_active: int = 1,
) -> int:
    url = f"https://example.invalid/jobs/{suffix}"
    cursor = connection.execute(
        """INSERT INTO opportunities (
            canonical_title, organization, location, description, discovered_at,
            first_seen_at, last_seen_at, source_url, application_url, canonical_url,
            status, is_active
        ) VALUES (?, 'TEST ONLY organization', 'TEST ONLY location', ?,
                  '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', ?,
                  ?, NULL, ?, ?, ?)""",
        (
            f"TEST ONLY role {suffix}",
            NEUTRAL_DESCRIPTION,
            last_seen_at,
            url,
            url,
            status,
            is_active,
        ),
    )
    return int(cursor.lastrowid)


def _insert_qualification(
    connection: sqlite3.Connection,
    opportunity_id: int,
    *,
    qualification: str,
    fine_primary_category: str | None = None,
    fine_secondary_categories_json: str | None = None,
    fine_category_evidence_json: str | None = None,
    fine_reasons_json: str | None = None,
    fine_classifier_version: str | None = None,
) -> None:
    """Write one current qualification row with exactly the fine values given.

    Defaults reproduce a row migration 0025 reached and fine classification never
    did — the legacy state — so a test only names the columns it is about.
    """
    connection.execute(
        """INSERT INTO opportunity_qualifications (
            opportunity_id, qualification, primary_domain, opportunity_type,
            employment_type, listing_quality, matched_domains_json,
            matched_title_signals_json, matched_description_signals_json,
            matched_exclusion_signals_json, reasons_json, classifier_version,
            input_fingerprint, classified_at, created_at, updated_at,
            fine_primary_category, fine_secondary_categories_json,
            fine_category_evidence_json, fine_reasons_json, fine_classifier_version
        ) VALUES (?, ?, 'UNKNOWN', 'UNKNOWN', 'UNKNOWN', 'NORMAL_LISTING',
                  '[]', '[]', '[]', '[]', '[]', 'qualification-rules-v2', ?,
                  '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00',
                  '2026-01-01T00:00:00+00:00', ?, ?, ?, ?, ?)""",
        (
            opportunity_id,
            qualification,
            "a" * 64,
            fine_primary_category,
            fine_secondary_categories_json,
            fine_category_evidence_json,
            fine_reasons_json,
            fine_classifier_version,
        ),
    )


def _get(monkeypatch, path: Path, query: str = "?limit=100"):
    monkeypatch.setenv("DATABASE_BACKEND", "sqlite")
    monkeypatch.setenv("SQLITE_DATABASE_PATH", str(path))
    return TestClient(app).get(f"/api/opportunities{query}")


def _by_title(payload: dict) -> dict[str, dict]:
    return {item["canonical_title"]: item for item in payload["items"]}


def _snapshot(path: Path) -> list[tuple]:
    """Every column of every qualification row, for a read-only proof."""
    connection = _connect(path)
    try:
        return connection.execute(
            "SELECT * FROM opportunity_qualifications ORDER BY opportunity_id"
        ).fetchall()
    finally:
        connection.close()


# --- B: a qualified CORE_TARGET row with a persisted category ----------------

CORE_SECONDARY = ["MLOPS", "DATA_ENGINEERING", "NLP"]
CORE_EVIDENCE = [
    {
        "category": "COMPUTER_VISION",
        "field": "TITLE",
        "kind": "ROLE_PHRASE",
        "signal": "computer vision engineer",
    },
    {
        "category": "MLOPS",
        "field": "TITLE",
        "kind": "CONTEXT_PHRASE",
        "signal": "model serving",
    },
    {
        "category": "DATA_ENGINEERING",
        "field": "DESCRIPTION",
        "kind": "CONCRETE_CONCEPT",
        "signal": "airflow",
    },
    {
        "category": "NLP",
        "field": "DESCRIPTION",
        "kind": "CONCRETE_CONCEPT",
        "signal": "named entity recognition",
    },
]
CORE_REASONS = [
    "fine categories evidenced by the title take precedence",
    "TEST ONLY second reason",
]


def _seed_core(connection: sqlite3.Connection) -> int:
    opportunity_id = _insert_opportunity(
        connection, "core", last_seen_at="2026-01-05T00:00:00+00:00"
    )
    _insert_qualification(
        connection,
        opportunity_id,
        qualification="CORE_TARGET",
        fine_primary_category="COMPUTER_VISION",
        fine_secondary_categories_json=json.dumps(CORE_SECONDARY),
        fine_category_evidence_json=json.dumps(CORE_EVIDENCE),
        fine_reasons_json=json.dumps(CORE_REASONS),
        fine_classifier_version=FINE_VERSION,
    )
    return opportunity_id


def test_persisted_core_fine_classification_is_returned_verbatim_and_in_order(
    tmp_path, monkeypatch
) -> None:
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        _seed_core(connection)
        connection.commit()
    finally:
        connection.close()

    response = _get(monkeypatch, path)

    assert response.status_code == 200
    item = _by_title(response.json())["TEST ONLY role core"]
    assert item["fine_primary_category"] == "COMPUTER_VISION"
    # Exact order, not a set: precedence order is the persisted meaning.
    assert item["fine_secondary_categories"] == CORE_SECONDARY
    assert item["fine_category_evidence"] == CORE_EVIDENCE
    assert item["fine_reasons"] == CORE_REASONS
    assert item["fine_classifier_version"] == FINE_VERSION


def test_returned_fine_values_are_the_stored_row_not_a_recomputation(
    tmp_path, monkeypatch
) -> None:
    """The stored values contradict what the classifiers would derive here.

    The title is a plain Data Engineer and the description carries no signal, so
    a fine classifier run over this row could not answer COMPUTER_VISION. Only a
    read of the persisted row can.
    """
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        opportunity_id = _insert_opportunity(
            connection, "stored", last_seen_at="2026-01-05T00:00:00+00:00"
        )
        connection.execute(
            "UPDATE opportunities SET canonical_title = ?, description = ? WHERE id = ?",
            ("Data Engineer", "We build data pipelines with dbt, airflow and spark.", opportunity_id),
        )
        _insert_qualification(
            connection,
            opportunity_id,
            qualification="CORE_TARGET",
            fine_primary_category="COMPUTER_VISION",
            fine_secondary_categories_json=json.dumps(["GENERATIVE_AI"]),
            fine_category_evidence_json=json.dumps(
                [
                    {
                        "category": "COMPUTER_VISION",
                        "field": "TITLE",
                        "kind": "ROLE_PHRASE",
                        "signal": "TEST ONLY fixture signal",
                    }
                ]
            ),
            fine_reasons_json=json.dumps(["TEST ONLY fixture reason"]),
            fine_classifier_version="TEST-ONLY-fixture-version",
        )
        connection.commit()
    finally:
        connection.close()

    item = _by_title(_get(monkeypatch, path).json())["Data Engineer"]

    assert item["fine_primary_category"] == "COMPUTER_VISION"
    assert item["fine_secondary_categories"] == ["GENERATIVE_AI"]
    assert item["fine_category_evidence"] == [
        {
            "category": "COMPUTER_VISION",
            "field": "TITLE",
            "kind": "ROLE_PHRASE",
            "signal": "TEST ONLY fixture signal",
        }
    ]
    assert item["fine_reasons"] == ["TEST ONLY fixture reason"]
    # A version string no classifier in this repository defines.
    assert item["fine_classifier_version"] == "TEST-ONLY-fixture-version"


# --- C: ADJACENT_TARGET classified OTHER -------------------------------------


def test_adjacent_target_other_is_a_value_with_empty_collections(
    tmp_path, monkeypatch
) -> None:
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        opportunity_id = _insert_opportunity(
            connection, "adjacent", last_seen_at="2026-01-04T00:00:00+00:00"
        )
        _insert_qualification(
            connection,
            opportunity_id,
            qualification="ADJACENT_TARGET",
            fine_primary_category="OTHER",
            fine_secondary_categories_json="[]",
            fine_category_evidence_json="[]",
            fine_reasons_json=json.dumps(
                [
                    "qualified as Data/AI but no supported fine category is "
                    "evidenced; classified OTHER"
                ]
            ),
            fine_classifier_version=FINE_VERSION,
        )
        connection.commit()
    finally:
        connection.close()

    item = _by_title(_get(monkeypatch, path).json())["TEST ONLY role adjacent"]

    assert item["fine_primary_category"] == "OTHER"
    assert item["fine_primary_category"] is not None
    assert item["fine_secondary_categories"] == []
    assert item["fine_category_evidence"] == []
    assert item["fine_reasons"] == [
        "qualified as Data/AI but no supported fine category is evidenced; classified OTHER"
    ]
    assert item["fine_classifier_version"] == FINE_VERSION


# --- D and E: reconciled rows the classifier deliberately left uncategorized --

NOT_QUALIFIED_REASON = (
    "opportunity is not qualified as Data/AI; no fine category is assigned"
)


def _seed_unqualified(connection: sqlite3.Connection, qualification: str, suffix: str) -> int:
    opportunity_id = _insert_opportunity(
        connection, suffix, last_seen_at="2026-01-03T00:00:00+00:00"
    )
    _insert_qualification(
        connection,
        opportunity_id,
        qualification=qualification,
        fine_primary_category=None,
        fine_secondary_categories_json="[]",
        fine_category_evidence_json="[]",
        fine_reasons_json=json.dumps([NOT_QUALIFIED_REASON]),
        fine_classifier_version=FINE_VERSION,
    )
    return opportunity_id


def test_reconciled_uncertain_row_has_no_category_but_states_it_was_classified(
    tmp_path, monkeypatch
) -> None:
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        _seed_unqualified(connection, "UNCERTAIN", "uncertain")
        connection.commit()
    finally:
        connection.close()

    item = _by_title(_get(monkeypatch, path).json())["TEST ONLY role uncertain"]

    assert item["fine_primary_category"] is None
    # Absence of a category is never OTHER, and the collections prove the
    # classifier ran rather than that nothing is known.
    assert item["fine_primary_category"] != "OTHER"
    assert item["fine_secondary_categories"] == []
    assert item["fine_category_evidence"] == []
    assert item["fine_reasons"] == [NOT_QUALIFIED_REASON]
    assert item["fine_classifier_version"] == FINE_VERSION


def test_reconciled_out_of_scope_row_uses_the_same_safe_absence_semantics(
    tmp_path, monkeypatch
) -> None:
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        _seed_unqualified(connection, "OUT_OF_SCOPE", "outofscope")
        connection.commit()
    finally:
        connection.close()

    item = _by_title(_get(monkeypatch, path).json())["TEST ONLY role outofscope"]

    assert item["fine_primary_category"] is None
    assert item["fine_primary_category"] != "OTHER"
    assert item["fine_secondary_categories"] == []
    assert item["fine_category_evidence"] == []
    assert item["fine_reasons"] == [NOT_QUALIFIED_REASON]
    assert item["fine_classifier_version"] == FINE_VERSION


# --- F and G: the two ways a row can be unclassified --------------------------


def test_legacy_migrated_but_never_reconciled_row_reads_as_never_classified(
    tmp_path, monkeypatch
) -> None:
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        opportunity_id = _insert_opportunity(
            connection, "legacy", last_seen_at="2026-01-02T00:00:00+00:00"
        )
        _insert_qualification(connection, opportunity_id, qualification="CORE_TARGET")
        connection.commit()
        stored = connection.execute(
            """SELECT fine_primary_category, fine_secondary_categories_json,
                      fine_category_evidence_json, fine_reasons_json,
                      fine_classifier_version
               FROM opportunity_qualifications WHERE opportunity_id = ?""",
            (opportunity_id,),
        ).fetchone()
    finally:
        connection.close()

    assert stored == (None, None, None, None, None)

    item = _by_title(_get(monkeypatch, path).json())["TEST ONLY role legacy"]

    # All five null together: nothing ran, and the API does not run it now.
    assert item["fine_primary_category"] is None
    assert item["fine_secondary_categories"] is None
    assert item["fine_category_evidence"] is None
    assert item["fine_reasons"] is None
    assert item["fine_classifier_version"] is None


def test_opportunity_without_a_qualification_row_is_still_listed_and_counted(
    tmp_path, monkeypatch
) -> None:
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        _seed_core(connection)
        _insert_opportunity(
            connection, "unqualified", last_seen_at="2026-01-02T00:00:00+00:00"
        )
        connection.commit()
        rows = connection.execute(
            "SELECT COUNT(*) FROM opportunity_qualifications"
        ).fetchone()[0]
    finally:
        connection.close()

    assert rows == 1

    payload = _get(monkeypatch, path).json()

    # The LEFT JOIN adds columns, never a filter: both opportunities are here.
    assert payload["returned"] == payload["total"] == 2
    item = _by_title(payload)["TEST ONLY role unqualified"]
    assert item["fine_primary_category"] is None
    assert item["fine_secondary_categories"] is None
    assert item["fine_category_evidence"] is None
    assert item["fine_reasons"] is None
    assert item["fine_classifier_version"] is None
    # The classified neighbour is unaffected by the row that has no qualification.
    assert _by_title(payload)["TEST ONLY role core"]["fine_primary_category"] == (
        "COMPUTER_VISION"
    )


def test_listing_filters_ordering_and_totals_are_unchanged_by_the_join(
    tmp_path, monkeypatch
) -> None:
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        _seed_core(connection)  # visible, last seen 2026-01-05
        _seed_unqualified(connection, "UNCERTAIN", "uncertain")  # 2026-01-03
        _insert_opportunity(
            connection, "unqualified", last_seen_at="2026-01-04T00:00:00+00:00"
        )
        hidden = _insert_opportunity(
            connection, "hidden", last_seen_at="2026-01-06T00:00:00+00:00", status="hidden"
        )
        inactive = _insert_opportunity(
            connection, "inactive", last_seen_at="2026-01-07T00:00:00+00:00", is_active=0
        )
        # Excluded rows carry a full classification, so exclusion cannot be an
        # accident of the join finding nothing to attach.
        for opportunity_id in (hidden, inactive):
            _insert_qualification(
                connection,
                opportunity_id,
                qualification="CORE_TARGET",
                fine_primary_category="DATA_SCIENCE",
                fine_secondary_categories_json="[]",
                fine_category_evidence_json="[]",
                fine_reasons_json='["TEST ONLY reason"]',
                fine_classifier_version=FINE_VERSION,
            )
        connection.commit()
    finally:
        connection.close()

    payload = _get(monkeypatch, path).json()

    assert payload["returned"] == payload["total"] == 3
    assert [item["canonical_title"] for item in payload["items"]] == [
        "TEST ONLY role core",
        "TEST ONLY role unqualified",
        "TEST ONLY role uncertain",
    ]
    assert all(item["status"] == "visible" for item in payload["items"])
    assert all("description" not in item for item in payload["items"])
    assert all(item["description_length"] == len(NEUTRAL_DESCRIPTION) for item in payload["items"])
    assert all(
        item["original_url"] == f"https://example.invalid/jobs/{suffix}"
        for item, suffix in zip(payload["items"], ("core", "unqualified", "uncertain"))
    )


def test_limit_still_bounds_the_listing_without_changing_the_total(
    tmp_path, monkeypatch
) -> None:
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        _seed_core(connection)
        _seed_unqualified(connection, "OUT_OF_SCOPE", "outofscope")
        connection.commit()
    finally:
        connection.close()

    payload = _get(monkeypatch, path, "?limit=1").json()

    assert payload["returned"] == 1
    assert payload["total"] == 2
    assert payload["items"][0]["canonical_title"] == "TEST ONLY role core"


# --- H: malformed persisted JSON fails through the public 503 -----------------

MALFORMED = (
    # Not JSON at all.
    ("fine_secondary_categories_json", "not json"),
    # A JSON object where the contract persists an array.
    ("fine_secondary_categories_json", '{"MLOPS": true}'),
    # A category outside the closed fine taxonomy — never silently substituted.
    ("fine_secondary_categories_json", '["QUANTUM_COMPUTING"]'),
    # A non-string member of an array of categories.
    ("fine_secondary_categories_json", "[7]"),
    # Evidence that is not an object.
    ("fine_category_evidence_json", '["computer vision engineer"]'),
    # Evidence missing a persisted key.
    (
        "fine_category_evidence_json",
        '[{"category":"MLOPS","field":"TITLE","kind":"ROLE_PHRASE"}]',
    ),
    # Evidence carrying a key the persisted shape does not have.
    (
        "fine_category_evidence_json",
        '[{"category":"MLOPS","field":"TITLE","kind":"ROLE_PHRASE","signal":"x","score":1}]',
    ),
    # An evidence field outside EvidenceField.
    (
        "fine_category_evidence_json",
        '[{"category":"MLOPS","field":"ORGANIZATION","kind":"ROLE_PHRASE","signal":"x"}]',
    ),
    # An evidence kind outside EvidenceKind.
    (
        "fine_category_evidence_json",
        '[{"category":"MLOPS","field":"TITLE","kind":"GUESS","signal":"x"}]',
    ),
    # A non-string signal.
    (
        "fine_category_evidence_json",
        '[{"category":"MLOPS","field":"TITLE","kind":"ROLE_PHRASE","signal":42}]',
    ),
    # A non-string reason.
    ("fine_reasons_json", "[null]"),
    # Reasons that are not an array.
    ("fine_reasons_json", '"one reason"'),
)


def test_every_malformed_persisted_fine_payload_answers_the_public_503(
    tmp_path, monkeypatch
) -> None:
    for index, (column, payload) in enumerate(MALFORMED):
        path = tmp_path / f"malformed-{index}.db"
        connection = sqlite3.connect(path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            apply_migrations(connection)
            opportunity_id = _seed_core(connection)
            connection.execute(
                f"UPDATE opportunity_qualifications SET {column} = ? WHERE opportunity_id = ?",
                (payload, opportunity_id),
            )
            connection.commit()
        finally:
            connection.close()

        response = _get(monkeypatch, path)

        assert response.status_code == 503, (column, payload)
        detail = response.json()["detail"]
        assert detail == "Opportunity data is temporarily unavailable."
        # No stack, no column name, no persisted value reaches the caller.
        body = response.text
        for leak in ("Traceback", column, "FineClassificationDecodeError", "json"):
            assert leak not in body, (column, payload, leak)


def test_half_written_fine_row_is_refused_rather_than_read_as_never_classified(
    tmp_path,
) -> None:
    """A version-less row carrying fine data contradicts migration 0025's CHECK.

    Two independent lines of defence are asserted here. The database refuses to
    produce the state at all, which is why it cannot be reached through a real
    table; and the read model refuses it too, rather than reporting a row with a
    category as the legacy "nothing ran" state because its version is missing.
    """
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        opportunity_id = _insert_opportunity(
            connection, "half", last_seen_at="2026-01-01T00:00:00+00:00"
        )
        _insert_qualification(connection, opportunity_id, qualification="CORE_TARGET")
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """UPDATE opportunity_qualifications
                   SET fine_primary_category = 'NLP' WHERE opportunity_id = ?""",
                (opportunity_id,),
            )
        connection.rollback()
    finally:
        connection.close()

    with pytest.raises(FineClassificationDecodeError):
        decode_fine_classification("CORE_TARGET", "NLP", None, None, None, None)


# --- I: a read changes nothing ------------------------------------------------


def test_reading_the_api_modifies_no_row_and_no_qualification_value(
    tmp_path, monkeypatch
) -> None:
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        _seed_core(connection)
        _seed_unqualified(connection, "UNCERTAIN", "uncertain")
        legacy = _insert_opportunity(
            connection, "legacy", last_seen_at="2026-01-02T00:00:00+00:00"
        )
        _insert_qualification(connection, legacy, qualification="CORE_TARGET")
        _insert_opportunity(
            connection, "unqualified", last_seen_at="2026-01-01T00:00:00+00:00"
        )
        connection.commit()
        opportunities_before = connection.execute(
            "SELECT * FROM opportunities ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    qualifications_before = _snapshot(path)

    assert _get(monkeypatch, path).status_code == 200
    assert _get(monkeypatch, path).status_code == 200

    connection = _connect(path)
    try:
        opportunities_after = connection.execute(
            "SELECT * FROM opportunities ORDER BY id"
        ).fetchall()
    finally:
        connection.close()

    # Every column, including `updated_at` and `classified_at`: a read leaves no
    # timestamp behind and reconciles nothing.
    assert _snapshot(path) == qualifications_before
    assert opportunities_after == opportunities_before
    assert len(qualifications_before) == 3


# --- Coherence between the persisted coarse and fine halves of one row --------
#
# Migration 0025 states the fine half against itself: nothing written, or the
# version and its three JSON columns written together. It cannot state the fine
# half against the coarse half in the same row, so every row below is one SQLite
# accepts and Phase 8 forbids. Each must fail closed through the public 503,
# because publishing any of them would do the one thing this phase exists to
# prevent: turn an unknown into an assertion, or an assertion into an unknown.

EVIDENCE_NLP = (
    '[{"category":"NLP","field":"TITLE","kind":"ROLE_PHRASE","signal":"nlp engineer"}]'
)
EVIDENCE_OTHER = (
    '[{"category":"OTHER","field":"TITLE","kind":"ROLE_PHRASE","signal":"nlp engineer"}]'
)

INCOHERENT = (
    # 1. An UNCERTAIN opportunity cannot be OTHER: OTHER claims the opportunity
    #    is demonstrably Data/AI, which UNCERTAIN says was never established.
    ("uncertain_other", "UNCERTAIN", "OTHER", "[]", "[]"),
    # 2. Nor can an OUT_OF_SCOPE one carry a supported sub-domain.
    ("out_of_scope_supported", "OUT_OF_SCOPE", "NLP", "[]", "[]"),
    # 3-4. A qualified opportunity that was classified has a verdict. "Nothing"
    #      is the answer for an ineligible row, never for CORE or ADJACENT.
    ("core_no_primary", "CORE_TARGET", None, "[]", "[]"),
    ("adjacent_no_primary", "ADJACENT_TARGET", None, "[]", "[]"),
    # 5-6. A row with no primary category cannot carry matched secondaries or
    #      evidence: the evidence would have produced a primary.
    ("uncertain_secondary", "UNCERTAIN", None, '["NLP"]', "[]"),
    ("uncertain_evidence", "UNCERTAIN", None, "[]", EVIDENCE_NLP),
    ("out_of_scope_secondary", "OUT_OF_SCOPE", None, '["NLP"]', "[]"),
    ("out_of_scope_evidence", "OUT_OF_SCOPE", None, "[]", EVIDENCE_NLP),
    # 7-8. OTHER *means* no supported sub-domain was evidenced, so a secondary
    #      category or evidence beside it contradicts the value itself.
    ("core_other_secondary", "CORE_TARGET", "OTHER", '["NLP"]', "[]"),
    ("core_other_evidence", "CORE_TARGET", "OTHER", "[]", EVIDENCE_NLP),
    ("adjacent_other_secondary", "ADJACENT_TARGET", "OTHER", '["NLP"]', "[]"),
    ("adjacent_other_evidence", "ADJACENT_TARGET", "OTHER", "[]", EVIDENCE_NLP),
    # 9. OTHER is a primary-only value: the classifier assigns it exactly when
    #    nothing was evidenced, so it can never itself be evidence.
    ("core_other_as_secondary", "CORE_TARGET", "NLP", '["OTHER"]', EVIDENCE_NLP),
    ("core_other_as_evidence", "CORE_TARGET", "NLP", "[]", EVIDENCE_OTHER),
    ("adjacent_other_as_secondary", "ADJACENT_TARGET", "NLP", '["OTHER"]', "[]"),
    ("adjacent_other_as_evidence", "ADJACENT_TARGET", "NLP", "[]", EVIDENCE_OTHER),
)


@pytest.mark.parametrize(
    ("qualification", "primary", "secondary", "evidence"),
    [case[1:] for case in INCOHERENT],
    ids=[case[0] for case in INCOHERENT],
)
def test_incoherent_persisted_coarse_and_fine_halves_answer_the_public_503(
    tmp_path, monkeypatch, qualification, primary, secondary, evidence
) -> None:
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        opportunity_id = _insert_opportunity(
            connection, "incoherent", last_seen_at="2026-01-05T00:00:00+00:00"
        )
        _insert_qualification(
            connection,
            opportunity_id,
            qualification=qualification,
            fine_primary_category=primary,
            fine_secondary_categories_json=secondary,
            fine_category_evidence_json=evidence,
            fine_reasons_json='["TEST ONLY persisted reason"]',
            fine_classifier_version=FINE_VERSION,
        )
        connection.commit()
        # The row really is one the schema accepts: the gap this test covers is
        # exactly the coherence migration 0025 cannot express.
        stored = connection.execute(
            """SELECT qualification, fine_primary_category
               FROM opportunity_qualifications WHERE opportunity_id = ?""",
            (opportunity_id,),
        ).fetchone()
    finally:
        connection.close()

    assert stored == (qualification, primary)

    response = _get(monkeypatch, path)

    assert response.status_code == 503
    assert response.json() == {"detail": "Opportunity data is temporarily unavailable."}
    for leak in ("Traceback", "FineClassificationDecodeError", qualification):
        assert leak not in response.text, leak


@pytest.mark.parametrize(
    "qualification", ["CORE_TARGET", "ADJACENT_TARGET", "OUT_OF_SCOPE", "UNCERTAIN"]
)
def test_legacy_never_fine_classified_row_is_legal_under_every_qualification(
    tmp_path, monkeypatch, qualification
) -> None:
    """State B is the pre-reconciliation row, and coarse outcome says nothing about it.

    Migration 0025 reached every existing row, whatever it was qualified as, and
    fine classification reached none of them. Refusing any of these would break
    a database that is merely not reconciled yet.
    """
    path = _database(tmp_path)
    connection = _connect(path)
    try:
        opportunity_id = _insert_opportunity(
            connection, "legacy", last_seen_at="2026-01-05T00:00:00+00:00"
        )
        _insert_qualification(connection, opportunity_id, qualification=qualification)
        connection.commit()
    finally:
        connection.close()

    response = _get(monkeypatch, path)

    assert response.status_code == 200
    item = _by_title(response.json())["TEST ONLY role legacy"]
    assert item["fine_primary_category"] is None
    assert item["fine_secondary_categories"] is None
    assert item["fine_category_evidence"] is None
    assert item["fine_reasons"] is None
    assert item["fine_classifier_version"] is None


def test_fine_values_without_a_qualification_row_are_refused(tmp_path) -> None:
    """A LEFT JOIN miss NULLs every joined column, coarse and fine alike.

    Fine data arriving with no coarse qualification therefore came from a row
    that does not exist. SQL cannot produce it — there is no row to attach it to
    — so the invariant is asserted against the decoder directly.
    """
    with pytest.raises(FineClassificationDecodeError):
        decode_fine_classification(
            None, "NLP", "[]", "[]", '["reason"]', FINE_VERSION
        )
    with pytest.raises(FineClassificationDecodeError):
        decode_fine_classification(None, None, None, None, None, FINE_VERSION)
    # The genuine LEFT JOIN miss stays legal and stays unclassified.
    assert decode_fine_classification(None, None, None, None, None, None) == (
        UNCLASSIFIED
    )


def test_a_qualification_outside_the_closed_coarse_vocabulary_is_refused(
    tmp_path,
) -> None:
    """The coarse value is validated against `Qualification`, not merely read."""
    with pytest.raises(FineClassificationDecodeError):
        decode_fine_classification(
            "PROBABLY_TARGET", "NLP", "[]", "[]", '["reason"]', FINE_VERSION
        )


def test_the_read_model_reuses_the_classifier_eligibility_contract() -> None:
    """The eligible coarse outcomes are the classifier's set, not a second copy.

    A duplicated pair here would keep passing today and start refusing valid
    persisted rows the day the fine classifier's own contract moves.
    """
    assert FINE_ELIGIBLE_QUALIFICATIONS == (
        Qualification.CORE_TARGET,
        Qualification.ADJACENT_TARGET,
    )
    assert set(Qualification) - set(FINE_ELIGIBLE_QUALIFICATIONS) == {
        Qualification.OUT_OF_SCOPE,
        Qualification.UNCERTAIN,
    }

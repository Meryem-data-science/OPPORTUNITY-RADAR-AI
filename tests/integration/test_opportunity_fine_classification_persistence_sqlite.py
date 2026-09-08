"""SQLite integration coverage for persisted fine Data/AI classification (Phase 8B.1).

The coarse contract is exercised in
``test_opportunity_qualification_persistence_sqlite.py``; this file covers what
migration 0025 adds to the same current row: the fine category, its evidence,
its reasons, and the second independent classifier version that decides, with
the fingerprint and the coarse version, whether a row is still current.

Everything here goes through the real classifiers wherever the assertion is
about *meaning*. Injection is used only where the test is about the persistence
mechanism itself — a version that moved, or a classifier that failed part-way.
"""

import json
from pathlib import Path
import sqlite3

import pytest

from services.collector.database.migrations import (
    DEFAULT_MIGRATIONS_DIRECTORY, apply_migrations, discover_migrations,
)
from services.collector.qualification.classifier import CLASSIFIER_VERSION, classify_opportunity
from services.collector.qualification.fine_classifier import (
    FINE_CLASSIFIER_VERSION, NOT_QUALIFIED_REASON, classify_fine_categories,
)
from services.collector.qualification.persistence import (
    QualificationPersistenceError, persist_qualifications,
)

FINE_COLUMNS = (
    "fine_primary_category", "fine_secondary_categories_json", "fine_category_evidence_json",
    "fine_reasons_json", "fine_classifier_version",
)


def _connection(tmp_path: Path, name: str = "fine-persist.db") -> sqlite3.Connection:
    connection = sqlite3.connect(tmp_path / name)
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(connection)
    return connection


def _migrations_below(tmp_path: Path, version: str) -> Path:
    """A migrations directory holding every migration before ``version``."""
    directory = tmp_path / f"migrations-below-{version}"
    directory.mkdir()
    for migration in discover_migrations(DEFAULT_MIGRATIONS_DIRECTORY):
        if migration.version < version:
            (directory / migration.path.name).write_text(
                migration.path.read_text(encoding="utf-8"), encoding="utf-8"
            )
    return directory


def _insert(connection: sqlite3.Connection, title: str, description: str) -> int:
    url = f"https://example.test/jobs/{title.lower().replace(' ', '-')}"
    cursor = connection.execute(
        """INSERT INTO opportunities (
            canonical_title, organization, location, description, discovered_at,
            first_seen_at, last_seen_at, source_url, application_url, canonical_url,
            status, is_active
        ) VALUES (?, 'Example Org', 'Casablanca', ?, '2026-01-01', '2026-01-01',
                  '2026-01-01', ?, ?, ?, 'visible', 1)""",
        (title, description, url, url, url),
    )
    connection.commit()
    return cursor.lastrowid


def _row(connection: sqlite3.Connection, opportunity_id: int) -> dict[str, object]:
    cursor = connection.execute(
        "SELECT * FROM opportunity_qualifications WHERE opportunity_id = ?", (opportunity_id,)
    )
    columns = [item[0] for item in cursor.description]
    return dict(zip(columns, cursor.fetchone(), strict=True))


def test_a_core_target_persists_its_primary_secondary_and_evidence(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    opportunity_id = _insert(
        connection, "Senior Data Scientist / ML Engineer",
        "Own model training and model evaluation for our products. Full-time role.",
    )

    summary = persist_qualifications(connection)
    row = _row(connection, opportunity_id)

    assert summary.created == summary.total == 1
    assert row["qualification"] == "CORE_TARGET"
    assert row["fine_primary_category"] == "MACHINE_LEARNING"
    assert "DATA_SCIENCE" in json.loads(row["fine_secondary_categories_json"])
    assert row["fine_classifier_version"] == FINE_CLASSIFIER_VERSION
    assert row["classifier_version"] == CLASSIFIER_VERSION
    assert json.loads(row["fine_reasons_json"])
    connection.close()


def test_an_adjacent_target_without_evidence_persists_other_and_empty_arrays(
    tmp_path: Path,
) -> None:
    """OTHER is a proven Data/AI opportunity with no assertable sub-domain."""
    connection = _connection(tmp_path)
    opportunity_id = _insert(
        connection, "Data Consultant",
        "Advise clients on their data strategy and governance. Full-time role.",
    )

    persist_qualifications(connection)
    row = _row(connection, opportunity_id)

    assert row["qualification"] == "ADJACENT_TARGET"
    assert row["fine_primary_category"] == "OTHER"
    assert json.loads(row["fine_secondary_categories_json"]) == []
    assert json.loads(row["fine_category_evidence_json"]) == []
    assert row["fine_classifier_version"] == FINE_CLASSIFIER_VERSION
    connection.close()


@pytest.mark.parametrize(
    "title, description, expected_qualification",
    [
        ("Interesting Opportunity", "Short", "UNCERTAIN"),
        ("Sales Manager", "Manage the regional sales team and its quota.", "OUT_OF_SCOPE"),
    ],
)
def test_an_unqualified_opportunity_is_classified_with_no_category_at_all(
    tmp_path: Path, title: str, description: str, expected_qualification: str
) -> None:
    """The second NULL semantics: the classifier ran and assigned nothing.

    This row is not the same thing as a row migration 0025 has never reached.
    Its version is written and its reasons say why there is no category, which
    is exactly what distinguishes it from the pre-reconciliation NULL state.
    """
    connection = _connection(tmp_path)
    opportunity_id = _insert(connection, title, description)

    persist_qualifications(connection)
    row = _row(connection, opportunity_id)

    assert row["qualification"] == expected_qualification
    assert row["fine_primary_category"] is None
    assert json.loads(row["fine_secondary_categories_json"]) == []
    assert json.loads(row["fine_category_evidence_json"]) == []
    assert json.loads(row["fine_reasons_json"]) == [NOT_QUALIFIED_REASON]
    assert row["fine_classifier_version"] == FINE_CLASSIFIER_VERSION
    connection.close()


def test_fine_evidence_is_persisted_as_semantic_objects_not_python_reprs(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    opportunity_id = _insert(
        connection, "Tech Lead Manager- MLRE, ML Systems",
        "Lead the team behind our ml systems. Full-time role.",
    )

    persist_qualifications(connection)
    row = _row(connection, opportunity_id)

    assert row["fine_primary_category"] == "MLOPS"
    assert json.loads(row["fine_category_evidence_json"]) == [
        {
            "category": "MLOPS", "field": "TITLE", "kind": "CONTEXT_PHRASE",
            "signal": "ml systems",
        }
    ]
    # Stable, compact, enum values only: no dataclass repr, no memory address,
    # and no whitespace that a byte-level comparison would trip over.
    assert row["fine_category_evidence_json"] == (
        '[{"category":"MLOPS","field":"TITLE","kind":"CONTEXT_PHRASE","signal":"ml systems"}]'
    )
    assert "FineEvidence" not in row["fine_category_evidence_json"]
    connection.close()


def test_evidence_order_follows_the_classifier_and_not_a_set(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    title = "Machine Learning Engineer, Computer Vision"
    description = "Train detection models and own model evaluation. Full-time role."
    opportunity_id = _insert(connection, title, description)
    expected = classify_fine_categories(
        title, description,
        qualification=classify_opportunity(
            title, description, source_url=None, application_url=None, canonical_url=None,
        ).qualification,
    )

    persist_qualifications(connection)
    row = _row(connection, opportunity_id)

    assert [item["category"] for item in json.loads(row["fine_category_evidence_json"])] == [
        str(item.category) for item in expected.evidence
    ]
    assert json.loads(row["fine_secondary_categories_json"]) == [
        str(category) for category in expected.secondary_categories
    ]
    connection.close()


def test_an_identical_second_run_changes_nothing(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    _insert(connection, "Data Engineer", "Build data pipelines and ETL jobs. Full-time role.")
    _insert(connection, "MLOps Engineer", "Own model serving and model monitoring. Full-time.")
    first = persist_qualifications(connection)
    before = connection.execute(
        "SELECT * FROM opportunity_qualifications ORDER BY opportunity_id"
    ).fetchall()

    second = persist_qualifications(connection)

    assert (first.created, first.total) == (2, 2)
    assert (second.created, second.updated, second.unchanged, second.total) == (0, 0, 2, 2)
    assert connection.execute(
        "SELECT * FROM opportunity_qualifications ORDER BY opportunity_id"
    ).fetchall() == before
    connection.close()


def test_a_new_fine_version_alone_reconciles_the_row(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    _insert(connection, "Data Engineer", "Build data pipelines and ETL jobs. Full-time role.")
    persist_qualifications(connection)

    summary = persist_qualifications(connection, fine_classifier_version="fine-data-ai-rules-test")

    assert (summary.created, summary.updated, summary.unchanged) == (0, 1, 0)
    assert connection.execute(
        "SELECT classifier_version, fine_classifier_version FROM opportunity_qualifications"
    ).fetchone() == (CLASSIFIER_VERSION, "fine-data-ai-rules-test")
    connection.close()


def test_a_new_coarse_version_alone_reconciles_the_row(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    _insert(connection, "Data Engineer", "Build data pipelines and ETL jobs. Full-time role.")
    persist_qualifications(connection)

    summary = persist_qualifications(connection, classifier_version="qualification-rules-test")

    assert (summary.created, summary.updated, summary.unchanged) == (0, 1, 0)
    assert connection.execute(
        "SELECT classifier_version, fine_classifier_version FROM opportunity_qualifications"
    ).fetchone() == ("qualification-rules-test", FINE_CLASSIFIER_VERSION)
    connection.close()


def test_a_changed_input_reconciles_the_row_under_unchanged_versions(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    opportunity_id = _insert(
        connection, "Data Engineer", "Build data pipelines and ETL jobs. Full-time role."
    )
    persist_qualifications(connection)
    old = connection.execute(
        "SELECT input_fingerprint, fine_primary_category FROM opportunity_qualifications"
    ).fetchone()

    connection.execute(
        "UPDATE opportunities SET canonical_title = 'NLP Engineer' WHERE id = ?",
        (opportunity_id,),
    )
    connection.commit()
    summary = persist_qualifications(connection)

    new = connection.execute(
        "SELECT input_fingerprint, fine_primary_category FROM opportunity_qualifications"
    ).fetchone()
    assert summary.updated == 1
    assert new[0] != old[0]
    assert (old[1], new[1]) == ("DATA_ENGINEERING", "NLP")
    connection.close()


def test_a_row_whose_fine_version_is_null_is_always_reconciled(tmp_path: Path) -> None:
    """The migration-transition state: coarse data current, fine half never written."""
    connection = _connection(tmp_path)
    _insert(connection, "Data Engineer", "Build data pipelines and ETL jobs. Full-time role.")
    persist_qualifications(connection)
    coarse_before = connection.execute(
        "SELECT qualification, primary_domain, classifier_version, input_fingerprint"
        " FROM opportunity_qualifications"
    ).fetchone()
    connection.execute(
        """UPDATE opportunity_qualifications SET
            fine_primary_category = NULL, fine_secondary_categories_json = NULL,
            fine_category_evidence_json = NULL, fine_reasons_json = NULL,
            fine_classifier_version = NULL"""
    )
    connection.commit()

    summary = persist_qualifications(connection)

    assert (summary.created, summary.updated, summary.unchanged) == (0, 1, 0)
    assert connection.execute(
        "SELECT qualification, primary_domain, classifier_version, input_fingerprint"
        " FROM opportunity_qualifications"
    ).fetchone() == coarse_before
    assert connection.execute(
        "SELECT fine_primary_category, fine_classifier_version FROM opportunity_qualifications"
    ).fetchone() == ("DATA_ENGINEERING", FINE_CLASSIFIER_VERSION)
    connection.close()


def test_a_database_without_migration_0025_is_refused_before_any_write(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "below-0025.db")
    connection.execute("PRAGMA foreign_keys = ON")
    apply_migrations(connection, _migrations_below(tmp_path, "0025"))
    _insert(connection, "Data Engineer", "Build data pipelines and ETL jobs. Full-time role.")

    with pytest.raises(QualificationPersistenceError) as refusal:
        persist_qualifications(connection)

    # A named missing migration, not "sqlite3.OperationalError: no such column".
    assert "migration 0025 is required" in str(refusal.value)
    assert "fine_primary_category" in str(refusal.value)
    assert connection.execute("SELECT COUNT(*) FROM opportunity_qualifications").fetchone() == (0,)
    # Persistence never applies a migration on its own.
    columns = {row[1] for row in connection.execute("PRAGMA table_info(opportunity_qualifications)")}
    assert not columns & set(FINE_COLUMNS)
    connection.close()


def test_a_failing_fine_classifier_rolls_back_the_whole_batch(tmp_path: Path) -> None:
    """Coarse and fine are one derived record: no row may keep half of a batch."""
    connection = _connection(tmp_path)
    _insert(connection, "Data Engineer", "Build data pipelines and ETL jobs. Full-time role.")
    _insert(connection, "Data Scientist", "Own experimentation and statistical modeling.")
    persist_qualifications(connection)
    before = connection.execute(
        "SELECT * FROM opportunity_qualifications ORDER BY opportunity_id"
    ).fetchall()
    # Both rows now need reconciliation, so the first is rewritten successfully
    # inside the transaction before the second one fails.
    connection.execute("UPDATE opportunities SET description = description || ' Updated.'")
    connection.commit()
    calls = 0

    def failing_fine_classifier(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected fine classifier failure")
        return classify_fine_categories(*args, **kwargs)

    with pytest.raises(RuntimeError, match="injected fine classifier failure"):
        persist_qualifications(connection, fine_classifier=failing_fine_classifier)

    assert calls == 2
    assert connection.execute(
        "SELECT * FROM opportunity_qualifications ORDER BY opportunity_id"
    ).fetchall() == before
    connection.close()


def test_a_fine_failure_on_a_first_run_leaves_no_row_behind(tmp_path: Path) -> None:
    connection = _connection(tmp_path)
    _insert(connection, "Data Engineer", "Build data pipelines and ETL jobs. Full-time role.")
    _insert(connection, "Data Scientist", "Own experimentation and statistical modeling.")

    def failing_fine_classifier(*args, **kwargs):
        if kwargs.get("qualification") is not None and args[0] == "Data Scientist":
            raise RuntimeError("injected fine classifier failure")
        return classify_fine_categories(*args, **kwargs)

    with pytest.raises(RuntimeError, match="injected fine classifier failure"):
        persist_qualifications(connection, fine_classifier=failing_fine_classifier)

    assert connection.execute("SELECT COUNT(*) FROM opportunity_qualifications").fetchone() == (0,)
    connection.close()


def test_the_fine_classifier_sees_only_title_description_and_qualification(
    tmp_path: Path,
) -> None:
    """Structural neutrality: employer, location and source are not arguments."""
    connection = _connection(tmp_path)
    _insert(connection, "Data Engineer", "Build data pipelines and ETL jobs. Full-time role.")
    seen: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def recording_fine_classifier(*args, **kwargs):
        seen.append((args, kwargs))
        return classify_fine_categories(*args, **kwargs)

    persist_qualifications(connection, fine_classifier=recording_fine_classifier)

    (args, kwargs), = seen
    assert args == ("Data Engineer", "Build data pipelines and ETL jobs. Full-time role.")
    assert set(kwargs) == {"qualification"}
    assert str(kwargs["qualification"]) == "CORE_TARGET"
    connection.close()


def test_inactive_and_merged_rows_are_excluded_from_fine_persistence_too(
    tmp_path: Path,
) -> None:
    connection = _connection(tmp_path)
    kept = _insert(connection, "Data Engineer", "Build data pipelines and ETL jobs. Full-time.")
    hidden = _insert(connection, "Data Scientist", "Own experimentation and modeling.")
    merged = _insert(connection, "ML Engineer", "Own model training and evaluation.")
    connection.execute("UPDATE opportunities SET is_active = 0 WHERE id = ?", (hidden,))
    connection.execute(
        "UPDATE opportunities SET status = 'merged_duplicate' WHERE id = ?", (merged,)
    )
    connection.commit()

    summary = persist_qualifications(connection)

    assert summary.total == summary.created == 1
    assert connection.execute(
        "SELECT opportunity_id, fine_primary_category FROM opportunity_qualifications"
    ).fetchall() == [(kept, "DATA_ENGINEERING")]
    connection.close()


@pytest.mark.parametrize("version", ["", None])
def test_an_empty_fine_classifier_version_is_refused(tmp_path: Path, version: object) -> None:
    connection = _connection(tmp_path)
    _insert(connection, "Data Engineer", "Build data pipelines and ETL jobs. Full-time role.")
    with pytest.raises(ValueError, match="fine_classifier_version"):
        persist_qualifications(connection, fine_classifier_version=version)
    connection.close()

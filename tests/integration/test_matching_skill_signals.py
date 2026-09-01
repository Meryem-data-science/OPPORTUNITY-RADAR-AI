"""Phase 4.2 through the real Phase 4.1 loader and disposable SQLite."""

import pytest

from services.collector.database.connection import connect_database
from services.collector.database.migrations import apply_migrations
from services.collector.extractors.opportunity_constraints.requirements.service import (
    extract_one_opportunity_requirements,
)
from services.collector.extractors.opportunity_constraints.service import (
    synchronize_opportunity_constraints,
)
from services.collector.matching import (
    RequirementsState,
    SkillSignalKind,
    SkillSignalSource,
    build_opportunity_skill_signals,
    load_opportunity_matching_input,
)


@pytest.fixture
def database(tmp_path):
    connection = connect_database(tmp_path / "signals.db")
    apply_migrations(connection)
    yield connection
    connection.close()


def _opportunity(connection, title, description):
    opportunity_id = int(connection.execute(
        """INSERT INTO opportunities
           (canonical_title, organization, description, discovered_at, first_seen_at,
            last_seen_at, source_url, status)
           VALUES (?, 'TEST ONLY', ?, 't', 't', 't', 'https://example.invalid', 'new')
           RETURNING id""",
        (title, description),
    ).fetchone()[0])
    connection.commit()
    return opportunity_id


def test_real_loader_states_signals_and_builder_are_read_only(database):
    rich_id = _opportunity(
        database,
        "Python Data Engineer",
        "Required Qualifications\nPython\nPreferred Skills\nAirflow\n"
        "Responsibilities\nBuild Kafka pipelines.\nTech Stack\nSQL and Python.",
    )
    empty_id = _opportunity(database, "Analytics role", "Tech Stack\nPyTorch")
    unknown_id = _opportunity(database, "SQL analyst", "Tech Stack\nTensorFlow")
    synchronize_opportunity_constraints(database)
    extract_one_opportunity_requirements(database, rich_id)
    extract_one_opportunity_requirements(database, empty_id)

    before = database.total_changes
    rich = build_opportunity_skill_signals(load_opportunity_matching_input(database, rich_id))
    empty = build_opportunity_skill_signals(load_opportunity_matching_input(database, empty_id))
    unknown = build_opportunity_skill_signals(load_opportunity_matching_input(database, unknown_id))
    after = database.total_changes

    rich_signals = {signal.canonical_key: signal for signal in rich.signals}
    assert rich_signals["python"].kind is SkillSignalKind.REQUIRED
    assert rich_signals["python"].sources == (
        SkillSignalSource.REQUIREMENTS,
        SkillSignalSource.TITLE,
        SkillSignalSource.TECH_STACK,
    )
    assert rich_signals["apache airflow"].kind is SkillSignalKind.PREFERRED
    assert rich_signals["apache kafka"].kind is SkillSignalKind.CONTEXT
    assert empty.requirements_state is RequirementsState.EXTRACTED
    assert {signal.canonical_key for signal in empty.signals} == {"pytorch"}
    assert unknown.requirements_state is RequirementsState.UNKNOWN
    assert {signal.canonical_key for signal in unknown.signals} == {"sql"}
    assert after == before

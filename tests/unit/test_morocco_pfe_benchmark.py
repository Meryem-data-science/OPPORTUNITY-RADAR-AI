"""Tests for the Phase 7C.1 Morocco source map and gold benchmark foundation.

Every test here is offline. No test opens a socket, and none should ever: the
URLs in the benchmark are recorded facts, and checking whether one still
resolves is a live concern that would make this suite fail for reasons that
have nothing to do with the artefacts it validates.

The rejection tests are written against small in-memory documents rather than
by mutating the committed files, so a real artefact is never rewritten to prove
that a validator refuses something.
"""

from copy import deepcopy
import json
from pathlib import Path

import pytest
import yaml

from evaluation.morocco_pfe.validator import (
    BENCHMARK_COUNTRY_CODE,
    COLLECTION_STRATEGIES,
    COVERAGE_ROLES,
    DEFAULT_BENCHMARK_PATH,
    DEFAULT_MANIFEST_PATH,
    DEFAULT_SOURCE_MAP_PATH,
    FUTURE_STRATEGIES,
    INTEGRATION_STATUSES,
    OBSERVATION_HORIZONS,
    PRIORITIES,
    PRODUCTION_ACTIVE_STATUS,
    SOURCE_AUTHORITIES,
    SOURCE_CLASSES,
    BenchmarkValidationError,
    SourceMapValidationError,
    check_source_map_against_production_registry,
    load_benchmark_records,
    load_manifest,
    load_source_map,
    parse_benchmark_lines,
    parse_source_map,
    validate_benchmark,
    validate_manifest_document,
)
from services.collector.sources import DEFAULT_SOURCE_REGISTRY, load_source_registry
from services.digital_twin.preferences.models import OpportunityType

PRODUCTION_SOURCE_REGISTRY = Path("config/sources.yaml")


def seed_record() -> dict[str, object]:
    """Return the first committed benchmark row, as a mutable copy."""
    return deepcopy(load_benchmark_records(DEFAULT_BENCHMARK_PATH)[0])


def as_jsonl(*records: dict[str, object]) -> str:
    return "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)


def source_map_document() -> dict[str, object]:
    """Return the committed source map document, as a mutable copy."""
    return yaml.safe_load(DEFAULT_SOURCE_MAP_PATH.read_text(encoding="utf-8"))


# --------------------------------------------------------------- benchmark ---


def test_committed_benchmark_and_manifest_validate() -> None:
    report = validate_benchmark(DEFAULT_BENCHMARK_PATH, DEFAULT_MANIFEST_PATH)

    assert report.record_count == 2
    assert report.status == "DRAFT"
    assert report.target_minimum_rows >= 60
    assert report.evaluation_ready is False
    assert report.rows_missing_for_target == report.target_minimum_rows - 2


def test_seed_rows_are_the_two_real_observed_stage_ma_opportunities() -> None:
    records = load_benchmark_records(DEFAULT_BENCHMARK_PATH)

    by_id = {record["benchmark_id"]: record for record in records}
    assert sorted(by_id) == ["stage-ma-9233", "stage-ma-9279"]
    for record in records:
        # Identity is derived from the source, never from `opportunities.id`.
        assert record["benchmark_id"].startswith("stage-ma-")
        assert record["source_name"] == "Stage.ma"
        assert record["country_code"] == BENCHMARK_COUNTRY_CODE
        assert record["historical_or_live"] == "HISTORICAL"
        assert record["source_authority"] == "JOB_BOARD"
        assert record["expected_opportunity_type"] == OpportunityType.PFE.value
        assert record["expected_data_ai"] is True
        assert record["source_url"].startswith("https://www.stage.ma/offres-stage/")
        # No official employer URL is known for either row, and none is invented.
        assert record["official_application_url"] is None

        # A publication date is not a cohort, so nothing derives one from it.
        assert record["pfe_cohort_year"] is None

    assert by_id["stage-ma-9279"]["organization"] == "ARRA Engineering"
    assert by_id["stage-ma-9279"]["published_at"] == "2026-03-02"
    assert by_id["stage-ma-9233"]["organization"] == "PionovaAI"
    assert by_id["stage-ma-9233"]["published_at"] == "2026-01-31"


def test_seed_cohort_year_is_null_because_no_evidence_states_it() -> None:
    """`pfe_cohort_year` is an observed gold fact, never a derived one.

    A PFE campaign published in October 2025 can be the 2026 cohort, and one
    published in August 2026 can be the 2027 cohort. Neither seed posting
    states its cohort, so the field stays null rather than inheriting the year
    of `published_at` — a benchmark that guesses a label cannot be used to
    score anything against that label.
    """
    records = load_benchmark_records(DEFAULT_BENCHMARK_PATH)

    assert records
    for record in records:
        assert record["pfe_cohort_year"] is None
        published_year = int(str(record["published_at"])[:4])
        assert record["pfe_cohort_year"] != published_year

    # Null is a real answer here, and a stated cohort is still accepted.
    record = seed_record()
    record["pfe_cohort_year"] = 2027
    assert parse_benchmark_lines(as_jsonl(record))[0]["pfe_cohort_year"] == 2027


def test_benchmark_ids_are_unique_and_a_duplicate_is_rejected() -> None:
    records = load_benchmark_records(DEFAULT_BENCHMARK_PATH)
    identifiers = [record["benchmark_id"] for record in records]
    assert len(set(identifiers)) == len(identifiers)

    duplicate = deepcopy(records[0])
    with pytest.raises(BenchmarkValidationError, match="duplicate benchmark_id"):
        parse_benchmark_lines(as_jsonl(records[0], duplicate))


def test_invalid_jsonl_syntax_is_rejected() -> None:
    with pytest.raises(BenchmarkValidationError, match="invalid JSON"):
        parse_benchmark_lines('{"benchmark_id": "stage-ma-9279",\n')


@pytest.mark.parametrize("empty", ["", "   ", None, 7])
def test_empty_or_non_string_benchmark_id_is_rejected(empty: object) -> None:
    record = seed_record()
    record["benchmark_id"] = empty
    with pytest.raises(BenchmarkValidationError, match="benchmark_id"):
        parse_benchmark_lines(as_jsonl(record))


@pytest.mark.parametrize("country", ["FR", "ma", "MAR", "", None])
def test_country_other_than_ma_is_rejected(country: object) -> None:
    record = seed_record()
    record["country_code"] = country
    with pytest.raises(BenchmarkValidationError, match="country_code"):
        parse_benchmark_lines(as_jsonl(record))


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url",
        "ftp://www.stage.ma/offres-stage/9279",
        "www.stage.ma/offres-stage/9279",
        "https://",
        None,
    ],
)
def test_invalid_source_url_is_rejected(url: object) -> None:
    record = seed_record()
    record["source_url"] = url
    with pytest.raises(BenchmarkValidationError, match="source_url"):
        parse_benchmark_lines(as_jsonl(record))


def test_official_application_url_may_be_null_but_not_malformed() -> None:
    record = seed_record()
    record["official_application_url"] = None
    assert parse_benchmark_lines(as_jsonl(record))[0][
        "official_application_url"
    ] is None

    record["official_application_url"] = "javascript:void(0)"
    with pytest.raises(BenchmarkValidationError, match="official_application_url"):
        parse_benchmark_lines(as_jsonl(record))


@pytest.mark.parametrize("horizon", ["EXPIRED", "historical", "ARCHIVED", None])
def test_invalid_historical_or_live_is_rejected(horizon: object) -> None:
    record = seed_record()
    record["historical_or_live"] = horizon
    with pytest.raises(BenchmarkValidationError, match="historical_or_live"):
        parse_benchmark_lines(as_jsonl(record))


@pytest.mark.parametrize("authority", ["BOARD", "job_board", "SCRAPER", None])
def test_invalid_source_authority_is_rejected(authority: object) -> None:
    record = seed_record()
    record["source_authority"] = authority
    with pytest.raises(BenchmarkValidationError, match="source_authority"):
        parse_benchmark_lines(as_jsonl(record))


def test_expected_opportunity_type_uses_the_shared_closed_registry() -> None:
    record = seed_record()
    for member in OpportunityType:
        record["expected_opportunity_type"] = member.value
        assert parse_benchmark_lines(as_jsonl(record))

    record["expected_opportunity_type"] = "GRADUATION_PROJECT"
    with pytest.raises(BenchmarkValidationError, match="expected_opportunity_type"):
        parse_benchmark_lines(as_jsonl(record))


@pytest.mark.parametrize("label", [True, False, None])
def test_expected_data_ai_accepts_exactly_three_answers(label: object) -> None:
    record = seed_record()
    record["expected_data_ai"] = label
    assert parse_benchmark_lines(as_jsonl(record))[0]["expected_data_ai"] is label


@pytest.mark.parametrize("label", ["true", 1, "YES"])
def test_expected_data_ai_rejects_anything_else(label: object) -> None:
    record = seed_record()
    record["expected_data_ai"] = label
    with pytest.raises(BenchmarkValidationError, match="expected_data_ai"):
        parse_benchmark_lines(as_jsonl(record))


@pytest.mark.parametrize(
    "published_at", ["02/03/2026", "2026-3-2", "20260302", "2026-13-02"]
)
def test_published_at_must_be_null_or_an_iso_date(published_at: str) -> None:
    record = seed_record()
    record["published_at"] = published_at
    with pytest.raises(BenchmarkValidationError, match="published_at"):
        parse_benchmark_lines(as_jsonl(record))

    record["published_at"] = None
    assert parse_benchmark_lines(as_jsonl(record))[0]["published_at"] is None


def test_observed_at_is_required_and_cannot_precede_publication() -> None:
    record = seed_record()
    record["observed_at"] = None
    with pytest.raises(BenchmarkValidationError, match="observed_at"):
        parse_benchmark_lines(as_jsonl(record))

    record = seed_record()
    record["published_at"] = "2026-03-02"
    record["observed_at"] = "2026-03-01"
    with pytest.raises(BenchmarkValidationError, match="precedes published_at"):
        parse_benchmark_lines(as_jsonl(record))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Sample PFE offer"),
        ("organization", "Acme SARL"),
        ("organization", "Example Corp"),
        ("title", "TODO fill this in"),
        ("source_name", "Placeholder board"),
    ],
)
def test_manifestly_synthetic_records_are_rejected(field: str, value: str) -> None:
    record = seed_record()
    record[field] = value
    with pytest.raises(BenchmarkValidationError, match="synthetic"):
        parse_benchmark_lines(as_jsonl(record))


def test_placeholder_urls_are_rejected() -> None:
    record = seed_record()
    record["source_url"] = "https://example.com/offres-stage/9279"
    with pytest.raises(BenchmarkValidationError, match="placeholder host"):
        parse_benchmark_lines(as_jsonl(record))


def test_a_record_missing_or_gaining_a_field_is_rejected() -> None:
    record = seed_record()
    del record["notes"]
    with pytest.raises(BenchmarkValidationError, match="missing field"):
        parse_benchmark_lines(as_jsonl(record))

    record = seed_record()
    record["opportunity_id"] = 12
    with pytest.raises(BenchmarkValidationError, match="unexpected field"):
        parse_benchmark_lines(as_jsonl(record))


def test_no_seed_record_carries_an_operational_database_identity() -> None:
    for record in load_benchmark_records(DEFAULT_BENCHMARK_PATH):
        assert "opportunity_id" not in record
        assert "id" not in record


# ---------------------------------------------------------------- manifest ---


def test_committed_manifest_declares_a_draft_seed() -> None:
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)

    assert manifest["benchmark_name"] == "morocco_pfe_gold_v1"
    assert manifest["scope_country"] == BENCHMARK_COUNTRY_CODE
    assert manifest["status"] == "DRAFT"
    assert manifest["created_for_phase"] == "7C.1"
    assert manifest["target_minimum_rows"] >= 60
    assert manifest["evaluation_ready"] is False
    assert manifest["current_rows"] == 2


def test_manifest_row_count_must_match_the_jsonl(tmp_path: Path) -> None:
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    manifest["current_rows"] = 47
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BenchmarkValidationError, match="current_rows"):
        validate_benchmark(DEFAULT_BENCHMARK_PATH, manifest_path)


def test_evaluation_ready_is_refused_while_the_seed_is_too_small(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    manifest["evaluation_ready"] = True
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(BenchmarkValidationError, match="evaluation_ready must be false"):
        validate_benchmark(DEFAULT_BENCHMARK_PATH, manifest_path)


def test_a_benchmark_at_its_target_may_be_declared_ready_after_human_review(
    tmp_path: Path,
) -> None:
    """The readiness rule is coherence, not a ceiling on the benchmark's life."""
    seed = seed_record()
    rows = []
    for index in range(60):
        row = deepcopy(seed)
        row["benchmark_id"] = f"stage-ma-{9000 + index}"
        rows.append(row)
    benchmark_path = tmp_path / "gold.jsonl"
    benchmark_path.write_text(as_jsonl(*rows), encoding="utf-8")

    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    manifest["current_rows"] = 60
    manifest["evaluation_ready"] = True
    manifest["status"] = "REVIEWED"
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    report = validate_benchmark(benchmark_path, manifest_path)
    assert report.evaluation_ready is True
    assert report.rows_missing_for_target == 0


def test_manifest_scope_country_must_be_ma() -> None:
    manifest = load_manifest(DEFAULT_MANIFEST_PATH)
    manifest["scope_country"] = "FR"
    with pytest.raises(BenchmarkValidationError, match="scope_country"):
        validate_manifest_document(manifest)


# -------------------------------------------------------------- source map ---


def test_committed_source_map_loads() -> None:
    source_map = load_source_map(DEFAULT_SOURCE_MAP_PATH)

    assert source_map.map_name == "morocco_pfe_source_coverage"
    assert source_map.version == "v1"
    assert source_map.scope_country == BENCHMARK_COUNTRY_CODE
    assert source_map.created_for_phase == "7C.1"
    assert len(source_map.sources) == 14
    assert len({entry.id for entry in source_map.sources}) == len(source_map.sources)
    for priority in ("P0", "P1", "P2", "P3"):
        assert source_map.by_priority(priority), f"{priority} has no source"


def test_every_mapped_source_is_morocco_scoped_and_uses_closed_registries() -> None:
    for entry in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources:
        assert entry.country == BENCHMARK_COUNTRY_CODE
        assert entry.source_class in SOURCE_CLASSES
        assert entry.priority in PRIORITIES
        assert entry.coverage_role in COVERAGE_ROLES
        assert entry.collection_strategy in COLLECTION_STRATEGIES
        assert entry.integration_status in INTEGRATION_STATUSES


def test_the_expected_morocco_sources_are_mapped() -> None:
    by_id = {entry.id: entry for entry in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources}

    # ReKrute was evaluated in 7C.3 and deliberately not selected; it is no
    # longer a P1 collector candidate. See the not-selected tests below.
    assert by_id["rekrute"].priority == "P2"
    assert by_id["stagiaires_ma"].priority == "P1"
    assert by_id["stage_ma"].priority == "P1"
    assert by_id["talentsoft_hosted_career_sites"].source_class == "ATS"
    for identifier in ("dreamjob_ma", "wadifaweb", "ekhadma", "indeed_maroc"):
        assert by_id[identifier].priority == "P2"
    for identifier in ("marocannonces", "pfe_daba", "interactjob"):
        assert by_id[identifier].priority == "P3"
        assert by_id[identifier].coverage_role == "AUDIT"


def test_linkedin_is_collected_through_gmail_alerts_only() -> None:
    source_map = load_source_map(DEFAULT_SOURCE_MAP_PATH)
    linkedin = next(
        entry for entry in source_map.sources if entry.source_class == "LINKEDIN_ALERT"
    )

    assert linkedin.id == "linkedin_job_alert_email"
    assert linkedin.collection_strategy == "GMAIL_ALERT"
    assert linkedin.integration_status == "ACTIVE"
    assert linkedin.production_source_id == "linkedin_job_alert_email"
    # There is no scraping strategy to choose, in this entry or in the registry.
    assert "SCRAPER" not in COLLECTION_STRATEGIES
    assert not any(
        "scrap" in strategy.lower() for strategy in COLLECTION_STRATEGIES
    )


def test_a_linkedin_source_may_not_declare_another_strategy() -> None:
    document = source_map_document()
    for entry in document["sources"]:
        if entry["source_class"] == "LINKEDIN_ALERT":
            entry["collection_strategy"] = "FUTURE_COLLECTOR"
            entry["integration_status"] = "CANDIDATE"
            entry["production_source_id"] = None
    with pytest.raises(SourceMapValidationError, match="GMAIL_ALERT only"):
        parse_source_map(document)


def test_only_really_configured_sources_may_claim_to_be_active() -> None:
    source_map = load_source_map(DEFAULT_SOURCE_MAP_PATH)
    configured = {source.id for source in load_source_registry(PRODUCTION_SOURCE_REGISTRY)}

    active = check_source_map_against_production_registry(
        source_map, PRODUCTION_SOURCE_REGISTRY
    )
    assert [entry.id for entry in active] == [
        "linkedin_job_alert_email",
        "stagiaires_ma",
    ]
    for entry in source_map.sources:
        if entry.integration_status == "ACTIVE":
            assert entry.production_source_id in configured
        else:
            assert entry.production_source_id is None


def test_an_active_claim_without_a_configured_collector_is_rejected() -> None:
    document = source_map_document()
    for entry in document["sources"]:
        if entry["id"] == "rekrute":
            entry["integration_status"] = "ACTIVE"
            entry["collection_strategy"] = "EXISTING_COLLECTOR"
            entry["production_source_id"] = "rekrute_collector"
    source_map = parse_source_map(document)

    with pytest.raises(SourceMapValidationError, match="does not configure"):
        check_source_map_against_production_registry(
            source_map, PRODUCTION_SOURCE_REGISTRY
        )


def test_an_enabled_but_inactive_production_source_may_not_be_active(
    tmp_path: Path,
) -> None:
    """ACTIVE means what `RadarAgent.run_once` means: enabled *and* active.

    A source that is enabled but carries any other status is never collected,
    so representing it as ACTIVE would credit the coverage map with a source
    nobody reads.
    """
    registry_path = tmp_path / "sources.yaml"
    registry_path.write_text(
        "sources:\n"
        "  - id: linkedin_job_alert_email\n"
        "    type: gmail_linkedin_alert\n"
        "    enabled: true\n"
        "    status: inactive\n"
        "    gmail_query: from:linkedin.com\n"
        "    gmail_message_limit: 50\n",
        encoding="utf-8",
    )
    source_map = load_source_map(DEFAULT_SOURCE_MAP_PATH)

    with pytest.raises(SourceMapValidationError, match="never runs it"):
        check_source_map_against_production_registry(source_map, registry_path)


def test_a_disabled_production_source_may_not_be_active(tmp_path: Path) -> None:
    registry_path = tmp_path / "sources.yaml"
    registry_path.write_text(
        "sources:\n"
        "  - id: linkedin_job_alert_email\n"
        "    type: gmail_linkedin_alert\n"
        "    enabled: false\n"
        "    status: active\n"
        "    gmail_query: from:linkedin.com\n"
        "    gmail_message_limit: 50\n",
        encoding="utf-8",
    )
    source_map = load_source_map(DEFAULT_SOURCE_MAP_PATH)

    with pytest.raises(SourceMapValidationError, match="is disabled"):
        check_source_map_against_production_registry(source_map, registry_path)


def test_a_gmail_alert_entry_must_name_a_gmail_alert_production_source(
    tmp_path: Path,
) -> None:
    """The strategy and the configured collector type have to agree."""
    registry_path = tmp_path / "sources.yaml"
    registry_path.write_text(
        "sources:\n"
        "  - id: linkedin_job_alert_email\n"
        "    type: greenhouse\n"
        "    enabled: true\n"
        "    status: active\n"
        "    organization: Scale AI\n"
        "    board_token: scaleai\n",
        encoding="utf-8",
    )
    source_map = load_source_map(DEFAULT_SOURCE_MAP_PATH)

    with pytest.raises(SourceMapValidationError, match="not 'gmail_linkedin_alert'"):
        check_source_map_against_production_registry(source_map, registry_path)


def test_the_active_invariant_matches_what_the_radar_agent_runs() -> None:
    """The map's ACTIVE rule is the agent's eligibility rule, not a copy of it.

    `RadarAgent.run_once` keeps `source.enabled and source.status == "active"`.
    Reading the real catalogue through both makes a future divergence a test
    failure rather than a silently overstated coverage denominator.
    """
    configured = load_source_registry(PRODUCTION_SOURCE_REGISTRY)
    collected_by_the_agent = {
        source.id
        for source in configured
        if source.enabled and source.status == PRODUCTION_ACTIVE_STATUS
    }
    active = check_source_map_against_production_registry(
        load_source_map(DEFAULT_SOURCE_MAP_PATH), PRODUCTION_SOURCE_REGISTRY
    )

    assert {entry.production_source_id for entry in active} <= collected_by_the_agent


def test_a_candidate_source_may_not_claim_an_implemented_strategy() -> None:
    document = source_map_document()
    for entry in document["sources"]:
        if entry["id"] == "stage_ma":
            entry["collection_strategy"] = "EXISTING_COLLECTOR"
    with pytest.raises(SourceMapValidationError, match="claims an implemented collector"):
        parse_source_map(document)


def test_a_source_outside_morocco_is_rejected() -> None:
    document = source_map_document()
    document["sources"][0]["country"] = "FR"
    with pytest.raises(SourceMapValidationError, match="country"):
        parse_source_map(document)


def test_an_unrecorded_homepage_url_must_say_so() -> None:
    document = source_map_document()
    for entry in document["sources"]:
        if entry["id"] == "wadifaweb":
            entry["homepage_url_status"] = "WELL_KNOWN_UNVERIFIED"
    with pytest.raises(SourceMapValidationError, match="homepage_url_status"):
        parse_source_map(document)


def test_oracle_is_a_live_canary_and_not_a_gold_opportunity_row() -> None:
    source_map = load_source_map(DEFAULT_SOURCE_MAP_PATH)
    canaries = source_map.live_canaries

    assert [entry.id for entry in canaries] == ["oracle_morocco_rd_careers"]
    oracle = canaries[0]
    assert oracle.source_class == "OFFICIAL_CAREER"
    assert oracle.homepage_url == "https://www.oracle.com/ma/careers/research-development/"
    assert oracle.integration_status == "NEEDS_VERIFICATION"
    assert oracle.production_source_id is None

    # A canary is evidence that a source publishes this kind of thing. It is
    # never silently promoted into an opportunity-level benchmark row.
    benchmark_urls = {
        record["source_url"] for record in load_benchmark_records(DEFAULT_BENCHMARK_PATH)
    }
    assert oracle.homepage_url not in benchmark_urls
    assert not any("oracle" in url.lower() for url in benchmark_urls)


# ------------------------------------------------------------- separation ---


def test_the_coverage_map_is_not_the_production_source_registry() -> None:
    source_map = load_source_map(DEFAULT_SOURCE_MAP_PATH)

    assert source_map.is_production_registry is False
    assert DEFAULT_SOURCE_MAP_PATH != DEFAULT_SOURCE_REGISTRY
    assert DEFAULT_SOURCE_MAP_PATH != PRODUCTION_SOURCE_REGISTRY
    assert source_map.production_registry_path == "config/sources.yaml"

    # The declared universe is far larger than what is collected, which is the
    # whole point: the map is the denominator, not the catalogue.
    configured = load_source_registry(PRODUCTION_SOURCE_REGISTRY)
    assert len(source_map.sources) > len(configured)
    assert len(source_map.active_sources) < len(source_map.sources)


def test_a_map_declaring_itself_a_production_registry_is_rejected() -> None:
    document = source_map_document()
    document["is_production_registry"] = True
    with pytest.raises(SourceMapValidationError, match="not a production source registry"):
        parse_source_map(document)


def test_the_production_catalogue_holds_only_sources_a_phase_really_added() -> None:
    """The catalogue grows only when a phase actually integrates a collector.

    It was Phase 2's three rows through 7C.1-7C.4A, which activated nothing.
    Phase 7C.4B added Stagiaires.ma and 7C.5B added Stage.ma, each with a real
    collector behind it. The list is pinned so a source cannot appear here
    without a phase claiming it.
    """
    configured = load_source_registry(PRODUCTION_SOURCE_REGISTRY)

    assert {source.id for source in configured} == {
        "scale_ai_greenhouse",
        "artefact_greenhouse",
        "linkedin_job_alert_email",
        "stagiaires_ma",
        "stage_ma",
    }
    assert {source.type for source in configured} == {
        "greenhouse",
        "gmail_linkedin_alert",
        "stagiaires_sitemap",
        "stage_ma_html",
    }


def test_no_benchmark_row_is_an_operational_opportunity() -> None:
    """The benchmark carries evidence, never a row of the operational database."""
    records = load_benchmark_records(DEFAULT_BENCHMARK_PATH)

    assert records
    for record in records:
        assert record["historical_or_live"] in OBSERVATION_HORIZONS
        assert record["source_authority"] in SOURCE_AUTHORITIES
        # Identity is the source's, so it survives a rebuilt database file.
        assert not str(record["benchmark_id"]).isdigit()


def test_not_selected_is_a_closed_integration_status() -> None:
    """A source can be real, reachable, and still deliberately not chosen.

    Without this member, "we evaluated it and said no" would have to be spelled
    as NEEDS_VERIFICATION or CANDIDATE, both of which claim the decision is
    still open. It carries no legal meaning whatsoever.
    """
    assert "NOT_SELECTED" in INTEGRATION_STATUSES


def test_rekrute_records_the_evaluated_but_not_selected_decision() -> None:
    entry = {
        item.id: item for item in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
    }["rekrute"]

    assert entry.integration_status == "NOT_SELECTED"
    assert entry.priority == "P2"
    assert entry.coverage_role == "AUDIT"
    assert entry.collection_strategy == "MANUAL_BENCHMARK"
    assert entry.production_source_id is None
    assert entry.country == BENCHMARK_COUNTRY_CODE
    assert entry.live_canary is False
    # The domain and its public robots/terms URLs were reached for real, so the
    # homepage is no longer merely written down from public knowledge.
    assert entry.homepage_url == "https://www.rekrute.com"
    assert entry.homepage_url_status == "EVIDENCED"


def test_the_rekrute_note_records_the_decision_without_a_legal_claim() -> None:
    """The note must not turn "we chose not to" into "we are not allowed to"."""
    entry = {
        item.id: item for item in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
    }["rekrute"]
    assert entry.notes is not None
    note = entry.notes.lower()

    assert "not selected" in note
    assert "403" in note
    assert "no bypass" in note or "bypass" in note
    for forbidden in ("forbids", "prohibits scraping", "illegal", "not allowed to"):
        assert forbidden not in note


def test_a_not_selected_source_cannot_claim_production_activity() -> None:
    """NOT_SELECTED is subject to the same rule as every other non-ACTIVE status."""
    document = source_map_document()
    for entry in document["sources"]:
        if entry["id"] == "rekrute":
            entry["production_source_id"] = "rekrute"

    with pytest.raises(SourceMapValidationError, match="only an ACTIVE source"):
        parse_source_map(document)


def test_every_active_entry_names_the_production_row_it_really_runs() -> None:
    """Two ACTIVE entries since Phase 7C.4B, each pointing at a real row.

    LinkedIn was alone until Stagiaires.ma was validated end to end. What has
    not changed is the rule underneath: ACTIVE means a configured, enabled,
    active `config/sources.yaml` row exists and is named here.
    """
    source_map = load_source_map(DEFAULT_SOURCE_MAP_PATH)
    active = {entry.id: entry for entry in source_map.active_sources}

    assert set(active) == {"linkedin_job_alert_email", "stagiaires_ma"}
    assert active["linkedin_job_alert_email"].production_source_id == (
        "linkedin_job_alert_email"
    )
    assert active["stagiaires_ma"].production_source_id == "stagiaires_ma"


def test_the_source_map_still_validates_with_unique_ids() -> None:
    sources = load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
    identifiers = [entry.id for entry in sources]

    assert len(identifiers) == len(set(identifiers))
    assert "rekrute" in identifiers


@pytest.mark.parametrize("strategy", sorted(FUTURE_STRATEGIES))
def test_a_not_selected_source_cannot_claim_a_future_strategy(strategy: str) -> None:
    """Declined and planned are contradictory claims about the same source."""
    document = source_map_document()
    for entry in document["sources"]:
        if entry["id"] == "rekrute":
            entry["collection_strategy"] = strategy

    with pytest.raises(SourceMapValidationError, match="NOT_SELECTED"):
        parse_source_map(document)


def test_the_committed_not_selected_entry_uses_a_present_tense_strategy() -> None:
    """MANUAL_BENCHMARK describes how ReKrute is read today, not a plan."""
    entry = {
        item.id: item for item in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
    }["rekrute"]

    assert entry.integration_status == "NOT_SELECTED"
    assert entry.collection_strategy == "MANUAL_BENCHMARK"
    assert entry.collection_strategy not in FUTURE_STRATEGIES


def test_future_strategies_remain_valid_for_undecided_sources() -> None:
    """The invariant is narrow: it constrains NOT_SELECTED and nothing else."""
    document = source_map_document()
    for entry in document["sources"]:
        if entry["id"] == "rekrute":
            entry["integration_status"] = "CANDIDATE"
            entry["collection_strategy"] = "FUTURE_COLLECTOR"

    source_map = parse_source_map(document)
    rekrute = {item.id: item for item in source_map.sources}["rekrute"]
    assert rekrute.collection_strategy == "FUTURE_COLLECTOR"


# ------------------------------------------- 7C.4A — Stagiaires.ma audit -----


def test_stagiaires_is_active_and_points_at_its_production_row() -> None:
    """Activated by real local validation, not by the collector existing.

    7C.4A asserted the opposite, and correctly: an audit that promoted its own
    subject would have been evidence of nothing. What moved this entry was the
    user's local run — a successful production dry-run, then a fresh-database
    double run that created 25 opportunities and then updated the same 25
    without recreating any of them.
    """
    entry = {
        item.id: item for item in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
    }["stagiaires_ma"]

    assert entry.integration_status == "ACTIVE"
    assert entry.collection_strategy == "EXISTING_COLLECTOR"
    assert entry.production_source_id == "stagiaires_ma"
    # Unchanged by activation: it is still the P1 internship board it was.
    assert entry.priority == "P1"
    assert entry.coverage_role == "PRIMARY"
    assert entry.source_class == "INTERNSHIP_BOARD"
    assert entry.country == BENCHMARK_COUNTRY_CODE
    # Still not a canary: it is a collected source, not a reachability probe.
    assert entry.live_canary is False


def test_the_active_stagiaires_entry_names_a_real_enabled_active_row() -> None:
    """The map's claim is checked against the operational catalogue itself."""
    source_map = load_source_map(DEFAULT_SOURCE_MAP_PATH)
    active = check_source_map_against_production_registry(
        source_map, PRODUCTION_SOURCE_REGISTRY
    )
    entry = {item.id: item for item in active}["stagiaires_ma"]

    configured = {
        source.id: source for source in load_source_registry(PRODUCTION_SOURCE_REGISTRY)
    }[entry.production_source_id]
    assert configured.type == "stagiaires_sitemap"
    assert configured.enabled is True
    assert configured.status == "active"
    assert configured.detail_page_limit == 25


def test_the_audited_stagiaires_homepage_is_recorded_as_evidenced() -> None:
    """EVIDENCED because a real request was made, not because the domain is known."""
    entry = {
        item.id: item for item in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
    }["stagiaires_ma"]

    assert entry.homepage_url == "https://www.stagiaires.ma"
    assert entry.homepage_url_status == "EVIDENCED"


def test_the_stagiaires_note_records_evidence_without_a_legal_or_date_claim() -> None:
    """The two claims the note must never make, and the two it must.

    A public HTTP 200 is reachability, not permission; and a sitemap `<lastmod>`
    is not a publication date. Both are easy to lose in a later edit, and both
    would be wrong in a way nobody would notice until the data was already
    corrupted or the project had asserted something it cannot support.
    """
    note = " ".join(
        {
            item.id: item for item in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
        }["stagiaires_ma"].notes.split()
    )

    # The claims that must survive every future edit of this entry.
    assert "NO legal claim" in note
    assert "reachability, not permission" in note
    assert "NOT a publication date" in note
    assert "CANDIDATE source_external_id" in note
    assert "no bypass" in note.lower()
    assert "NOT treated as evidence of a more recent publication" in note

    # And the claims that must no longer appear: the entry collects now, so a
    # note still saying it does not would be the map lying about production.
    for stale in (
        "nothing collects it",
        "No collector",
        "no production parser",
        "no config/sources.yaml row",
        "no SourceConfig type",
        "no factory registration",
        "no RadarAgent run",
        "planned next step is Phase 7C.4B",
    ):
        assert stale not in note, stale


def test_the_stagiaires_production_source_is_configured_and_bounded() -> None:
    """Phase 7C.4B's row, pinned to the values the architecture approved.

    7C.4A asserted the opposite — that no such row existed — because an audit
    that quietly activated its subject would have been an audit of nothing. That
    invariant belonged to that phase; this one records what replaced it, and
    keeps the bound from being widened without a test failing.
    """
    configured = {
        source.id: source for source in load_source_registry(PRODUCTION_SOURCE_REGISTRY)
    }
    stagiaires = configured["stagiaires_ma"]

    assert stagiaires.type == "stagiaires_sitemap"
    assert stagiaires.enabled is True
    assert stagiaires.country == "MA"
    assert stagiaires.detail_page_limit == 25
    assert stagiaires.frequency_minutes == 360


def test_the_source_map_still_records_stagiaires_honestly() -> None:
    """Code existing is not evidence that it works.

    The map moves to ACTIVE only on a real dry-run and SQLite double-run, not
    because a collector was written. Until that evidence exists it stays a
    CANDIDATE with no production identity — which is also why the validator's
    "only an ACTIVE entry may name a production_source_id" rule still holds.
    """
    entry = {
        item.id: item for item in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
    }["stagiaires_ma"]

    assert entry.integration_status in {"CANDIDATE", "ACTIVE"}
    if entry.integration_status == "CANDIDATE":
        assert entry.production_source_id is None


def test_no_entry_claims_production_identity_without_being_active() -> None:
    """Activation is earned one source at a time, and only with evidence.

    Two entries are ACTIVE. Every other entry in the map must still carry a null
    `production_source_id`, so a source cannot drift into looking operational
    without the ACTIVE status — and the validated review — that goes with it.
    """
    source_map = load_source_map(DEFAULT_SOURCE_MAP_PATH)

    activated = {entry.id for entry in source_map.active_sources}
    assert activated == {"linkedin_job_alert_email", "stagiaires_ma"}

    for entry in source_map.sources:
        if entry.id in activated:
            assert entry.production_source_id is not None
        else:
            assert entry.production_source_id is None, entry.id
            assert entry.integration_status != "ACTIVE", entry.id


# ------------------------------- 7C.5A / 7C.5B — Stage.ma source evidence -----


def stage_ma_entry():
    return {
        item.id: item for item in load_source_map(DEFAULT_SOURCE_MAP_PATH).sources
    }["stage_ma"]


def stage_ma_note() -> str:
    return " ".join(stage_ma_entry().notes.split())


def test_stage_ma_stays_non_production_after_its_audit() -> None:
    """A completed audit is evidence about a source, and so is a completed run.

    7C.5A reached the site, discovered real offer URLs and sampled real detail
    pages. 7C.5B then built a collector and validated it live. Neither makes
    Stage.ma collected: the live run found ten offers and every one was expired,
    so the activation gate was not met and these four fields are exactly what
    they were before either phase.
    """
    entry = stage_ma_entry()

    assert entry.integration_status == "CANDIDATE"
    assert entry.collection_strategy == "FUTURE_COLLECTOR"
    assert entry.production_source_id is None
    assert entry.live_canary is False
    assert entry.priority == "P1"
    assert entry.coverage_role == "PRIMARY"
    assert entry.country == BENCHMARK_COUNTRY_CODE


def test_the_stage_ma_note_records_the_completed_live_audit() -> None:
    note = stage_ma_note()

    assert "7C.5A" in note
    assert "COMPLETED with exit 0" in note
    assert "PUBLIC_HTML_CANDIDATE" in note


def test_the_stage_ma_note_records_where_discovery_actually_worked() -> None:
    """The specialty surface carried it; the generic listing did not.

    Recording only "discovery worked" would let a later phase build on
    /offres-stage, which returned 200 and exposed nothing.
    """
    note = stage_ma_note()

    assert "/specialites/computer-science exposed 10 real" in note
    assert "ZERO offer detail URLs in ordinary server HTML" in note
    assert "/offres-stage is NOT established as a usable discovery surface" in note


def test_the_stage_ma_note_records_that_no_sitemap_was_declared_or_guessed() -> None:
    note = stage_ma_note()

    assert "declared NO Sitemap" in note
    assert "No sitemap filename was guessed" in note


def test_the_stage_ma_note_does_not_overclaim_field_completeness() -> None:
    """One of three pages carried an organization. The note has to say so.

    "JobPosting is present" is true and would read as "the fields are there";
    the gap between those two is what a later parser would fall into.
    """
    note = stage_ma_note()

    assert "only ONE of the three exposed a usable organization" in note
    assert "NOT proven" in note
    assert "all Stage.ma detail pages" not in note
    assert "straightforward" not in note


def test_the_stage_ma_note_does_not_generalize_from_the_sample() -> None:
    note = stage_ma_note()

    # Expired pages in a three-page sample say nothing about the whole source.
    assert "says nothing about whether the source currently carries active" in note
    assert "nothing here claims the other specialties behave the same way" in note


def test_the_stage_ma_note_keeps_the_date_and_identity_boundaries() -> None:
    note = stage_ma_note()

    assert "persisted nothing and asserted no published_at" in note
    assert "CANDIDATE source_external_id" in note
    assert "never read as recency" in note
    assert "does not retire the audit's sentinel protection" in note


def test_the_stage_ma_note_claims_no_application_url() -> None:
    note = stage_ma_note()

    assert "no usable href was established" in note
    assert "no application_url is claimed" in note


def test_the_stage_ma_note_keeps_the_legal_boundary() -> None:
    """Reachability is not permission, and this note may never say otherwise."""
    note = stage_ma_note()

    assert "NO legal claim in either direction" in note
    assert "reachability, not permission" in note
    assert "remain a question for a human" in note


def test_the_stage_ma_note_records_the_real_7c5b_production_dry_run() -> None:
    """The run that decided this: a real local, bounded, GET-only dry-run at a
    named collector SHA, so the evidence can be traced back to code."""
    note = stage_ma_note()

    assert "PHASE 7C.5B, real local production evidence" in note
    assert "2cbb703c7eb769e927667915bb237ac9de019903" in note
    assert "COMPLETED successfully with exit 0" in note
    assert "items_found was 0 and items_returned was 0" in note
    assert "/specialites/computer-science" in note


def test_the_stage_ma_note_explains_the_zero_rather_than_just_stating_it() -> None:
    """Ten offers read, ten expired, none admissible.

    Without the explanation a future reader has no way to tell this zero from a
    broken collector, and "0 opportunities" is exactly the shape a silent
    structural failure takes.
    """
    note = stage_ma_note()

    assert "Ten live-discovered offer detail pages were actually read" in note
    assert "all TEN were explicitly EXPIRED" in note
    assert "ZERO admissible candidates remained" in note
    assert "pages_checked was 12 and parser_version was stage-ma-html-v1" in note
    assert "no parser structural failure" in note
    assert "no candidate was invented" in note


def test_the_stage_ma_note_does_not_call_the_expired_rows_opportunities() -> None:
    """Ten discovered listing entries is not ten current opportunities."""
    note = stage_ma_note()

    assert "not ten current opportunities" in note


def test_the_stage_ma_note_says_why_sqlite_was_not_run() -> None:
    """Not run is not the same as failed, and a zero-row double-run would prove
    nothing about idempotence."""
    note = stage_ma_note()

    assert "No SQLite double-run was performed" in note
    assert "N=0" in note
    assert "must never be presented as activation evidence" in note


def test_the_stage_ma_note_records_the_dormant_operational_config() -> None:
    """Dormant, not deleted: manual runs stay possible, automatic ones do not."""
    note = stage_ma_note()

    assert "intentionally DORMANT" in note
    assert "enabled: true" in note
    assert "status: candidate" in note
    assert "leaves it out of every automatic run" in note


def test_the_stage_ma_note_states_what_a_future_activation_requires() -> None:
    note = stage_ma_note()

    assert "at least ONE admissible current opportunity" in note
    assert "no historical or expired offer may be used to satisfy that gate" in note
    assert "code existing has never been evidence that a source works" in note


def test_the_stage_ma_note_records_the_collector_without_claiming_activation() -> None:
    """The collector exists — saying otherwise is now false — and Stage.ma is
    still not ACTIVE. Both halves have to survive together."""
    note = stage_ma_note()

    assert "collector now EXISTS" in note
    assert "no collector exists" not in note
    assert "remains non-production" in note
    assert "Nothing here claims Stage.ma is ACTIVE" in note
    for overclaim in (
        "integration_status: ACTIVE",
        "is now ACTIVE",
        "an ACTIVE production source",
        "activation succeeded",
        "SQLite double-run passed",
    ):
        assert overclaim not in note


def test_the_stage_ma_note_still_refuses_to_generalize_to_the_whole_site() -> None:
    """One specialty surface was audited and collected from. That is not
    Stage.ma."""
    note = stage_ma_note()

    assert "represents Stage.ma as a whole" in note
    assert "comprehensive" not in note


def test_stage_ma_is_still_not_an_active_production_mapped_source() -> None:
    source_map = load_source_map(DEFAULT_SOURCE_MAP_PATH)

    assert "stage_ma" not in {entry.id for entry in source_map.active_sources}
    assert {entry.id for entry in source_map.active_sources} == {
        "linkedin_job_alert_email",
        "stagiaires_ma",
    }

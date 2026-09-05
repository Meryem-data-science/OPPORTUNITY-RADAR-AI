"""Offline tests for the ReKrute parsing foundation (Phase 7C.3A).

Every test here is network-free and every value is a tiny synthetic test-only
fixture. Nothing in this file is a real ReKrute offer, and nothing in it may be
read as evidence about ReKrute's live markup: 7C.3A never reached the site, so
these tests pin the *rules this repository chose*, not the site's contract.
"""

from __future__ import annotations

import pytest

from services.collector.models.opportunity import OpportunityCandidate
from services.collector.parsers.rekrute import (
    REKRUTE_SOURCE_ID,
    UNBRIDGED_RECORD_FIELDS,
    RekruteOfferRecord,
    RekruteParseError,
    assess_target_evidence,
    canonical_offer_url,
    is_rekrute_url,
    to_opportunity_candidate,
)

# Synthetic, test-only. Not a real offer, not a benchmark row.
_OFFER_URL = "https://www.rekrute.com/offre/12345"


def _record(**overrides: object) -> RekruteOfferRecord:
    fields: dict[str, object] = {
        "external_id": "12345",
        "title": "Ingenieur",
        "organization": "Org",
        "source_url": _OFFER_URL,
    }
    fields.update(overrides)
    return RekruteOfferRecord.from_fields(**fields)  # type: ignore[arg-type]


class TestCanonicalUrl:
    def test_normalizes_scheme_host_and_drops_fragment(self) -> None:
        assert canonical_offer_url("http://rekrute.com/offre/1#apply") == (
            "https://www.rekrute.com/offre/1"
        )

    def test_is_deterministic_and_order_independent(self) -> None:
        first = canonical_offer_url("https://www.rekrute.com/o?b=2&a=1")
        second = canonical_offer_url("https://www.rekrute.com/o?a=1&b=2")
        assert first == second == "https://www.rekrute.com/o?a=1&b=2"

    def test_drops_only_universal_tracking_parameters(self) -> None:
        # `id` is preserved: 7C.3A never verified where the offer id lives, so
        # discarding an unrecognised parameter could destroy offer identity.
        assert canonical_offer_url(
            "https://www.rekrute.com/o?id=9&utm_source=x&gclid=y"
        ) == "https://www.rekrute.com/o?id=9"

    @pytest.mark.parametrize(
        "url",
        [
            "ftp://www.rekrute.com/offre/1",
            "javascript:alert(1)",
            "https://www.example.com/offre/1",
            "https://rekrute.com.evil.test/offre/1",
            "https://notrekrute.com/offre/1",
            "https://www.rekrute.com/of<fre>/1",
            "",
            None,
        ],
    )
    def test_rejects_non_http_and_non_rekrute_urls(self, url: str | None) -> None:
        assert is_rekrute_url(url) is False
        with pytest.raises(RekruteParseError):
            canonical_offer_url(url)


class TestRecordIdentity:
    @pytest.mark.parametrize("missing", ["external_id", "title", "organization"])
    def test_missing_required_identity_is_an_explicit_failure(self, missing: str) -> None:
        with pytest.raises(RekruteParseError, match=missing):
            _record(**{missing: "   "})

    def test_optional_fields_default_to_none_and_are_never_invented(self) -> None:
        record = _record()
        assert record.location is None
        assert record.description is None
        assert record.published_at is None
        assert record.deadline is None
        assert record.contract_type is None
        assert record.application_url is None

    def test_whitespace_is_normalized_without_altering_facts(self) -> None:
        record = _record(title="  Ingenieur   Data  ", location=" Casablanca ")
        assert record.title == "Ingenieur Data"
        assert record.location == "Casablanca"

    def test_application_url_is_dropped_when_not_genuinely_distinct(self) -> None:
        assert _record(application_url=_OFFER_URL + "#apply").application_url is None

    def test_application_url_is_kept_when_distinct(self) -> None:
        record = _record(application_url="https://www.rekrute.com/postuler/12345")
        assert record.application_url == "https://www.rekrute.com/postuler/12345"

    def test_source_url_must_be_a_rekrute_url(self) -> None:
        with pytest.raises(RekruteParseError):
            _record(source_url="https://www.example.com/offre/1")


class TestTargetEvidence:
    def test_explicit_stage_contract_field_is_target_evidence(self) -> None:
        evidence = assess_target_evidence(_record(contract_type="Stage"))
        assert evidence.is_target is True
        assert "stage" in evidence.contract_signals

    def test_explicit_pfe_in_title_is_target_evidence(self) -> None:
        evidence = assess_target_evidence(
            _record(title="PFE - Ingenieur", contract_type="CDI")
        )
        assert evidence.is_target is True
        assert "pfe" in evidence.pfe_signals

    def test_accented_projet_de_fin_detudes_in_text_is_target_evidence(self) -> None:
        evidence = assess_target_evidence(
            _record(description="Projet de Fin d'Études encadre.")
        )
        assert evidence.is_target is True
        assert "projet de fin d etudes" in evidence.pfe_signals

    def test_cdi_with_incidental_stage_wording_is_not_target_evidence(self) -> None:
        # The locked rule: a CDI that merely mentions a past internship is a
        # CDI. This is the exact false positive 7C.3A must not produce.
        evidence = assess_target_evidence(
            _record(
                title="Ingenieur",
                contract_type="CDI",
                description="Une premiere experience ou un stage est apprecie.",
            )
        )
        assert evidence.is_target is False
        assert evidence.contract_signals == ()
        assert evidence.pfe_signals == ()

    def test_incidental_stage_wording_without_any_contract_field_is_rejected(self) -> None:
        evidence = assess_target_evidence(
            _record(description="Un stage anterieur serait un plus.")
        )
        assert evidence.is_target is False

    def test_non_target_offer_explains_itself(self) -> None:
        evidence = assess_target_evidence(_record(contract_type="CDI"))
        assert evidence.is_target is False
        assert evidence.reasons
        assert any("not sufficient" in reason for reason in evidence.reasons)


class TestSharedModelBridge:
    def test_converts_only_fields_the_shared_model_supports(self) -> None:
        record = _record(
            location="Casablanca",
            description="Texte.",
            published_at="2026-01-05",
            deadline="2026-02-05",
            contract_type="Stage",
        )
        candidate = to_opportunity_candidate(record)
        assert isinstance(candidate, OpportunityCandidate)
        assert candidate.source_id == REKRUTE_SOURCE_ID
        assert candidate.source_external_id == "12345"
        assert candidate.canonical_url == _OFFER_URL
        assert candidate.published_at == "2026-01-05"

    def test_deadline_and_contract_survive_on_the_record(self) -> None:
        record = _record(deadline="2026-02-05", contract_type="Stage")
        assert record.deadline == "2026-02-05"
        assert record.contract_type == "Stage"
        assert set(UNBRIDGED_RECORD_FIELDS) == {"deadline", "contract_type"}

    def test_unbridged_fields_are_not_smuggled_into_another_column(self) -> None:
        candidate = to_opportunity_candidate(
            _record(deadline="2026-02-05", contract_type="Stage", description="Texte.")
        )
        serialized = " ".join(str(value) for value in vars(candidate).values())
        assert "2026-02-05" not in serialized


class TestNoProductionActivation:
    """7C.3A adds a parser and activates nothing. These pin that boundary."""

    def test_rekrute_is_absent_from_the_production_source_registry(self) -> None:
        from services.collector.sources import load_source_registry

        assert all(source.id != REKRUTE_SOURCE_ID for source in load_source_registry())

    def test_sourceconfig_still_refuses_a_rekrute_production_type(self) -> None:
        from services.collector.sources import SourceConfig, SourceConfigurationError

        with pytest.raises(SourceConfigurationError, match="unsupported type"):
            SourceConfig.from_mapping({"id": "rekrute", "type": "rekrute", "enabled": True})

    def test_collector_factory_has_no_rekrute_collector(self) -> None:
        from services.collector.collectors.factory import COLLECTOR_REGISTRY

        assert "rekrute" not in COLLECTOR_REGISTRY

    def test_parser_module_imports_no_database_or_network_module(self) -> None:
        import services.collector.parsers.rekrute as module

        source = module.__file__
        assert source is not None
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        for forbidden in ("import httpx", "import sqlite3", "import requests", "libsql"):
            assert forbidden not in text

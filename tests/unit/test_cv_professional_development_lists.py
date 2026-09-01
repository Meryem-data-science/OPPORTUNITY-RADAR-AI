"""Synthetic adversarial tests for labelled professional-development lists."""
from services.digital_twin.cv.candidates.labelled_lists import (
    certification_items,
    scan_certification_lists,
)
from services.digital_twin.cv.candidates.layout import LAYOUT_CONTINUATION_SECTION_TYPES
from services.digital_twin.cv.sections import SourceLine
from services.digital_twin.cv.models import SectionType


def test_wrapped_item_keeps_physical_line_break_after_layout_has_grouped_it() -> None:
    block = (
        SourceLine("Planned certifications: Alpha Certificate", 1),
        SourceLine("Advanced Track", 1),
    )
    assert certification_items(block) == (
        "planned", ("Alpha Certificate\nAdvanced Track",)
    )


def test_separate_logical_blocks_are_never_semantically_joined() -> None:
    first = (SourceLine("Planned certifications: Alpha Certificate", 1),)
    second = (SourceLine("Unrelated workshop", 1),)
    assert certification_items(first) == ("planned", ("Alpha Certificate",))
    assert certification_items(second) is None


def test_spacing_not_typography_is_enabled_for_professional_development() -> None:
    assert SectionType.PROFESSIONAL_DEVELOPMENT in LAYOUT_CONTINUATION_SECTION_TYPES


def test_scanner_reconstructs_a_punctuation_proven_preparing_wrap() -> None:
    body = (
        SourceLine("Currently preparing: Certificate Alpha; Certified Beta", 1),
        SourceLine("Advanced Track; Certificate Gamma.", 2),
    )
    items = scan_certification_lists(body)
    assert [(item.meaning, item.text, item.page_numbers) for item in items] == [
        ("preparing", "Certificate Alpha", (1, 2)),
        ("preparing", "Certified Beta\nAdvanced Track", (1, 2)),
        ("preparing", "Certificate Gamma.", (1, 2)),
    ]


def test_scanner_applies_preparing_evidence_after_reconstruction() -> None:
    body = (
        SourceLine("Currently preparing: Alpha topic; Advanced", 1),
        SourceLine("Certificate Track; SQL.", 1),
    )
    assert [item.text for item in scan_certification_lists(body)] == [
        "Advanced\nCertificate Track"
    ]


def test_scanner_does_not_infer_evidence_or_continuation_from_newline() -> None:
    assert scan_certification_lists(
        (SourceLine("Currently preparing: Python; SQL", 1),)
    ) == ()
    items = scan_certification_lists(
        (
            SourceLine("Currently preparing: Certificate Alpha; Python", 1),
            SourceLine("SQL; Rust.", 1),
        )
    )
    assert [item.text for item in items] == ["Certificate Alpha"]


def test_language_label_and_blank_line_stop_certification_carry() -> None:
    language = (
        SourceLine("Currently preparing: Certificate Alpha; Certified Beta", 1),
        SourceLine("Languages: Alder; Birch", 1),
    )
    separated = (
        SourceLine("Currently preparing: Certificate Alpha; Certified Beta", 1),
        SourceLine("", 1),
        SourceLine("Advanced Track; Certificate Gamma.", 1),
    )
    assert [item.text for item in scan_certification_lists(language)] == [
        "Certificate Alpha", "Certified Beta"
    ]
    assert [item.text for item in scan_certification_lists(separated)] == [
        "Certificate Alpha", "Certified Beta"
    ]


def test_new_certification_label_stops_carry_and_starts_its_own_list() -> None:
    body = (
        SourceLine("Currently preparing: Certificate Alpha; Certified Beta", 1),
        SourceLine("Planned certifications: Delta Track; Epsilon Track.", 1),
    )
    assert [(item.meaning, item.text) for item in scan_certification_lists(body)] == [
        ("preparing", "Certificate Alpha"),
        ("preparing", "Certified Beta"),
        ("planned", "Delta Track"),
        ("planned", "Epsilon Track."),
    ]


def test_completed_list_does_not_swallow_adjacent_prose_or_certification() -> None:
    body = (
        SourceLine("Planned certifications: Alpha Track; Beta Track.", 1),
        SourceLine("Peer mentoring; community workshops.", 1),
        SourceLine("Certificate Independent", 1),
    )
    assert [item.text for item in scan_certification_lists(body)] == [
        "Alpha Track", "Beta Track."
    ]

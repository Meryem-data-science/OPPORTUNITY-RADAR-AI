"""Synthetic adversarial tests for labelled professional-development lists."""
from services.digital_twin.cv.candidates.labelled_lists import certification_items
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

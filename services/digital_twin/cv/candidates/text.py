"""Small deterministic string helpers shared by the 3.2B extraction rules.

Each helper folds a fixed, documented list of variations away so a rule can be
written once and still match the way a CV happens to write the same label.
None of them changes what a value *is*: the folded forms exist only to compare
and to deduplicate, and every candidate keeps the source text untouched.
"""

from __future__ import annotations

import unicodedata

#: Characters a CV uses to open a list item. A line starting with one of them
#: is an item marker, never part of the item's meaning.
BULLET_MARKERS = "-–—*•·▪◦‣⁃>"
#: Decoration to trim off both ends of a fragment cut out of a list line.
FRAGMENT_EDGE = " \t" + BULLET_MARKERS


def fold_words(value: str) -> str:
    """Return the word form used to look a closed vocabulary up in a line.

    Accents, case and every non-alphanumeric character are folded to a space,
    so "Tél. :", "TEL", and "téléphone" all reduce to comparable words. Only
    the closed label and keyword dictionaries of this package are looked up in
    this form; no candidate value is ever built from it.
    """
    decomposed = unicodedata.normalize("NFKD", value)
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    spelled = "".join(
        character if character.isalnum() else " "
        for character in without_accents.casefold()
    )
    return " ".join(spelled.split())


def holds_phrase(folded_text: str, phrase: str) -> bool:
    """Say whether `phrase` appears in `folded_text` as whole words."""
    return f" {phrase} " in f" {folded_text} "


def comparison_form(value: str) -> str:
    """Return the form two candidate values are compared on for deduplication.

    Case and space runs are folded; nothing else is. Accents survive, because
    they distinguish real values, and punctuation survives, because it
    distinguishes real URLs.
    """
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def starts_with_bullet(text: str) -> bool:
    """Say whether the line opens with a list marker."""
    return bool(text) and text[0] in BULLET_MARKERS


def trim_fragment(value: str) -> str:
    """Trim list decoration and surrounding spaces off a cut-out fragment."""
    return value.strip(FRAGMENT_EDGE)

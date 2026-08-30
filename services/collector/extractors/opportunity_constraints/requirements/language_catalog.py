"""The closed registry of languages 3.5B can recognise, in code.

Like the skill catalogue, it seeds nothing: `migrations/0013` inserts no
language row, and a language reaches the database only when a posting was found
to demand it.

**A language is never inferred.** Not from the country a posting is in, not
from the city, not from the company's name, not from a nationality, and — the
one worth stating twice — not from the language the advertisement itself is
written in. An English-language posting for a role in Paris has required
neither English nor French. Every row this package produces comes from a
sentence that named a language and asked for it.

`language_key` is a stable ASCII key chosen here, not derived from the display
name: deriving it would make `français` and `francais` two keys and would make
the key depend on which spelling a posting happened to use. The key identifies
the language; `language_name` is what a reader sees.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

__all__ = [
    "LANGUAGE_CATALOG",
    "LANGUAGE_DEFINITIONS",
    "LanguageCatalogError",
    "LanguageTerm",
]


class LanguageCatalogError(RuntimeError):
    """Raised at import time when two registry entries collide."""


@dataclass(frozen=True)
class LanguageTerm:
    """One language, its stable key, and every spelling a posting may use."""

    language_key: str
    language_name: str
    aliases: tuple[str, ...]


#: The registry. Mandarin is its own entry rather than an alias of Chinese:
#: postings that write "Mandarin" are naming a spoken variety, and folding it
#: into "Chinese" would be this file deciding they meant the same thing.
LANGUAGE_DEFINITIONS: tuple[LanguageTerm, ...] = (
    LanguageTerm("english", "English", ("english", "anglais", "englisch")),
    LanguageTerm("french", "French", ("french", "français", "francais", "französisch")),
    LanguageTerm("arabic", "Arabic", ("arabic", "arabe", "arabophone")),
    LanguageTerm("spanish", "Spanish", ("spanish", "espagnol", "español", "castellano")),
    LanguageTerm("german", "German", ("german", "allemand", "deutsch")),
    LanguageTerm("italian", "Italian", ("italian", "italien", "italiano")),
    LanguageTerm("portuguese", "Portuguese", ("portuguese", "portugais", "português", "portugues")),
    LanguageTerm("dutch", "Dutch", ("dutch", "néerlandais", "neerlandais", "nederlands")),
    LanguageTerm("chinese", "Chinese", ("chinese", "chinois")),
    LanguageTerm("mandarin", "Mandarin", ("mandarin",)),
    LanguageTerm("japanese", "Japanese", ("japanese", "japonais")),
    LanguageTerm("korean", "Korean", ("korean", "coréen", "coreen")),
    LanguageTerm("russian", "Russian", ("russian", "russe")),
    LanguageTerm("turkish", "Turkish", ("turkish", "turc")),
    LanguageTerm("amazigh", "Amazigh", ("amazigh", "tamazight", "berbère", "berbere")),
    LanguageTerm("darija", "Darija", ("darija",)),
)


def _validate(definitions: tuple[LanguageTerm, ...]) -> tuple[LanguageTerm, ...]:
    """Refuse a duplicate key and a shared alias, loudly, at import time."""
    keys: set[str] = set()
    alias_owner: dict[str, str] = {}
    for definition in definitions:
        if definition.language_key in keys:
            raise LanguageCatalogError(
                f"language key {definition.language_key!r} is declared twice"
            )
        keys.add(definition.language_key)
        for alias in definition.aliases:
            folded = " ".join(
                unicodedata.normalize("NFKC", alias).split()
            ).casefold()
            owner = alias_owner.get(folded)
            if owner is not None and owner != definition.language_key:
                raise LanguageCatalogError(
                    f"alias {alias!r} is claimed by two languages"
                )
            alias_owner[folded] = definition.language_key
    return definitions


#: The registry, validated once at import time.
LANGUAGE_CATALOG: tuple[LanguageTerm, ...] = _validate(LANGUAGE_DEFINITIONS)

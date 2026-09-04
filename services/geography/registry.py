"""The closed, auditable registry the geographic resolver is allowed to use.

Everything this package knows about the world is written here, by hand, and
nothing else is consulted: **no network, no geocoding API, no model, no fuzzy
matching, no edit distance and no external dataset**. A country code that comes
out of the resolver can always be traced to one line of this file.

That is a deliberate limitation, not an unfinished one. A global geocoder would
answer far more strings, and it would answer them with a confidence nobody in
this project can audit, on a machine that is supposed to work offline. The
product looks for internships in **one** country; a registry that covers the
places the collected corpus actually names, and honestly answers UNKNOWN for
the rest, is the useful shape.

Three catalogues, three jobs:

`COUNTRY_ALIASES`   text an employer writes for a country -> ISO 3166-1 alpha-2.
`CITY_COUNTRIES`    the few cities allowed to imply a country on their own.
`AMBIGUOUS_REGIONS` text that names a real area but never one country.

**No bare two-letter alias is registered**, and that is the trap this file
exists to avoid: the corpus contains `San Francisco, CA` and `New York, NY`,
where `CA` is California and `MA` is Massachusetts — not Canada and not
Morocco. `UK`, `USA` and `UAE` are the exceptions, and each is three letters or
collides with no state code. A posting written `Casablanca, MA` therefore
resolves through the city, or not at all; it never resolves through `MA`.

The city catalogue is Moroccan and small on purpose. The product targets the
whole of Morocco with no city restriction, so a city is never a decision here —
it is only sometimes the one token that names the country, which is exactly
what `Casablanca` alone is. Adding `Paris -> FR` would be a different promise:
there is a Paris in Texas, and this file may not contain a guess.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = [
    "AMBIGUOUS_REGIONS",
    "CITY_COUNTRIES",
    "COUNTRY_ALIASES",
    "country_for_city",
    "country_for_text",
    "is_ambiguous_region",
    "normalize_geographic_text",
]

_PUNCTUATION = re.compile(r"[^0-9a-z]+")


def normalize_geographic_text(text: str) -> str:
    """The comparison form of a piece of location text.

    Accents are folded, case is dropped, periods are removed so `U.K.` and `UK`
    are one token, and every other punctuation mark becomes a space. The result
    is only ever compared for **equality** against the catalogues below: there
    is no substring match, no prefix match and no distance, so a normalisation
    that produced a surprising token can only ever fail to match, never match
    the wrong entry.
    """
    decomposed = unicodedata.normalize("NFKD", text)
    without_accents = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    lowered = without_accents.casefold().replace(".", "")
    return " ".join(_PUNCTUATION.sub(" ", lowered).split())


def _catalogue(entries: dict[str, tuple[str, ...]]) -> dict[str, str]:
    """Alias -> code, with every alias put through the resolver's normaliser.

    The aliases are written below the way a person writes them, and normalised
    once here, so the file stays readable and the lookup stays exact.
    """
    catalogue: dict[str, str] = {}
    for code, aliases in entries.items():
        for alias in aliases:
            key = normalize_geographic_text(alias)
            if not key:
                raise ValueError(f"alias {alias!r} normalises to nothing")
            if catalogue.get(key, code) != code:
                raise ValueError(
                    f"alias {alias!r} is claimed by {catalogue[key]} and {code}"
                )
            catalogue[key] = code
    return catalogue


#: Country name -> ISO 3166-1 alpha-2. French and English spellings side by
#: side, because the corpus holds both: a Moroccan posting says `Maroc` as often
#: as `Morocco`.
COUNTRY_ALIASES: dict[str, str] = _catalogue(
    {
        "MA": ("Maroc", "Morocco", "Marokko", "Kingdom of Morocco", "Royaume du Maroc"),
        "FR": ("France", "Francia", "French Republic"),
        "GB": (
            "UK", "United Kingdom", "Great Britain", "Grande-Bretagne",
            "Royaume-Uni", "England", "Angleterre", "Scotland", "Ecosse",
            "Wales", "Pays de Galles", "Northern Ireland",
        ),
        "US": (
            "US", "USA", "United States", "United States of America",
            "Etats-Unis", "États-Unis",
        ),
        "QA": ("Qatar",),
        "BE": ("Belgium", "Belgique", "Belgie", "België"),
        "AE": (
            "UAE", "United Arab Emirates", "Emirats arabes unis",
            "Émirats arabes unis",
        ),
        "DE": ("Germany", "Allemagne", "Deutschland"),
        "ES": ("Spain", "Espagne", "Espana", "España"),
        "IT": ("Italy", "Italie", "Italia"),
        "PT": ("Portugal",),
        "NL": ("Netherlands", "Pays-Bas", "Nederland", "Holland"),
        "LU": ("Luxembourg", "Luxemburg"),
        "CH": ("Switzerland", "Suisse", "Schweiz"),
        "IE": ("Ireland", "Irlande"),
        "AT": ("Austria", "Autriche"),
        "SE": ("Sweden", "Suede", "Suède"),
        "NO": ("Norway", "Norvege", "Norvège"),
        "DK": ("Denmark", "Danemark"),
        "FI": ("Finland", "Finlande"),
        "PL": ("Poland", "Pologne"),
        "CZ": ("Czechia", "Czech Republic", "Republique tcheque"),
        "RO": ("Romania", "Roumanie"),
        "GR": ("Greece", "Grece", "Grèce"),
        "TR": ("Turkey", "Turkiye", "Türkiye", "Turquie"),
        "CA": ("Canada",),
        "MX": ("Mexico", "Mexique"),
        "BR": ("Brazil", "Bresil", "Brésil"),
        "TN": ("Tunisia", "Tunisie"),
        "DZ": ("Algeria", "Algerie", "Algérie"),
        "EG": ("Egypt", "Egypte", "Égypte"),
        "SN": ("Senegal", "Sénégal"),
        "CI": ("Ivory Coast", "Cote d'Ivoire", "Côte d'Ivoire"),
        "NG": ("Nigeria",),
        "KE": ("Kenya",),
        "ZA": ("South Africa", "Afrique du Sud"),
        "SA": ("Saudi Arabia", "Arabie saoudite"),
        "IN": ("India", "Inde"),
        "SG": ("Singapore", "Singapour"),
        "JP": ("Japan", "Japon"),
        "CN": ("China", "Chine"),
        "AU": ("Australia", "Australie"),
        "NZ": ("New Zealand", "Nouvelle-Zelande", "Nouvelle-Zélande"),
    }
)

#: The cities allowed to name a country by themselves. Moroccan, explicit, and
#: chosen because the corpus writes them alone — `Casablanca` with no country
#: is an ordinary way to write a Moroccan posting's location.
CITY_COUNTRIES: dict[str, str] = _catalogue(
    {
        "MA": (
            "Casablanca", "Rabat", "Sale", "Salé", "Marrakech", "Marrakesh",
            "Tanger", "Tangier", "Tangiers", "Fes", "Fès", "Fez", "Meknes",
            "Meknès", "Agadir", "Oujda", "Kenitra", "Kénitra", "Tetouan",
            "Tétouan", "Mohammedia", "El Jadida", "Safi", "Beni Mellal",
            "Khouribga", "Settat", "Berrechid", "Temara", "Témara", "Nador",
            "Benguerir", "Ben Guerir", "Ifrane", "Essaouira", "Laayoune",
            "Laâyoune", "Dakhla", "Errachidia", "Ouarzazate",
        )
    }
)

#: Text that names a real geographic area but never a single country. These are
#: `AMBIGUOUS`, not `UNKNOWN`: the string did say something about where the work
#: is, and it said something no country code can carry.
AMBIGUOUS_REGIONS: frozenset[str] = frozenset(
    normalize_geographic_text(name)
    for name in (
        "APAC", "Asia Pacific", "Asia-Pacific", "EMEA", "LATAM", "MENA",
        "NORAM", "AMER", "EU", "Europe", "European Union", "Western Europe",
        "Eastern Europe", "Southern Europe", "Northern Europe", "Africa",
        "Afrique", "North Africa", "Afrique du Nord", "West Africa",
        "Sub-Saharan Africa", "Asia", "Asie", "South East Asia",
        "Southeast Asia", "South Asia", "East Asia", "Central Asia",
        "Middle East", "Moyen-Orient", "Gulf", "GCC", "North America",
        "Amerique du Nord", "South America", "Amerique du Sud",
        "Latin America", "Amerique latine", "Caribbean", "Oceania", "Nordics",
        "Benelux", "Maghreb", "Balkans", "Worldwide", "Global", "Globally",
        "International", "Anywhere", "Multiple Locations",
        "Various Locations", "Multiple Countries",
    )
)


def country_for_text(text: str) -> str | None:
    """The country an already-normalised token names, or None."""
    return COUNTRY_ALIASES.get(text)


def country_for_city(text: str) -> str | None:
    """The country an already-normalised city token implies, or None."""
    return CITY_COUNTRIES.get(text)


def is_ambiguous_region(text: str) -> bool:
    """Whether an already-normalised token names an area but not a country."""
    return text in AMBIGUOUS_REGIONS

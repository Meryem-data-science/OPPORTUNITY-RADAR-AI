"""Render one digest twice — plain text and HTML — from the same items.

The two bodies are not two designs. They carry exactly the same business
facts, in exactly the same order, and are generated from the same list in one
pass each, so a reader whose client refuses HTML loses styling and nothing
else.

The HTML half is deliberately inert. Every dynamic value is escaped, including
inside the ``href`` — a title or an organization is text an external source
wrote, and it is never allowed to become markup. There is no script, no
external stylesheet, no remote image, and no tracking pixel of any kind: the
message does not report that it was opened, because nothing in this product
needs to know that.

Nothing here reads a database or invents a value. A digest never shows a match
percentage, because no authoritative per-opportunity match percentage is
persisted for it to show.
"""

from __future__ import annotations

from collections.abc import Sequence
from html import escape

from services.portfolio import PortfolioBucket

from .models import BUCKET_ORDER, DIGEST_VERSION, DigestItem, GmailDigestError

#: Shown where the opportunity authority holds no location. It states an
#: absence rather than filling one in.
UNKNOWN_LOCATION_LABEL = "Lieu non précisé"

BUCKET_LABELS: dict[PortfolioBucket, str] = {
    PortfolioBucket.TARGET: "TARGET",
    PortfolioBucket.SAFE: "SAFE",
    PortfolioBucket.AMBITIOUS: "AMBITIOUS",
}


def render_subject(items: Sequence[DigestItem]) -> str:
    """Return the subject line: the one number that decides whether to open."""
    count = len(items)
    if count <= 0:
        raise GmailDigestError("a digest with no opportunity has no subject")
    plural = "s" if count > 1 else ""
    return f"Opportunity Radar — {count} opportunité{plural} à examiner"


def _location(item: DigestItem) -> str:
    return UNKNOWN_LOCATION_LABEL if item.location is None else item.location


def _grouped(
    items: Sequence[DigestItem],
) -> tuple[tuple[PortfolioBucket, tuple[DigestItem, ...]], ...]:
    """Group the already-ordered items by bucket, keeping their order.

    An empty bucket produces no group at all: a heading over nothing is noise,
    not information.
    """
    groups = []
    for bucket in BUCKET_ORDER:
        member = tuple(item for item in items if item.bucket is bucket)
        if member:
            groups.append((bucket, member))
    return tuple(groups)


def render_text(items: Sequence[DigestItem]) -> str:
    """Render the plain-text body: the same facts, in the same order."""
    if not items:
        raise GmailDigestError("a digest with no opportunity has no body")
    lines = [render_subject(items), ""]
    for bucket, members in _grouped(items):
        lines.append(f"{BUCKET_LABELS[bucket]} ({len(members)})")
        lines.append("")
        for item in members:
            lines.append(f"- {item.title} — {item.organization}")
            lines.append(f"  Lieu : {_location(item)}")
            lines.append(
                f"  Portfolio : {BUCKET_LABELS[item.bucket]}"
                f" | Priorité : {item.priority_category.value}"
                f" | Éligibilité : {item.eligibility_status.value}"
            )
            lines.append(f"  {item.url}")
            lines.append("")
    lines.append(
        "Ces opportunités proviennent du Portfolio audité ;"
        " aucune valeur n'a été recalculée pour cet e-mail."
    )
    return "\n".join(lines) + "\n"


def render_html(items: Sequence[DigestItem]) -> str:
    """Render the HTML body: escaped throughout, and inert by construction."""
    if not items:
        raise GmailDigestError("a digest with no opportunity has no body")
    subject = escape(render_subject(items))
    parts = [
        "<!DOCTYPE html>",
        '<html lang="fr">',
        "<head>",
        '<meta charset="utf-8">',
        f"<title>{subject}</title>",
        "</head>",
        "<body>",
        f"<h1>{subject}</h1>",
    ]
    for bucket, members in _grouped(items):
        parts.append(f"<h2>{escape(BUCKET_LABELS[bucket])} ({len(members)})</h2>")
        parts.append("<ul>")
        for item in members:
            parts.append("<li>")
            parts.append(
                f"<p><strong>{escape(item.title)}</strong>"
                f" — {escape(item.organization)}</p>"
            )
            parts.append(f"<p>Lieu : {escape(_location(item))}</p>")
            parts.append(
                f"<p>Portfolio : {escape(BUCKET_LABELS[item.bucket])}"
                f" | Priorité : {escape(item.priority_category.value)}"
                f" | Éligibilité : {escape(item.eligibility_status.value)}</p>"
            )
            # quote=True escapes the double quotes that delimit the attribute,
            # so a URL can never close it and open an attribute of its own.
            parts.append(
                f'<p><a href="{escape(item.url, quote=True)}">'
                f"{escape(item.url)}</a></p>"
            )
            parts.append("</li>")
        parts.append("</ul>")
    parts.append(
        "<p>Ces opportunités proviennent du Portfolio audité ;"
        " aucune valeur n'a été recalculée pour cet e-mail.</p>"
    )
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts) + "\n"


def render_digest(
    items: Sequence[DigestItem], *, digest_version: str = DIGEST_VERSION
) -> tuple[str, str, str]:
    """Return the subject, the text body and the HTML body of one digest."""
    if digest_version != DIGEST_VERSION:
        raise GmailDigestError(f"cannot render digest version {digest_version!r}")
    return render_subject(items), render_text(items), render_html(items)

"""Local CLI over the Phase 3.2B candidate extractor.

    python -m services.digital_twin.cv.candidates.cli extract /path/to/cv.pdf

The default output is privacy-safe by construction: it reports how many
candidates of each type were produced, by which rules, and with which warnings,
and never a candidate's text. That means no name, no email address, no phone
number, no URL, no employer, no school and no skill mention is printed.
`--json-out` is the one way to get the values themselves; it writes only where
the operator names a path, it overwrites nothing, and the file it creates is
readable by its owner alone.

The command reads one local PDF and returns. It opens no database connection,
writes no row, needs no environment variable, no network access and no API key,
and calls no model.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from pathlib import Path

from services.digital_twin.cv.candidates.extractor import extract_candidates
from services.digital_twin.cv.candidates.models import StructuredCvExtraction
from services.digital_twin.cv.export import (
    DETAILED_FILE_MODE,
    EXISTING_JSON_OUT_ERROR,
    write_detailed_json,
)
from services.digital_twin.cv.parser import parse_cv_pdf

EXTRACT_COMMAND = "extract"
JSON_OUT_NOTICE = (
    "wrote the detailed candidates, CV text included; keep that file out of "
    "the repository"
)
UNVERIFIED_NOTICE = (
    "these are unverified candidates read from one document, not facts about "
    "the person; nothing was stored"
)

__all__ = [
    "DETAILED_FILE_MODE",
    "EXISTING_JSON_OUT_ERROR",
    "EXTRACT_COMMAND",
    "JSON_OUT_NOTICE",
    "UNVERIFIED_NOTICE",
    "main",
    "parse_args",
]

# `DETAILED_FILE_MODE` and `EXISTING_JSON_OUT_ERROR` are re-exported above:
# this command makes the same export guarantees as the Phase 3.2A parser CLI,
# because it writes the same kind of file.


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract unverified structured candidates from a local CV PDF. "
            "Reads the file, writes no database row, and asserts no fact "
            "about the person."
        )
    )
    parser.add_argument("command", choices=(EXTRACT_COMMAND,))
    parser.add_argument("pdf_path", help="Path to a local PDF. Never printed back.")
    parser.add_argument(
        "--json-out",
        default=None,
        help=(
            "Write the detailed candidates, CV text included, to this new "
            "local path. Omit it to keep the run privacy-safe."
        ),
    )
    return parser.parse_args(argv)


def _report(result: StructuredCvExtraction) -> None:
    """Print the shape of the extraction. Reads no text out of any candidate."""
    print(f"extractor_version={result.extractor_version}")
    print(f"parser_version={result.parser_version}")
    print(f"sha256={result.cv_sha256}")
    print(f"candidates={len(result.candidates)}")
    for candidate_type, count in result.counts_by_type().items():
        print(f"  {candidate_type}={count}")
    rule_ids = sorted({candidate.rule_id.value for candidate in result.candidates})
    print(f"rules={','.join(rule_ids) if rule_ids else '(none)'}")
    print(f"warnings={len(result.warnings)}")
    for warning in result.warnings:
        print(f"  {warning.code.value}: {warning.message}")
    print(UNVERIFIED_NOTICE)


def main(argv: Sequence[str] | None = None) -> int:
    """Extract candidates from one local CV PDF. 0 on success, 1 on failure."""
    try:
        args = parse_args(argv)
        result = extract_candidates(parse_cv_pdf(args.pdf_path))
        if args.json_out is not None:
            write_detailed_json(result.as_dict(), Path(args.json_out))
    except Exception as error:
        # The message quotes the failure, never the path or the CV content.
        print(f"cv candidate extraction failed: {type(error).__name__}: {error}")
        return 1

    _report(result)
    if args.json_out is not None:
        print(JSON_OUT_NOTICE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

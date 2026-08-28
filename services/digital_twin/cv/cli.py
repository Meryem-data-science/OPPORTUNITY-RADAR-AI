"""Local CLI over the Phase 3.2A CV parser.

    python -m services.digital_twin.cv.cli parse /path/to/cv.pdf

The default output is privacy-safe by construction: it reports the shape of the
document — parser version, SHA-256, page count, canonical section types,
warnings — and never the CV text, a heading as written, a name, an email
address, a phone number or a postal address. `--json-out` is the one way to get
the full result, it writes only where the operator names a path, and it
overwrites nothing.
"""

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path

from services.digital_twin.cv.models import ParsedCv
from services.digital_twin.cv.parser import parse_cv_pdf

PARSE_COMMAND = "parse"
JSON_OUT_NOTICE = (
    "wrote the detailed result, CV text included; keep that file out of "
    "the repository"
)
EXISTING_JSON_OUT_ERROR = "the --json-out path already exists; it is never overwritten"
#: The detailed export holds the whole CV: owner read/write, nothing else.
DETAILED_FILE_MODE = 0o600
_EXCLUSIVE_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Extract and segment a local CV PDF. Reads the file, writes no "
            "database row, and asserts no fact about the person."
        )
    )
    parser.add_argument("command", choices=(PARSE_COMMAND,))
    parser.add_argument("pdf_path", help="Path to a local PDF. Never printed back.")
    parser.add_argument(
        "--json-out",
        default=None,
        help=(
            "Write the detailed result, CV text included, to this new local "
            "path. Omit it to keep the run privacy-safe."
        ),
    )
    return parser.parse_args(argv)


def _report(result: ParsedCv) -> None:
    """Print the shape of the document. Reads no text out of the CV."""
    print(f"parser_version={result.parser_version}")
    print(f"sha256={result.content_sha256}")
    print(f"pages={result.page_count}")
    empty_pages = [page.page_number for page in result.pages if page.is_empty]
    if empty_pages:
        print(f"empty_pages={','.join(str(number) for number in empty_pages)}")
    section_types = [section_type.value for section_type in result.section_types]
    print(f"sections={','.join(section_types) if section_types else '(none)'}")
    print(f"warnings={len(result.warnings)}")
    for warning in result.warnings:
        page = "" if warning.page_number is None else f" page={warning.page_number}"
        print(f"  {warning.code.value}{page}: {warning.message}")


def _write_json(result: ParsedCv, destination: Path) -> None:
    """Create the detailed export, or fail without touching what is there.

    `O_CREAT | O_EXCL` asks the kernel to create the file or refuse, atomically:
    there is no window between a check and a write in which an existing file
    could be truncated. The mode is `0o600` because this export holds the whole
    CV, so it must be readable by its owner alone; a umask can only remove
    further bits, never add group or other back. A write that fails after the
    file was created removes it, rather than leaving a partial CV on disk.
    """
    payload = json.dumps(result.as_dict(), ensure_ascii=False, indent=2, sort_keys=True)
    try:
        descriptor = os.open(destination, _EXCLUSIVE_CREATE_FLAGS, DETAILED_FILE_MODE)
    except FileExistsError as error:
        # Re-raised without the path: the operator named it, but it is not
        # echoed back, exactly like the CV path itself.
        raise FileExistsError(EXISTING_JSON_OUT_ERROR) from error
    except OSError as error:
        raise OSError(
            f"the detailed export could not be created: {type(error).__name__}"
        ) from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
    except OSError as error:
        destination.unlink(missing_ok=True)
        raise OSError(
            "the detailed export could not be written and was removed: "
            f"{type(error).__name__}"
        ) from error


def main(argv: Sequence[str] | None = None) -> int:
    """Parse one local CV PDF and report its shape. 0 on success, 1 on failure."""
    try:
        args = parse_args(argv)
        result = parse_cv_pdf(args.pdf_path)
        if args.json_out is not None:
            _write_json(result, Path(args.json_out))
    except Exception as error:
        # The message quotes the failure, never the path or the CV content.
        print(f"cv parse failed: {type(error).__name__}: {error}")
        return 1

    _report(result)
    if args.json_out is not None:
        print(JSON_OUT_NOTICE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

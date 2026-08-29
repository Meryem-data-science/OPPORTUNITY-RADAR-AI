"""The one way this package writes a detailed result to disk.

A detailed export holds the CV itself — its text in Phase 3.2A, the text every
candidate quotes in Phase 3.2B — so both slices write it under the same
guarantees rather than each restating them: the operator names the path, the
file is created or the write fails, an existing file is never overwritten, the
file is readable by its owner alone, and no path ever reaches an error message.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

EXISTING_JSON_OUT_ERROR = "the --json-out path already exists; it is never overwritten"
#: The detailed export holds the whole CV: owner read/write, nothing else.
DETAILED_FILE_MODE = 0o600
_EXCLUSIVE_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL


def write_detailed_json(payload: dict[str, object], destination: Path) -> None:
    """Create the detailed export, or fail without touching what is there.

    `O_CREAT | O_EXCL` asks the kernel to create the file or refuse, atomically:
    there is no window between a check and a write in which an existing file
    could be truncated. The mode is `0o600` because this export holds the whole
    CV, so it must be readable by its owner alone; a umask can only remove
    further bits, never add group or other back. A write that fails after the
    file was created removes it, rather than leaving a partial CV on disk.
    """
    document = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
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
            handle.write(document)
    except OSError as error:
        destination.unlink(missing_ok=True)
        raise OSError(
            "the detailed export could not be written and was removed: "
            f"{type(error).__name__}"
        ) from error

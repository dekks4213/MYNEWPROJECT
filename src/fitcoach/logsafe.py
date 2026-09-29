"""Privacy-preserving error logging: exception type and code location only.

Exception messages and tracebacks can contain user input (food text, names) or tokens, so
they are never written to logs.
"""

from __future__ import annotations

import logging
import traceback


def log_failure(log: logging.Logger, what: str, exc: BaseException) -> None:
    frames = traceback.extract_tb(exc.__traceback__)
    ours = [f for f in frames if "fitcoach" in f.filename] or frames
    where = f"{ours[-1].filename.rsplit('/', 1)[-1]}:{ours[-1].lineno}" if ours else "?"
    log.error("%s: %s at %s", what, type(exc).__name__, where)

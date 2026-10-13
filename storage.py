"""Crash-safe JSON persistence shared by main.py and browser_poller.py."""
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_json(path: Path, obj: Any, private: bool = False) -> None:
    """Write JSON to `path` via temp file + rename.

    A plain write_text() truncates first, so a crash or redeploy mid-write
    leaves an empty/half file — and history.json is the only copy of trades
    older than Bitget's 50-row page. os.replace() is atomic on POSIX, so
    readers see either the old file or the new one, never a partial.
    `private=True` restricts the file to the owner (credentials, cookies).
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(obj, f)
            f.flush()
            os.fsync(f.fileno())
        if private:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

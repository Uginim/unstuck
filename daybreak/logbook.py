"""Per-request JSONL with full content. Callers never pass tokens or signed links."""
import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock


class Logbook:
    def __init__(self, path: str | None):
        self._path = Path(path) if path else None
        self._lock = Lock()
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, event: str, **fields) -> None:
        if not self._path:
            return
        row = {"at": datetime.now(timezone.utc).isoformat(), "event": event, **fields}
        with self._lock, self._path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False, default=str) + "\n")

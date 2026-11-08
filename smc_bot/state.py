"""Persist completed-candle watermarks without storing API credentials."""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path


class State:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.streams: dict[str, str] = {}
        if self.path.exists():
            try:
                payload = json.loads(self.path.read_text(encoding="utf-8"))
                if payload.get("version") != 1 or not isinstance(payload.get("streams"), dict):
                    raise ValueError("Unsupported state format")
                for stream, stamp in payload["streams"].items():
                    if not isinstance(stream, str) or not isinstance(stamp, str):
                        raise ValueError("Invalid stream watermark")
                    parsed = datetime.fromisoformat(stamp)
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise ValueError("State watermark requires a timezone")
                self.streams = payload["streams"]
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f"Invalid state file {path}; restore it or choose a new --state path") from exc

    def last(self, stream: str) -> datetime | None:
        stamp = self.streams.get(stream)
        return datetime.fromisoformat(stamp).astimezone(timezone.utc) if stamp else None

    def advance(self, stream: str, time: datetime) -> None:
        previous = self.last(stream)
        if previous and time < previous:
            return
        streams = dict(self.streams)
        streams[stream] = time.astimezone(timezone.utc).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write in the destination directory so replacement stays on one filesystem.
        handle, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        try:
            with os.fdopen(handle, "w", encoding="utf-8") as file:
                json.dump({"version": 1, "streams": streams}, file, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, self.path)
            self.streams = streams
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

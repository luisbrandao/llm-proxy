"""In-process ring buffer of recent log records, for the `/admin/logs` view.

A `logging.Handler` that keeps the last N records in memory so the web UI can
tail them without a log file or docker-socket access. State is process-local —
exactly like the slot/health accounting in `slots.py`/`registry.py` — which is
correct under the single uvicorn worker this app runs as.

Each record gets a monotonically increasing sequence number so a poller can ask
for "everything since seq X" and never miss or re-fetch a line. The buffer is
bounded (`deque(maxlen=...)`) so memory stays flat under sustained logging; the
oldest lines simply drop off.
"""
import logging
from collections import deque
from datetime import datetime
from itertools import count

# Lines kept in memory. ~2000 covers a healthy scrollback for a live tail while
# staying small; tune if you need deeper history in the UI.
DEFAULT_CAPACITY = 2000


class RingBufferHandler(logging.Handler):
    """Keep the most recent formatted records in a bounded, sequence-stamped deque.

    `emit` runs under the handler's own lock (acquired by `logging.Handler.handle`),
    so appends are serialized. `entries` takes the same lock so a reader on the
    event loop never iterates the deque while a logging thread mutates it
    (uvicorn access logs and the reverse-DNS lookup can emit off the main thread).
    """

    def __init__(self, capacity: int = DEFAULT_CAPACITY):
        super().__init__()
        self._buf = deque(maxlen=capacity)
        self._seq = count(1)
        # Message-only: we carry level/ts as separate fields, but routing the
        # record through a Formatter still appends any exception traceback.
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
        except Exception:  # noqa: BLE001 - logging must never crash the caller
            self.handleError(record)
            return
        # Local-time ISO-8601 with offset, matching main._LocalTimeFormatter so
        # the UI's timestamps line up with the stdout/`docker logs` stream.
        ts = datetime.fromtimestamp(record.created).astimezone().isoformat(timespec="seconds")
        entry = {
            "seq": next(self._seq),
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "msg": msg,
        }
        self.acquire()
        try:
            self._buf.append(entry)
        finally:
            self.release()

    def entries(self, since: int = 0):
        """Buffered records with `seq > since`, oldest first."""
        self.acquire()
        try:
            return [e for e in self._buf if e["seq"] > since]
        finally:
            self.release()


# Module-level singleton: attached to the relevant loggers in `app.main` and read
# by the `/admin/logs` endpoint.
handler = RingBufferHandler()

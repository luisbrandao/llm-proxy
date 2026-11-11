"""A gate that opens at most once per interval.

For a warning about a condition that persists: a full disk fails every ledger
write, an unreachable vendor fails every refresh tick, and one line per interval
says as much as one line per failure.
"""
import time


class Throttle:
    """`ready(key)` is True the first time it is asked about `key`, then False
    until `every` seconds have passed since it last said True.

    The first call is always ready because an unseen key has no last time at all,
    rather than a last time of zero: monotonic time counts from boot, so on a
    freshly started host a zero baseline would swallow every warning for the
    first interval, which is exactly when a bad mount shows up.
    """

    def __init__(self, every: float):
        self.every = every
        self._last = {}

    def ready(self, key=None) -> bool:
        now = time.monotonic()
        last = self._last.get(key)
        if last is not None and now - last < self.every:
            return False
        self._last[key] = now
        return True

    def reset(self) -> None:
        self._last.clear()

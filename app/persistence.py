"""Optional on-disk persistence of cumulative metric counters.

Prometheus counters live in memory and reset to zero when the process restarts,
which shows up as a counter reset and loses the deltas around the reboot. When
METRICS_PERSIST is enabled we snapshot the counters to a JSON file (lazily, on a
timer and at shutdown) and re-seed them on boot, so the totals stay continuous.

Only counters are persisted (see metrics.PERSISTABLE_COUNTERS). Gauges reflect
live state and the latency histogram is left to reset — neither is a running
total worth restoring.

NOTE: the file must sit on a volume that outlives the container, otherwise it is
recreated empty on every pod restart and persistence is a no-op.
"""
import asyncio
import json
import logging
import os
import tempfile

from app import config as conf
from app.metrics import PERSISTABLE_COUNTERS

logger = logging.getLogger("llm-proxy")


def _snapshot() -> dict:
    out = {}
    for key, counter in PERSISTABLE_COUNTERS.items():
        series = []
        for metric in counter.collect():
            for sample in metric.samples:
                # The cumulative value sample ends in `_total` (vs `_created`).
                # Match by suffix so we don't depend on the exact emitted name.
                if sample.name.endswith("_total"):
                    series.append({"labels": dict(sample.labels), "value": sample.value})
        out[key] = series
    return out


def load() -> None:
    """Re-seed counters from the persisted snapshot (if enabled and present)."""
    if not conf.METRICS_PERSIST:
        return
    path = conf.METRICS_PERSIST_PATH
    if not os.path.exists(path):
        logger.info(f"No metrics state at {path}; starting fresh")
        return
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:  # noqa: BLE001 - never let a bad file block startup
        logger.warning(f"Could not read metrics state from {path}: {e}")
        return

    restored = 0
    for name, counter in PERSISTABLE_COUNTERS.items():
        for entry in data.get(name, []):
            try:
                value = float(entry["value"])
                if value > 0:
                    counter.labels(**entry["labels"]).inc(value)
                    restored += 1
            except Exception as e:  # noqa: BLE001 - skip malformed entries
                logger.warning(f"Skipping bad metric entry for {name}: {e}")
    logger.info(f"Restored {restored} metric series from {path}")


def dump() -> None:
    """Atomically write the current counter values to disk (if enabled)."""
    if not conf.METRICS_PERSIST:
        return
    path = conf.METRICS_PERSIST_PATH
    try:
        data = _snapshot()
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".metrics-", suffix=".tmp")
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)  # atomic on POSIX
    except Exception as e:  # noqa: BLE001 - persistence must never crash the app
        logger.warning(f"Could not persist metrics to {path}: {e}")


async def flush_loop() -> None:
    """Periodically snapshot counters to disk (the 'lazy write')."""
    while True:
        await asyncio.sleep(conf.METRICS_FLUSH_INTERVAL)
        dump()

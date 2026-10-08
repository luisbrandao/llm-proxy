"""The cost ledger: one durable row per completed request, in SQLite.

The console's Costs tab answers "what did this model cost today / this month,
and which requests were they?" — questions that span restarts. Nothing else in
the process does: the In-flight history is a bounded in-memory ring, and the
Prometheus counters are deliberately reset-on-restart (see app/metrics.py). The
per-request log line *is* durable, but it lives in Loki, which the console cannot
query. So every `event=request` line is also appended here, as a row, and the
tab reads back aggregates.

Why this is allowed when persisting the counters is not: a counter re-seeded
from disk comes back *lower* than the value Prometheus already scraped, and that
dip is read as a reset and credited as a phantom increase. This ledger is never
fed back into a counter — it is an append-only record read only by the admin
API, exactly like the log lines it mirrors. Losing a row (a crash between the
response and the write) loses one row, not the shape of a time series.

Where it lives: `LEDGER_PATH` (app/config.py), by default `ledger.sqlite` next
to the config file — in the deploy that directory is the rw bind mount, so the
ledger persists where the config does. `""` disables it; `":memory:"` keeps it
for the life of the process (tests). SQLite because it is in the stdlib, one
file, and the questions are GROUP BYs. Rows older than `LEDGER_RETENTION_DAYS`
are pruned at startup and once a day.

Threading: SQLite calls block, so every call here runs in a worker thread
(`asyncio.to_thread`) on one shared connection guarded by `_lock` — a few hundred
microseconds per insert, off the event loop that is relaying every stream. WAL
mode with `synchronous=NORMAL` so a commit does not fsync; durability to the
last checkpoint is plenty for a ledger of what the backends already billed.

Write-only from the request path, and a write that fails must never fail the
request: `record` swallows and logs (rate-limited), the response is already on
its way. Nothing in admission, routing or failover reads this.
"""
import asyncio
import logging
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from typing import Optional

from app import config as conf
from app.usage import Usage

logger = logging.getLogger("llm-proxy")

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None
_path: Optional[str] = None
# The local day the last retention prune ran for; a write on a new day re-prunes.
_pruned_for: Optional[str] = None
# When a write failure was last logged (monotonic), None until the first — one
# warning per WARN_EVERY seconds, not one per request, when the disk is full or
# the file unwritable. None rather than 0.0 on purpose: monotonic time counts
# from boot, so on a freshly started host a 0.0 baseline would swallow every
# failure in the first five minutes — which is exactly when a bad mount shows up.
_warned_at: Optional[float] = None
WARN_EVERY = 300.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id                 INTEGER PRIMARY KEY,
    ts                 REAL    NOT NULL,   -- unix time the request finished
    day                TEXT    NOT NULL,   -- local date, YYYY-MM-DD (container TZ)
    provider           TEXT    NOT NULL,
    model              TEXT    NOT NULL,   -- native id on the wire (the log's model=)
    asked              TEXT    NOT NULL,   -- what the client sent (the log's asked=, or model)
    served             TEXT,               -- what the backend says ran, when different
    status             INTEGER NOT NULL,
    stream             INTEGER NOT NULL,
    op                 TEXT,
    duration           REAL    NOT NULL,
    prompt_tokens      INTEGER NOT NULL,
    completion_tokens  INTEGER NOT NULL,
    cached_tokens      INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    reasoning_tokens   INTEGER NOT NULL,
    cost               REAL,               -- NULL: the backend reported no price
    currency           TEXT,
    svc                TEXT,
    client             TEXT
);
CREATE INDEX IF NOT EXISTS requests_day ON requests(day);
CREATE INDEX IF NOT EXISTS requests_ts  ON requests(ts);
"""

# The columns `requests` rows are returned with (and the order of `_ROW_SQL`).
_ROW_COLUMNS = (
    "ts", "day", "provider", "model", "asked", "served", "status", "stream", "op",
    "duration", "prompt", "completion", "cached", "cache_write", "reasoning",
    "cost", "currency", "svc", "client",
)
_ROW_SQL = (
    "ts, day, provider, model, asked, served, status, stream, op, duration, "
    "prompt_tokens, completion_tokens, cached_tokens, cache_write_tokens, "
    "reasoning_tokens, cost, currency, svc, client"
)

# One aggregate shape for every GROUP BY, so the totals, the per-day, per-model
# and per-provider rows all carry the same fields and the console renders them
# with one function.
# Aliased (`agg_*`) so an ORDER BY can name them without colliding with the
# table's own columns — `cost` is both a column and an aggregate here.
_AGG_SQL = (
    "COUNT(*) AS agg_requests, SUM(status >= 400), SUM(cost IS NOT NULL), "
    "COALESCE(SUM(cost), 0) AS agg_cost, "
    "SUM(prompt_tokens), SUM(completion_tokens), SUM(cached_tokens), "
    "SUM(cache_write_tokens), SUM(reasoning_tokens)"
)
_AGG_COLUMNS = (
    "requests", "errors", "priced", "cost", "prompt", "completion", "cached",
    "cache_write", "reasoning",
)


def local_day(now: Optional[datetime] = None) -> str:
    """Today as the container's local date — the bucket `by_day` groups on."""
    return (now or datetime.now().astimezone()).date().isoformat()


def _connect() -> Optional[sqlite3.Connection]:
    """The shared connection, opened on first use (and re-opened if LEDGER_PATH
    changed, which only tests do). None when the ledger is disabled. Caller
    holds `_lock`."""
    global _conn, _path, _pruned_for
    path = conf.LEDGER_PATH
    if _conn is not None and _path == path:
        return _conn
    if _conn is not None:
        _conn.close()
        _conn = None
    _path = path
    _pruned_for = None
    if not path:
        return None
    # isolation_level=None: autocommit, each insert its own transaction.
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(SCHEMA)
    _conn = conn
    _prune(conn, local_day())
    return conn


def _prune(conn: sqlite3.Connection, today: str) -> int:
    """Drop rows older than the retention window. Returns how many went."""
    global _pruned_for
    _pruned_for = today
    days = conf.LEDGER_RETENTION_DAYS
    if days <= 0:
        return 0
    cutoff = (date.fromisoformat(today) - timedelta(days=days)).isoformat()
    cur = conn.execute("DELETE FROM requests WHERE day < ?", (cutoff,))
    if cur.rowcount:
        logger.info("Cost ledger: pruned %d row(s) older than %s", cur.rowcount, cutoff)
    return cur.rowcount


def _insert(row: tuple, today: str) -> None:
    with _lock:
        conn = _connect()
        if conn is None:
            return
        if _pruned_for != today:
            _prune(conn, today)
        conn.execute(
            "INSERT INTO requests (ts, day, provider, model, asked, served, status, stream, op, "
            "duration, prompt_tokens, completion_tokens, cached_tokens, cache_write_tokens, "
            "reasoning_tokens, cost, currency, svc, client) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            row,
        )


async def record(
    *, provider: str, model: str, asked: Optional[str], served: Optional[str],
    status: int, stream: bool, op: Optional[str], duration: float, usage: Usage,
    svc: Optional[str], client: Optional[str], when: Optional[datetime] = None,
) -> None:
    """Append one finished request. Never raises: the response is already on its
    way, and a ledger that cannot be written is a warning, not a failed request.
    `when` is the completion time, now by default (tests place rows on other days)."""
    global _warned_at
    if not conf.LEDGER_PATH:
        return
    now = when or datetime.now().astimezone()
    row = (
        now.timestamp(), local_day(now), provider, model, asked or model,
        served if served and served != model else None,
        int(status), 1 if stream else 0, op, float(duration),
        usage.prompt, usage.completion, usage.cached, usage.cache_write, usage.reasoning,
        usage.cost, usage.currency if usage.cost is not None else None,
        svc, client,
    )
    try:
        # The prune key is the real today, whatever day the row itself is for.
        await asyncio.to_thread(_insert, row, local_day())
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 - a broken ledger must not break requests
        now_mono = time.monotonic()
        if _warned_at is None or now_mono - _warned_at >= WARN_EVERY:
            _warned_at = now_mono
            logger.warning(
                "Cost ledger write failed (%s): %s: %s — requests are not being recorded",
                conf.LEDGER_PATH, type(e).__name__, e,
            )


def _agg(row) -> dict:
    out = dict(zip(_AGG_COLUMNS, row))
    for key in _AGG_COLUMNS:
        if key == "cost":
            out[key] = float(out[key] or 0)
        else:
            out[key] = int(out[key] or 0)
    return out


def _where(clauses) -> str:
    return (" WHERE " + " AND ".join(clauses)) if clauses else ""


def _summary(days: int, day: Optional[str], model: Optional[str],
             provider: Optional[str], limit: int) -> dict:
    with _lock:
        conn = _connect()
        if conn is None:
            return {"enabled": False, "path": "", "retention_days": conf.LEDGER_RETENTION_DAYS}

        today = local_day()
        since = (date.fromisoformat(today) - timedelta(days=days - 1)).isoformat() if days > 0 else None

        # The window every view shares: the range plus the model/provider filters.
        scope, params = [], []
        if since:
            scope.append("day >= ?"); params.append(since)
        if model:
            scope.append("asked = ?"); params.append(model)
        if provider:
            scope.append("provider = ?"); params.append(provider)
        # Everything except by_day also narrows to the selected day, so the
        # chart stays navigable (every day of the range stays visible) while the
        # totals, the model table and the request list answer for that day.
        focus, fparams = list(scope), list(params)
        if day:
            focus.append("day = ?"); fparams.append(day)

        def agg_by(key_sql, where, p, order="agg_cost DESC, agg_requests DESC"):
            cur = conn.execute(
                f"SELECT {key_sql}, {_AGG_SQL} FROM requests{_where(where)} "
                f"GROUP BY {key_sql} ORDER BY {order}", p,
            )
            return cur.fetchall()

        totals = _agg(conn.execute(
            f"SELECT {_AGG_SQL} FROM requests{_where(focus)}", fparams
        ).fetchone())

        by_day = [
            {"day": r[0], **_agg(r[1:])}
            for r in agg_by("day", scope, params, order="day ASC")
        ]

        by_model = {}
        for r in agg_by("asked", focus, fparams):
            by_model[r[0]] = {"model": r[0], "providers": [], **_agg(r[1:])}
        for r in agg_by("asked, provider, model", focus, fparams):
            entry = by_model.get(r[0])
            if entry is not None:
                entry["providers"].append({"provider": r[1], "model": r[2], **_agg(r[3:])})

        by_provider = [
            {"provider": r[0], **_agg(r[1:])} for r in agg_by("provider", focus, fparams)
        ]

        rows = conn.execute(
            f"SELECT {_ROW_SQL} FROM requests{_where(focus)} "
            f"ORDER BY ts DESC, id DESC LIMIT ?", fparams + [limit],
        ).fetchall()
        requests = []
        for r in rows:
            item = dict(zip(_ROW_COLUMNS, r))
            item["stream"] = bool(item["stream"])
            item["at"] = datetime.fromtimestamp(item["ts"]).astimezone().isoformat(timespec="seconds")
            requests.append(item)

        # Filter options: everything in the range, regardless of the current filter.
        range_only, rparams = [], []
        if since:
            range_only.append("day >= ?"); rparams.append(since)
        models = [r[0] for r in conn.execute(
            f"SELECT DISTINCT asked FROM requests{_where(range_only)} ORDER BY asked", rparams
        )]
        providers = [r[0] for r in conn.execute(
            f"SELECT DISTINCT provider FROM requests{_where(range_only)} ORDER BY provider", rparams
        )]
        currencies = [r[0] for r in conn.execute(
            f"SELECT DISTINCT currency FROM requests{_where(focus + ['cost IS NOT NULL'])} "
            f"ORDER BY currency", fparams
        )]
        count, oldest = conn.execute("SELECT COUNT(*), MIN(day) FROM requests").fetchone()

    return {
        "enabled": True,
        "path": conf.LEDGER_PATH,
        "retention_days": conf.LEDGER_RETENTION_DAYS,
        "rows": int(count or 0),
        "oldest": oldest,
        "today": today,
        "range": {"days": days, "since": since, "day": day},
        "filters": {"model": model, "provider": provider},
        "totals": totals,
        "by_day": by_day,
        "by_model": list(by_model.values()),
        "by_provider": by_provider,
        "requests": requests,
        "limit": limit,
        "models": models,
        "providers": providers,
        "currencies": currencies,
    }


async def summary(days: int = 30, day: Optional[str] = None, model: Optional[str] = None,
                  provider: Optional[str] = None, limit: int = 200) -> dict:
    """What the Costs tab renders: totals, per-day, per-model (with the
    per-provider split under each), per-provider, and the newest `limit`
    matching requests — all over the last `days` days (0 = everything kept),
    optionally narrowed to one `day`, one `model` (the name the client sent) and
    one `provider`."""
    return await asyncio.to_thread(_summary, days, day, model, provider, limit)


def startup() -> None:
    """Open the ledger at boot so a bad path is logged once, up front, instead
    of on the first request — and so the retention prune runs before traffic."""
    if not conf.LEDGER_PATH:
        logger.info("Cost ledger disabled (LEDGER_PATH is empty)")
        return
    try:
        with _lock:
            conn = _connect()
            count, oldest = conn.execute("SELECT COUNT(*), MIN(day) FROM requests").fetchone()
        logger.info(
            "Cost ledger at %s: %d row(s)%s, keeping %d days",
            conf.LEDGER_PATH, count or 0, f" since {oldest}" if oldest else "",
            conf.LEDGER_RETENTION_DAYS,
        )
    except Exception as e:  # noqa: BLE001 - the proxy must come up without its ledger
        logger.warning(
            "Cost ledger unavailable at %s: %s: %s — the Costs tab will be empty",
            conf.LEDGER_PATH, type(e).__name__, e,
        )


def close() -> None:
    """Close the connection (lifespan shutdown; tests, between cases)."""
    global _conn, _path, _pruned_for
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = None
        _path = None
        _pruned_for = None

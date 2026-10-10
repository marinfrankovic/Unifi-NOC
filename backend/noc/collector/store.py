"""Shared in-memory collector state and the metrics writer."""
import logging
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from ..db import kv_set

log = logging.getLogger("noc.store")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def from_epoch(v) -> datetime | None:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= 0:
        return None
    if v > 1e12:
        v /= 1000.0
    return datetime.fromtimestamp(v, timezone.utc)


def num(v):
    """Controller values are often strings; return float or None (never invent a value)."""
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f == f else None  # NaN guard


@dataclass
class Site:
    name: str
    site_id: str
    uuid: str | None
    description: str
    slug: str


@dataclass
class Current:
    site: str
    entity_type: str
    entity_id: str
    entity_name: str
    metric: str
    value: float
    ts: float  # epoch seconds


@dataclass
class State:
    sites: dict[str, Site] = field(default_factory=dict)            # short name -> Site
    by_site_id: dict[str, str] = field(default_factory=dict)         # controller _id -> short name
    devices: dict[str, dict] = field(default_factory=dict)           # mac -> sanitized record (+site)
    current: dict[tuple[str, str], Current] = field(default_factory=dict)
    counters: dict[str, tuple[float, dict]] = field(default_factory=dict)  # key -> (monotonic, {name: value})
    windows: dict[str, deque] = field(default_factory=lambda: defaultdict(lambda: deque(maxlen=720)))
    tunnels: dict[str, dict] = field(default_factory=dict)           # connectionId -> config/status
    sources: dict[str, dict] = field(default_factory=dict)
    health: dict[str, dict] = field(default_factory=dict)

    def site_slug(self, site: str) -> str:
        s = self.sites.get(site)
        return s.slug if s else site

    def probe_origin(self, identity: str) -> str | None:
        if not identity:
            return None
        stable = [name for name, site in self.sites.items() if identity == name or identity == site.site_id]
        if len(stable) == 1:
            return stable[0]
        legacy = [name for name, site in self.sites.items() if identity.casefold() == site.description.casefold()]
        return legacy[0] if len(legacy) == 1 else None

    def put_current(self, site, etype, eid, name, metric, value, ts=None):
        if value is None:
            return
        self.current[(eid, metric)] = Current(site, etype, eid, name or eid, metric, float(value), ts or time.time())

    def get_current(self, eid, metric, max_age: float | None = None):
        c = self.current.get((eid, metric))
        if c is None:
            return None
        if max_age is not None and time.time() - c.ts > max_age:
            return None
        return c.value

    def rate(self, key: str, values: dict[str, float], now: float | None = None) -> dict[str, float]:
        """Per-second deltas of monotonically increasing counters. Resets yield no value."""
        now = now if now is not None else time.monotonic()
        prev = self.counters.get(key)
        self.counters[key] = (now, values)
        if not prev:
            return {}
        dt = now - prev[0]
        if dt <= 0.5:
            return {}
        out = {}
        for k, v in values.items():
            pv = prev[1].get(k)
            if pv is None or v is None or v < pv:
                continue
            out[k] = (v - pv) / dt
        return out

    def delta(self, key: str, values: dict[str, float]) -> dict[str, float]:
        prev = self.counters.get(key)
        self.counters[key] = (time.monotonic(), values)
        if not prev:
            return {}
        return {k: v - prev[1][k] for k, v in values.items() if v is not None and prev[1].get(k) is not None and v >= prev[1][k]}

    def window_add(self, key: str, value: float, ts: float | None = None) -> None:
        self.windows[key].append((ts or time.time(), value))

    def window(self, key: str, seconds: float) -> list[float]:
        cutoff = time.time() - seconds
        return [v for t, v in self.windows.get(key, ()) if t >= cutoff]


class MetricBuffer:
    def __init__(self) -> None:
        self.rows: list[tuple] = []

    def add(self, ts: datetime, site: str, etype: str, eid: str, metric: str, value) -> None:
        v = num(value)
        if v is None:
            return
        self.rows.append((ts, site, etype, eid, metric, v))

    async def flush(self, pool) -> int:
        if not self.rows:
            return 0
        rows, self.rows = self.rows, []
        # De-duplicate within the batch (unique index on entity_id, metric, ts).
        uniq = {(r[3], r[4], r[0]): r for r in rows}
        cols = list(zip(*uniq.values()))
        await pool.execute(
            """
            INSERT INTO metrics (ts, site, entity_type, entity_id, metric, value)
            SELECT * FROM unnest($1::timestamptz[], $2::text[], $3::text[], $4::text[], $5::text[], $6::float8[])
            ON CONFLICT DO NOTHING
            """,
            *[list(c) for c in cols],
        )
        return len(uniq)


async def record_source(state: State, pool, name: str, interval: float, ok: bool, duration_ms: float, error: str | None = None, extra: dict | None = None):
    now = time.time()
    s = state.sources.setdefault(name, {"name": name, "interval": interval, "last_ok": None, "consecutive_errors": 0, "total_errors": 0, "runs": 0})
    s["interval"] = interval
    s["runs"] += 1
    s["last_run"] = now
    s["duration_ms"] = round(duration_ms, 1)
    if ok:
        s["last_ok"] = now
        s["consecutive_errors"] = 0
        s["error"] = None
    else:
        s["consecutive_errors"] += 1
        s["total_errors"] += 1
        s["error"] = (error or "error")[:500]
        s["last_error"] = now
    if extra:
        s.update(extra)
    try:
        await kv_set(pool, f"source.{name}", s)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not persist source status %s: %s", name, exc)

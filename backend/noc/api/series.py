"""Time-series query helpers with automatic resolution / downsampling."""
import re
import time
from datetime import datetime, timezone

from fastapi import HTTPException

from ..config import settings

RANGES = {"15m": 900, "1h": 3600, "6h": 21600, "24h": 86400, "7d": 604800, "30d": 2592000}


def parse_time(v: str | None) -> float | None:
    if v is None or v == "":
        return None
    if re.fullmatch(r"\d+(\.\d+)?", v):
        f = float(v)
        return f / 1000 if f > 1e11 else f
    try:
        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise HTTPException(400, f"invalid time: {v}") from exc


def window(range_: str | None, frm: str | None, to: str | None) -> tuple[float, float]:
    end = parse_time(to) or time.time()
    start = parse_time(frm)
    if start is None:
        span = RANGES.get(range_ or "1h")
        if span is None:
            raise HTTPException(400, f"range must be one of {', '.join(RANGES)}")
        start = end - span
    if start >= end:
        raise HTTPException(400, "from must be before to")
    if end - start > 400 * 86400:
        raise HTTPException(400, "window too large")
    return start, end


def resolution(start: float, end: float) -> tuple[str, str | None]:
    span = end - start
    raw_ok = start > time.time() - settings.metrics_retention + 600
    if span <= 2 * 3600 and raw_ok:
        return "raw", None
    if span <= 8 * 3600 and raw_ok:
        return "raw", "1 minute"
    if span <= 36 * 3600:
        return "5m", "5 minutes"
    if span <= 8 * 86400:
        return "5m", "30 minutes"
    return "1h", "2 hours" if span > 40 * 86400 else "1 hour"


def to_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


async def series(pool, entity_ids: list[str], metrics: list[str], start: float, end: float, force: str | None = None,
                 allowed_sites: list[str] | None = None, denied_entities: list[str] | None = None,
                 allowed_tunnels: list[str] | None = None) -> dict:
    src, bucket = resolution(start, end)
    if force == "raw" and end - start <= 6 * 3600:
        src, bucket = "raw", None
    a, b = to_dt(start), to_dt(end)
    if src == "raw" and bucket is None:
        rows = await pool.fetch(
            """SELECT entity_id, metric, ts AS t, value AS avg, value AS min, value AS max FROM metrics
                    WHERE entity_id = ANY($1) AND metric = ANY($2) AND ts >= $3 AND ts <= $4
                    AND ($5::text[] IS NULL OR site=ANY($5)) AND NOT(entity_id=ANY($6::text[]))
                    AND ($7::text[] IS NULL OR entity_type<>'tunnel' OR entity_id=ANY($7)) ORDER BY entity_id, metric, ts""",
                entity_ids, metrics, a, b, allowed_sites, denied_entities or [], allowed_tunnels)
    elif src == "raw":
        rows = await pool.fetch(
            f"""SELECT entity_id, metric, time_bucket('{bucket}', ts) AS t, avg(value) AS avg, min(value) AS min, max(value) AS max FROM metrics
                    WHERE entity_id = ANY($1) AND metric = ANY($2) AND ts >= $3 AND ts <= $4
                    AND ($5::text[] IS NULL OR site=ANY($5)) AND NOT(entity_id=ANY($6::text[]))
                    AND ($7::text[] IS NULL OR entity_type<>'tunnel' OR entity_id=ANY($7)) GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""",
                entity_ids, metrics, a, b, allowed_sites, denied_entities or [], allowed_tunnels)
    else:
        view = "metrics_5m" if src == "5m" else "metrics_1h"
        rows = await pool.fetch(
            f"""SELECT entity_id, metric, time_bucket('{bucket}', bucket) AS t, sum(avg * n) / nullif(sum(n), 0) AS avg, min(min) AS min, max(max) AS max
                FROM {view} WHERE entity_id = ANY($1) AND metric = ANY($2) AND bucket >= $3 AND bucket <= $4
                AND ($5::text[] IS NULL OR site=ANY($5)) AND NOT(entity_id=ANY($6::text[]))
                AND ($7::text[] IS NULL OR entity_type<>'tunnel' OR entity_id=ANY($7)) GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""",
            entity_ids, metrics, a, b, allowed_sites, denied_entities or [], allowed_tunnels)
    out: dict[tuple[str, str], list] = {}
    for r in rows:
        out.setdefault((r["entity_id"], r["metric"]), []).append(
            [int(r["t"].timestamp() * 1000), _r(r["avg"]), _r(r["min"]), _r(r["max"])])
    bucket_seconds = {'1 minute': 60, '5 minutes': 300, '30 minutes': 1800, '1 hour': 3600, '2 hours': 7200}.get(bucket)
    result = []
    for (entity, metric), points in out.items():
        source_cadence = 300 if entity.startswith('wan:') and (metric.startswith('ua_') or metric in ('loss_pct', 'downtime', 'high_latency_flag', 'packet_loss_flag')) else settings.live_poll_interval if (entity.startswith(('tunnel:', 'gwif:')) and metric not in ('connected', 'reconnects_1h')) or metric.startswith('live_') else settings.client_poll_interval if entity.startswith('client:') else settings.poll_interval
        cadence = max(bucket_seconds or 0, source_cadence)
        expected = max(1, int((end - start) / cadence))
        occupied = len({int(point[0] / (cadence * 1000)) for point in points if point[1] is not None and start * 1000 <= point[0] <= end * 1000})
        result.append({'entity_id': entity, 'metric': metric, 'points': points, 'expected_cadence_ms': cadence * 1000,
                       'coverage': {'observed': occupied, 'expected': expected, 'percent': round(min(100, occupied / expected * 100), 1)}})
    return {
        "from": int(start * 1000), "to": int(end * 1000), "resolution": bucket or "raw", "source": src,
        "series": result,
    }


def _r(v):
    return None if v is None else round(float(v), 4)


async def latest(pool, entity_ids: list[str], max_age_s: int = 180, metrics: list[str] | None = None) -> dict[str, dict]:
    """Latest value per (entity, metric) within max_age; older values are NOT returned (stale data is never shown as current)."""
    if not entity_ids:
        return {}
    rows = await pool.fetch(
        """SELECT DISTINCT ON (entity_id, metric) entity_id, metric, value, ts FROM metrics
           WHERE entity_id = ANY($1) AND ts > now() - make_interval(secs => $2) AND ($3::text[] IS NULL OR metric = ANY($3))
           ORDER BY entity_id, metric, ts DESC""", entity_ids, max_age_s, metrics)
    out: dict[str, dict] = {}
    for r in rows:
        out.setdefault(r["entity_id"], {})[r["metric"]] = round(r["value"], 4)
        out[r["entity_id"]].setdefault("_ts", int(r["ts"].timestamp() * 1000))
    return out

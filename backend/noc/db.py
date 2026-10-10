import asyncio
import json
import logging
from pathlib import Path

import asyncpg

from .config import settings
from .migrations import apply_migrations, bind_environment_connection

log = logging.getLogger("noc.db")
SCHEMA = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


async def connect(min_size: int = 1, max_size: int = 8) -> asyncpg.Pool:
    delay = 1.0
    while True:
        try:
            return await asyncpg.create_pool(settings.database_url, min_size=min_size, max_size=max_size, init=_init_conn, command_timeout=60)
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning("database not ready (%s); retrying in %.0fs", exc, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 15)


async def _exists(conn, sql: str, *args) -> bool:
    return bool(await conn.fetchval(sql, *args))


async def migrate(pool: asyncpg.Pool) -> None:
    """Create schema, continuous aggregates and retention policies. Safe to run repeatedly."""
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock(424242)")
        try:
            await conn.execute(SCHEMA)
            await apply_migrations(conn)
            if not settings.ui_setup:
                await bind_environment_connection(conn)
            if not await _exists(conn, "SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name='metrics_5m'"):
                await conn.execute(
                    """
                    CREATE MATERIALIZED VIEW metrics_5m WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
                    SELECT time_bucket('5 minutes', ts) AS bucket, site, entity_type, entity_id, metric,
                           avg(value) AS avg, min(value) AS min, max(value) AS max, count(*) AS n
                    FROM metrics GROUP BY 1, 2, 3, 4, 5 WITH NO DATA
                    """
                )
                await conn.execute("CREATE INDEX IF NOT EXISTS metrics_5m_entity ON metrics_5m (entity_id, metric, bucket DESC)")
            if not await _exists(conn, "SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_name='metrics_1h'"):
                await conn.execute(
                    """
                    CREATE MATERIALIZED VIEW metrics_1h WITH (timescaledb.continuous, timescaledb.materialized_only = false) AS
                    SELECT time_bucket('1 hour', bucket) AS bucket, site, entity_type, entity_id, metric,
                           sum(avg * n) / nullif(sum(n), 0) AS avg, min(min) AS min, max(max) AS max, sum(n)::bigint AS n
                    FROM metrics_5m GROUP BY 1, 2, 3, 4, 5 WITH NO DATA
                    """
                )
                await conn.execute("CREATE INDEX IF NOT EXISTS metrics_1h_entity ON metrics_1h (entity_id, metric, bucket DESC)")

            # Compression on raw data (segment per series).
            if not await _exists(conn, "SELECT compression_enabled FROM timescaledb_information.hypertables WHERE hypertable_name='metrics'"):
                await conn.execute(
                    "ALTER TABLE metrics SET (timescaledb.compress, timescaledb.compress_segmentby = 'entity_id, metric', timescaledb.compress_orderby = 'ts DESC')"
                )
            await conn.execute("SELECT add_compression_policy('metrics', INTERVAL '1 day', if_not_exists => true)")

            # Policies are re-applied so changed retention settings take effect.
            raw_h = max(settings.metrics_retention // 3600, 6)
            refresh_h = max(raw_h - 2, 3)
            await conn.execute("SELECT remove_retention_policy('metrics', if_exists => true)")
            await conn.execute(f"SELECT add_retention_policy('metrics', INTERVAL '{raw_h} hours')")
            await conn.execute("SELECT remove_continuous_aggregate_policy('metrics_5m', if_exists => true)")
            await conn.execute(
                f"SELECT add_continuous_aggregate_policy('metrics_5m', start_offset => INTERVAL '{refresh_h} hours', end_offset => INTERVAL '5 minutes', schedule_interval => INTERVAL '5 minutes')"
            )
            await conn.execute("SELECT remove_continuous_aggregate_policy('metrics_1h', if_exists => true)")
            await conn.execute(
                "SELECT add_continuous_aggregate_policy('metrics_1h', start_offset => INTERVAL '3 days', end_offset => INTERVAL '1 hour', schedule_interval => INTERVAL '30 minutes')"
            )
            await conn.execute("SELECT remove_retention_policy('metrics_5m', if_exists => true)")
            await conn.execute(f"SELECT add_retention_policy('metrics_5m', INTERVAL '{max(settings.metrics_retention_5m // 86400, 2)} days')")
            await conn.execute("SELECT remove_retention_policy('metrics_1h', if_exists => true)")
            await conn.execute(f"SELECT add_retention_policy('metrics_1h', INTERVAL '{max(settings.metrics_retention_1h // 86400, 7)} days')")
            log.info("schema ready (raw retention %sh, 5m %sd, 1h %sd)", raw_h, settings.metrics_retention_5m // 86400, settings.metrics_retention_1h // 86400)
        finally:
            await conn.execute("SELECT pg_advisory_unlock(424242)")


async def kv_set(pool_or_conn, key: str, value) -> None:
    await pool_or_conn.execute(
        "INSERT INTO kv_state (key, value, updated_at) VALUES ($1, $2, now()) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()",
        key,
        value,
    )


async def kv_get(pool_or_conn, key: str, default=None):
    row = await pool_or_conn.fetchrow("SELECT value FROM kv_state WHERE key = $1", key)
    return row["value"] if row else default

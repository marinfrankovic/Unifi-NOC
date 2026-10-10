import hashlib
from pathlib import Path
import re

from .config import settings


DIRECTORY = Path(__file__).with_name("migration_sql")


async def apply_migrations(conn, directory: Path = DIRECTORY) -> None:
    migrations = []
    versions = set()
    for path in sorted(directory.glob("*.sql")):
        match = re.fullmatch(r"(\d{4})_[a-z0-9_]+\.sql", path.name)
        if not match or match[1] in versions:
            raise ValueError(f"Invalid or duplicate migration version: {path.name}")
        source = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        migrations.append((match[1], path.name, hashlib.sha256(source.encode()).hexdigest(), source))
        versions.add(match[1])
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(424243)")
        await conn.execute("""CREATE TABLE IF NOT EXISTS schema_migrations (
            version text PRIMARY KEY, name text NOT NULL, checksum text NOT NULL,
            applied_at timestamptz NOT NULL DEFAULT now())""")
        applied = {row["version"]: row for row in await conn.fetch("SELECT version, name, checksum FROM schema_migrations")}
        unknown = set(applied) - versions
        if unknown:
            raise ValueError("Database has migrations not present in this application: " + ", ".join(sorted(unknown)))
        for version, name, checksum, source in migrations:
            if version in applied:
                if applied[version]["checksum"] != checksum or applied[version]["name"] != name:
                    raise ValueError(f"Applied migration has changed: {name}")
                continue
            await conn.execute(source)
            await conn.execute("INSERT INTO schema_migrations (version, name, checksum) VALUES ($1, $2, $3)", version, name, checksum)


async def bind_environment_connection(conn) -> str:
    async with conn.transaction():
        await conn.execute("SELECT pg_advisory_xact_lock(424243)")
        source = "ui" if settings.ui_setup else "environment"
        row = await conn.fetchrow("SELECT id, controller_url FROM connections WHERE config_source=$1", source)
        if row:
            if row["controller_url"] != settings.controller_url:
                raise ValueError("Configured UniFi OS URL differs from the saved environment connection; explicit connection migration is required")
            return str(row["id"])
        if settings.ui_setup:
            raise ValueError("Connect UniFi OS through setup before collecting sites")
        connection_id = await conn.fetchval(
            """INSERT INTO connections (name, controller_url, enabled, config_source)
               VALUES ('Existing UniFi OS', $1, true, 'environment') RETURNING id""", settings.controller_url)
        sites = await conn.fetch("SELECT name, description FROM sites WHERE connection_id IS NULL")
        for site in sites:
            await conn.execute("UPDATE sites SET connection_id=$1, selected=$2 WHERE name=$3",
                               connection_id, settings.site_matches(site["description"]), site["name"])
        return str(connection_id)
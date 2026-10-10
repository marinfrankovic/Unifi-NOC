import asyncio
import ssl
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes

from ..config import settings
from ..db import kv_get, kv_set
from ..secrets import decrypt_config
from ..unifi import UniFiClient
from .poll import slugify


async def save_discovered_sites(conn, connection_id, legacy: list[dict], integration: list[dict], enable_all: bool) -> None:
    if not legacy:
        raise ValueError('No sites are visible to this API key; check UniFi OS permissions')
    existing = {row['name']: row for row in await conn.fetch('SELECT name, site_id, connection_id, slug, selected FROM sites')}
    taken = {row['slug'] for row in existing.values()}
    identifiers = {site.get('internalReference'): site.get('id') for site in integration}
    for site in legacy:
        previous = existing.get(site['name'])
        if previous and (previous['site_id'] != site['_id'] or previous['connection_id'] not in (None, connection_id)):
            raise ValueError('A discovered site conflicts with an existing identity; automatic replacement is refused')
        slug = previous['slug'] if previous else slugify(site.get('desc') or site['name'], taken)
        taken.add(slug)
        selected = enable_all or bool(previous and previous['selected'])
        await conn.execute("""INSERT INTO sites (name, site_id, uuid, description, slug, connection_id, selected)
            VALUES ($1,$2,$3,$4,$5,$6,$7) ON CONFLICT (name) DO UPDATE SET
            uuid=EXCLUDED.uuid, description=EXCLUDED.description, connection_id=EXCLUDED.connection_id,
            selected=EXCLUDED.selected, updated_at=now()""", site['name'], site['_id'], identifiers.get(site['name']),
            site.get('desc') or site['name'], slug, connection_id, selected)


async def inspect_certificate(profile: dict) -> dict:
    parsed = urlsplit(profile['url'])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.wait_for(asyncio.open_connection(parsed.hostname, parsed.port or 443,
        ssl=context, server_hostname=profile.get('tls_server_name') or parsed.hostname), timeout=10)
    try:
        certificate = x509.load_der_x509_certificate(writer.get_extra_info('ssl_object').getpeercert(binary_form=True))
        try:
            names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName)
        except x509.ExtensionNotFound:
            names = []
        from cryptography.hazmat.primitives.serialization import Encoding
        return {"state": "ready", "pem": certificate.public_bytes(Encoding.PEM).decode(),
                "fingerprint": certificate.fingerprint(hashes.SHA256()).hex(), "dns_names": names,
                "subject": certificate.subject.rfc4514_string(), "expires": certificate.not_valid_after_utc.isoformat()}
    finally:
        writer.close()
        await writer.wait_closed()


async def process_job(pool) -> None:
    job = await kv_get(pool, 'setup.connection_job')
    if not job:
        return
    try:
        profile = decrypt_config(job['encrypted'])
        if job['kind'] == 'certificate':
            await kv_set(pool, 'setup.certificate', await inspect_certificate(profile))
        else:
            client = UniFiClient(profile)
            try:
                info = await client.info()
                legacy, integration = await client.sites()
            finally:
                await client.close()
            if not info.get('applicationVersion'):
                raise ValueError('This endpoint did not identify a UniFi OS Network application')
            async with pool.acquire() as conn:
                async with conn.transaction():
                    first_setup = not bool(await kv_get(conn, 'setup.complete'))
                    existing = await conn.fetchrow('SELECT id, controller_url FROM connections ORDER BY created_at LIMIT 1')
                    if existing and existing['controller_url'] != profile['url'] and await conn.fetchval('SELECT count(*) FROM sites WHERE selected'):
                        raise ValueError('Deselect monitored sites before connecting a different UniFi OS instance')
                    if existing:
                        connection_id = existing['id']
                        await conn.execute("UPDATE connections SET controller_url=$1, config_source='ui', enabled=true WHERE id=$2", profile['url'], existing['id'])
                    else:
                        connection_id = await conn.fetchval("INSERT INTO connections (name, controller_url, enabled, config_source) VALUES ('UniFi OS', $1, true, 'ui') RETURNING id", profile['url'])
                    await save_discovered_sites(conn, connection_id, legacy, integration, first_setup)
                    await kv_set(conn, 'connection.profile', job['encrypted'])
                    await kv_set(conn, 'connection.runtime', {'controller_url':profile['url'],
                        'gateway_ssh_enabled':profile.get('gateway_ssh',False), 'probe_enabled':profile.get('probes',False),
                        'probe_local_site':profile.get('probe_site_id') or profile.get('probe_site','')})
                    await kv_set(conn, 'setup.connection_state', {"state": "connected", "url": profile['url'],
                        "version": info['applicationVersion'], "discovered_sites": len(legacy), "job_id": job['id']})
                    await kv_set(conn, 'setup.complete', True)
    except Exception as error:
        state = {"state": "error", "error": f"{type(error).__name__}: {error}"[:500]}
        await kv_set(pool, 'setup.certificate' if job['kind'] == 'certificate' else 'setup.connection_state', state)
    finally:
        await pool.execute("DELETE FROM kv_state WHERE key='setup.connection_job' AND value->>'id'=$1", job['id'])


async def load_profile(pool) -> tuple[str | None, dict | None]:
    encrypted = await kv_get(pool, 'connection.profile')
    return encrypted, decrypt_config(encrypted) if encrypted else None


def apply_profile(profile: dict) -> None:
    settings.controller_url = profile['url']
    settings.gateway_ssh_enabled = profile.get('gateway_ssh', False)
    settings.probe_enabled = profile.get('probes', False)
    settings.probe_local_site = profile.get('probe_site_id') or profile.get('probe_site', '')
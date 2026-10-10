import ssl
from urllib.parse import quote
from uuid import uuid4

import httpx

from .db import kv_get
from .maintenance import active_window
from .secrets import decrypt_config


async def configuration(pool):
    value = await kv_get(pool, 'notifications.config')
    return decrypt_config(value) if value else None


async def process_notifications(pool) -> None:
    config = await configuration(pool)
    if not config or not config.get('enabled'):
        return
    sites = await pool.fetch('SELECT id::text, name, description FROM sites WHERE selected')
    selected = {site['name']: site['description'] for site in sites if not config.get('sites') or site['id'] in config['sites']}
    alerts = await pool.fetch("SELECT * FROM alerts WHERE site=ANY($1::text[]) AND updated_at > now()-interval '1 day'", list(selected))
    for alert in alerts:
        if alert['severity'] not in ('warning','critical'):
            continue
        if config.get('severity') == 'critical' and alert['severity'] != 'critical':
            continue
        key = f"{config['id']}:{alert['id']}:{alert['state']}:{alert['severity']}"
        await pool.execute("""INSERT INTO notification_outbox (source_key, alert_id, site, event_state, payload)
            VALUES ($1,$2,$3,$4,$5) ON CONFLICT DO NOTHING""", key, alert['id'], alert['site'], alert['state'],
            {"message": f"{selected[alert['site']]}: {alert['message']}", "severity": alert['severity'], "state": alert['state'], "channel_id": config['id']})
    windows = await pool.fetch('SELECT * FROM maintenance_windows WHERE enabled')
    held = {window['site'] for window in windows if active_window(window)}
    async with pool.acquire() as conn:
        async with conn.transaction():
            rows = await conn.fetch("""SELECT * FROM notification_outbox WHERE status='pending' AND next_attempt<=now()
                ORDER BY id FOR UPDATE SKIP LOCKED LIMIT 10""")
            for row in rows:
                await conn.execute("UPDATE notification_outbox SET next_attempt=now()+interval '2 minutes' WHERE id=$1", row['id'])
    for row in rows:
        if row['payload'].get('channel_id') != config['id'] or (row['site'] != '_all' and row['site'] not in selected):
            await pool.execute("UPDATE notification_outbox SET status='cancelled', last_error='Delivery scope changed' WHERE id=$1", row['id'])
            continue
        if row['site'] in held:
            await pool.execute("UPDATE notification_outbox SET next_attempt=now()+interval '1 minute', last_error='Held by maintenance window' WHERE id=$1", row['id'])
            continue
        if row['alert_id']:
            current = await pool.fetchrow('SELECT state, severity, acked_by FROM alerts WHERE id=$1', row['alert_id'])
            if not current or current['state'] != row['event_state'] or current['severity'] != row['payload'].get('severity') or (current['acked_by'] and row['event_state'] == 'firing'):
                await pool.execute("UPDATE notification_outbox SET status='cancelled' WHERE id=$1", row['id'])
                continue
            if row['event_state'] == 'resolved' and not await pool.fetchval("SELECT EXISTS(SELECT 1 FROM notification_outbox WHERE alert_id=$1 AND event_state='firing' AND status='sent')", row['alert_id']):
                await pool.execute("UPDATE notification_outbox SET status='cancelled' WHERE id=$1", row['id'])
                continue
        try:
            context = ssl.create_default_context(cadata=config['ca_pem']) if config.get('ca_pem') else True
            async with httpx.AsyncClient(timeout=10, verify=context, trust_env=False) as client:
                if config['provider'] == 'telegram':
                    response = await client.post(f"https://api.telegram.org/bot{config['token']}/sendMessage",
                        json={"chat_id": config['chat_id'], "text": row['payload']['message']})
                else:
                    headers = {'Title': 'UniFi NOC', 'Priority': '4' if row['payload'].get('severity') == 'critical' else '3'}
                    if config.get('token'):
                        headers['Authorization'] = 'Bearer ' + config['token']
                    response = await client.post(config['url'].rstrip('/') + '/' + quote(config['topic'], safe=''),
                        content=row['payload']['message'].encode(), headers=headers)
                response.raise_for_status()
            await pool.execute("UPDATE notification_outbox SET status='sent', sent_at=now(), attempts=attempts+1, last_error=NULL WHERE id=$1", row['id'])
        except Exception as error:
            attempts = row['attempts'] + 1
            await pool.execute("""UPDATE notification_outbox SET attempts=$2, status=$3, last_error=$4,
                next_attempt=now()+make_interval(secs=>$5) WHERE id=$1""", row['id'], attempts,
                'failed' if attempts >= 8 else 'pending', type(error).__name__, min(30 * 2 ** attempts, 3600))
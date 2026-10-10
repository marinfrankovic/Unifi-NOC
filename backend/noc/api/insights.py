from fastapi import APIRouter, Depends, Query, HTTPException
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from urllib.parse import quote

from ..entities import entity_device_mac
from ..config import settings
from ..collector.store import num
from .series import latest
from .access import can_read_site, is_owner


def scoped_sites(sites, slug):
    if not slug or slug == 'all':
        return sites
    result = {name: site for name, site in sites.items() if slug in (name, site['slug'], site.get('description'))}
    if not result:
        raise HTTPException(404, 'Site unavailable')
    return result


def observed_intervals(runs, start, end, cadence):
    intervals = []
    cursor = start
    for index, run in enumerate(runs):
        first, last = max(start, run['first'].timestamp()), run['last'].timestamp()
        if first > cursor:
            intervals.append({'state': 'UNKNOWN', 'start': cursor, 'end': first})
        next_start = runs[index + 1]['first'].timestamp() if index + 1 < len(runs) else end
        stop = min(end, next_start if index + 1 < len(runs) and next_start - last <= cadence * 3 else last + cadence)
        if stop > first:
            intervals.append({'state': run['state'], 'start': first, 'end': stop})
        cursor = max(cursor, stop)
    if cursor < end:
        intervals.append({'state': 'UNKNOWN', 'start': cursor, 'end': end})
    return [{**item, 'duration_s': round(item['end'] - item['start'], 1), 'start': int(item['start'] * 1000), 'end': int(item['end'] * 1000)} for item in intervals]


def maintenance_occurrences(windows, start, end):
    result = []
    for window in windows:
        if not window['enabled']:
            continue
        duration = window['ends_at'] - window['starts_at']
        zone = ZoneInfo(window['timezone'])
        original = window['starts_at'].astimezone(zone)
        day = (datetime.fromtimestamp(start, timezone.utc) - duration).astimezone(zone).date()
        final_day = datetime.fromtimestamp(end, timezone.utc).astimezone(zone).date()
        while day <= final_day:
            occurrence = window['starts_at'] if window['repeat'] == 'none' else datetime.combine(day, original.timetz(), zone).astimezone(timezone.utc)
            valid = window['repeat'] != 'weekly' or day.weekday() == original.weekday()
            first, last = max(start, occurrence.timestamp()), min(end, (occurrence + duration).timestamp())
            if valid and occurrence >= window['starts_at'] and first < last:
                result.append({'title': window['title'], 'site': window['site'], 'start': int(first * 1000), 'end': int(last * 1000)})
            if window['repeat'] == 'none':
                break
            day += timedelta(days=1)
    return result


def correlated_incidents(alerts):
    def started_at(alert):
        value = alert.get('started_at')
        return datetime.fromisoformat(value.replace('Z', '+00:00')) if isinstance(value, str) else value

    roots = [alert for alert in alerts if alert.get('state') == 'firing' and alert.get('entity_type') in ('gateway', 'wan')
             and alert.get('metric') in ('online', 'up') and alert.get('value') is not None and alert['value'] < .5]
    groups = {str(root['id']): {'id': str(root['id']), 'primary': root, 'related': [], 'basis': 'Same-site alerts started within 2 minutes of the primary connectivity alert; correlation, not proven causation'} for root in roots}
    for alert in alerts:
        if str(alert['id']) in groups:
            continue
        started = started_at(alert)
        matches = [root for root in roots if root['site'] == alert['site'] and started and started_at(root)
               and abs((started - started_at(root)).total_seconds()) <= 120]
        if len(matches) == 1:
            groups[str(matches[0]['id'])]['related'].append(alert)
        else:
            groups[str(alert['id'])] = {'id': str(alert['id']), 'primary': alert, 'related': [], 'basis': 'Independent alert'}
    return list(groups.values())


def routes(get_pool, current_user, get_sites, get_devices, get_alerts, get_tunnels) -> APIRouter:
    router = APIRouter(prefix="/api", tags=["Operations"])

    @router.get('/changes')
    async def changes(user=Depends(current_user)):
        sites = await get_sites(user)
        rows = await get_pool().fetch('SELECT * FROM configuration_changes WHERE site=ANY($1::text[]) ORDER BY ts DESC LIMIT 200', list(sites))
        return [{**dict(row), 'site_name': sites[row['site']]['description']} for row in rows]

    @router.get('/reliability')
    async def reliability(site: str | None = None, user=Depends(current_user)):
        sites = scoped_sites(await get_sites(user), site)
        rows = await get_pool().fetch("""SELECT m.entity_id, d.name, d.site,
            count(*) AS samples, count(*) FILTER (WHERE m.value>=0.5) AS online_samples,
            min(m.ts) AS first_sample, max(m.ts) AS last_sample FROM metrics m JOIN devices d ON m.entity_id='dev:' || d.mac
            WHERE d.site=ANY($1::text[]) AND m.site=d.site AND m.metric='online' AND m.ts>now()-interval '24 hours'
            GROUP BY m.entity_id,d.name,d.site ORDER BY d.name""", list(sites))
        outages = await get_pool().fetch("""SELECT device_mac, count(*) AS outages FROM events WHERE site=ANY($1::text[])
            AND event_type='DEVICE_OFFLINE' AND ts>now()-interval '30 days' GROUP BY device_mac""", list(sites))
        counts = {row['device_mac']: row['outages'] for row in outages}
        end = time.time()
        start = end - 86400
        runs = await get_pool().fetch("""WITH samples AS (
            SELECT entity_id, ts, CASE WHEN value>=0.5 THEN 'ONLINE' ELSE 'OFFLINE' END AS state,
              lag(ts) OVER w AS previous_ts, lag(value>=0.5) OVER w AS previous_online
            FROM metrics WHERE site=ANY($1::text[]) AND metric='online' AND entity_id LIKE 'dev:%'
              AND ts>=to_timestamp($2) AND ts<=to_timestamp($3) WINDOW w AS (PARTITION BY entity_id ORDER BY ts)
          ), marked AS (SELECT *, sum(CASE WHEN previous_ts IS NULL OR ts-previous_ts>make_interval(secs=>$4)
              OR (state='ONLINE') IS DISTINCT FROM previous_online THEN 1 ELSE 0 END)
              OVER(PARTITION BY entity_id ORDER BY ts) AS run FROM samples)
          SELECT entity_id, state, min(ts) AS first, max(ts) AS last FROM marked
          GROUP BY entity_id, state, run ORDER BY entity_id, first""", list(sites), start, end, settings.poll_interval * 3)
        by_entity = {}
        for run in runs:
            by_entity.setdefault(run['entity_id'], []).append(run)
        windows = await get_pool().fetch('SELECT * FROM maintenance_windows WHERE site=ANY($1::text[]) AND enabled', list(sites))
        scheduled = maintenance_occurrences(windows, start, end)
        known = {row['entity_id'] for row in rows}
        devices = await get_pool().fetch('SELECT mac, name, site FROM devices WHERE site=ANY($1::text[])', list(sites))
        rows = [*rows, *[{'entity_id': 'dev:' + device['mac'], 'name': device['name'], 'site': device['site'], 'samples': 0,
                'online_samples': 0, 'first_sample': None, 'last_sample': None} for device in devices if 'dev:' + device['mac'] not in known]]
        result = []
        for row in rows:
            intervals = observed_intervals(by_entity.get(row['entity_id'], []), start, end, settings.poll_interval)
            measured = sum(item['duration_s'] for item in intervals if item['state'] != 'UNKNOWN')
            result.append({**dict(row), 'site_name': sites[row['site']]['description'],
                           'observed_online_pct': round(100 * row['online_samples'] / row['samples'], 2) if row['samples'] else None,
                           'coverage_pct': round(min(100, measured / 864), 2), 'intervals': intervals,
                           'maintenance': [item for item in scheduled if item['site'] == row['site']],
                           'offline_events_30d': counts.get(row['entity_id'].removeprefix('dev:'), 0)})
        return result

    @router.get('/operations')
    async def operations(site: str | None = None, user=Depends(current_user)):
        sites = scoped_sites(await get_sites(user), site)
        devices = [device for device in await get_devices(user) if device['site'] in sites]
        alerts = [alert for alert in await get_alerts(user) if alert['site'] in sites]
        attention = []
        for alert in alerts:
            mac = entity_device_mac(alert['entity_id'])
            path = f"/device/{mac}" if mac else '/s2s?tunnel=' + alert['entity_id'].removeprefix('tunnel:') if alert['entity_type'] == 'tunnel' else '/wan?site=' + sites[alert['site']]['slug'] if alert['entity_type'] == 'wan' else '/alerts'
            attention.append({"id": f"alert:{alert['id']}", "severity": alert['severity'], "site": alert.get('site_name') or alert['site'],
                              "title": alert['message'], "path": path, "since": alert['started_at'], "kind": alert['kind']})
        capacity = []
        updates = []
        for device in devices:
            if device['freshness']['state'] != 'LIVE':
                attention.append({"id": f"stale:{device['mac']}", "severity": "warning", "site": device['site_name'],
                                  "title": f"{device['name']}: monitoring data is stale", "path": f"/device/{device['mac']}", "kind": "collection"})
            values = device.get('now', {})
            capacity.append({"mac": device['mac'], "name": device['name'], "site": device['site_name'],
                             "cpu": values.get('cpu_pct'), "memory": values.get('mem_pct'), "freshness": device['freshness']})
            if device.get('upgradable'):
                updates.append({"mac": device['mac'], "name": device['name'], "site": device['site_name'],
                                "current": device['version'], "available": device.get('upgrade_to')})
        clients = await get_pool().fetch("""SELECT c.name, c.mac, c.site, s.description AS site_name, s.slug, c.signal
            FROM clients c JOIN sites s ON s.name=c.site WHERE c.site=ANY($1::text[]) AND c.online AND NOT c.is_wired
            AND c.signal < -75 AND c.updated_at > now()-interval '90 seconds' ORDER BY c.signal LIMIT 20""", list(sites))
        for client in clients:
            attention.append({"id": f"signal:{client['site']}:{client['mac']}", "severity": "warning", "site": client['site_name'],
                              "title": f"{client['name']}: weak Wi-Fi signal ({client['signal']} dBm)",
                              "path": f"/client/{client['slug']}/{client['mac']}", "kind": "wifi"})
        rank = {"critical": 0, "warning": 1, "info": 2}
        attention.sort(key=lambda item: rank.get(item['severity'], 3))
        capacity.sort(key=lambda item: max(item['cpu'] or 0, item['memory'] or 0), reverse=True)
        incidents = correlated_incidents(alerts)
        for incident in incidents:
            incident['path'] = next((item['path'] for item in attention if item['id'] == f"alert:{incident['id']}"), '/alerts')
        return {"attention": attention, "capacity": capacity, "updates": updates, "incidents": incidents}

    @router.get('/wireless')
    async def wireless(site: str | None = None, user=Depends(current_user)):
        sites = scoped_sites(await get_sites(user), site)
        devices = await get_pool().fetch("SELECT mac, name, site, raw, last_poll FROM devices WHERE site=ANY($1::text[]) AND device_type='ap'", list(sites))
        radios = []
        for device in devices:
            config = {radio['radio']: radio for radio in device['raw'].get('radio_table', []) if radio.get('radio')}
            for radio in device['raw'].get('radio_table_stats', []):
                band = radio.get('radio')
                radios.append({"mac": device['mac'], "device": device['name'], "site": device['site'], "site_name": sites[device['site']]['description'],
                               "band": band, "channel": num(radio.get('channel')), "width": num(config.get(band, {}).get('ht')),
                               "utilization": num(radio.get('cu_total')), "retries": num(radio.get('tx_retries_pct')), "clients": num(radio.get('num_sta')),
                               "last_poll": device['last_poll'],
                               "freshness": 'LIVE' if device['last_poll'] and time.time()-device['last_poll'].timestamp() <= settings.poll_interval*3+5 else 'STALE'})
        for radio in radios:
            radio['cochannel_radios'] = sum(1 for other in radios if other['site'] == radio['site'] and other['band'] == radio['band']
                and radio['freshness']=='LIVE' and other['freshness']=='LIVE'
                and radio['channel'] is not None and other['channel'] == radio['channel'] and other['mac'] != radio['mac'])
        distribution = await get_pool().fetch("""SELECT site,
            count(*) FILTER(WHERE signal < -75) AS poor, count(*) FILTER(WHERE signal >= -75 AND signal < -60) AS fair,
            count(*) FILTER(WHERE signal >= -60) AS good FROM clients WHERE site=ANY($1::text[]) AND online AND NOT is_wired
            AND updated_at > now()-interval '90 seconds' GROUP BY site""", list(sites))
        return {"radios": radios, "signal": [{**dict(row), "site_name": sites[row['site']]['description']} for row in distribution]}

    @router.get('/search')
    async def search(q: str = Query(min_length=1, max_length=100), offset: int = Query(0, ge=0),
                     limit: int = Query(100, ge=1, le=200), user=Depends(current_user)):
        sites = await get_sites(user)
        term = q.strip().lower()
        if not term:
            return []
        pool = get_pool()
        tunnels = await get_tunnels(user)
        visible_tunnels = [f"tunnel:{item['id']}" for item in tunnels]

        async def matching(table, fields, order, condition='TRUE', values=()):
            return await pool.fetch(f"""SELECT item.* FROM {table} item JOIN sites s ON s.name=item.site
                WHERE item.site=ANY($1::text[]) AND ({condition})
                AND position($2 in lower(concat_ws(' ', {fields}, s.name, s.description, s.slug)))>0
                ORDER BY {order} LIMIT $3 OFFSET $4""", list(sites), term, limit, offset, *values)

        results = []

        def add(kind, item_id, label, site, path, detail=''):
            results.append({'id': f'{kind}:{item_id}', 'type': kind, 'label': label, 'site': site, 'path': path, 'detail': detail})

        identities = await pool.fetch('SELECT id::text, name, description, slug, selected FROM sites WHERE $1 OR selected ORDER BY description, id', is_owner(user))
        identity_matches = [item for item in identities if can_read_site(user, item['id'])
                            and term in ' '.join(str(item[field]) for field in ('name', 'description', 'slug')).lower()]
        for item in identity_matches[offset:offset+limit]:
            add('site', item['id'], item['description'], item['description'], f"/site/{item['slug']}" if item['selected'] else '/monitoring',
                'Monitored' if item['selected'] else 'Not monitored')
        for kind in ('gateway', 'switch', 'ap', 'device'):
            condition = "item.device_type=$5" if kind != 'device' else "item.device_type NOT IN ('gateway','switch','ap') AND $5='device'"
            for item in await matching('devices', 'item.name,item.mac,item.ip,item.model,item.model_name,item.version,item.device_type',
                                       'item.name,item.mac', condition, (kind,)):
                add(kind, item['mac'], item['name'] or item['mac'], sites[item['site']]['description'], f"/device/{item['mac']}",
                    ' · '.join(str(item[field]) for field in ('model_name', 'ip', 'mac') if item[field]))
        for item in await matching('clients', 'item.name,item.hostname,item.mac,item.ip,item.network,item.essid,item.vlan::text,item.oui', 'item.name,item.site,item.mac'):
            add('client', f"{item['site']}:{item['mac']}", item['name'] or item['hostname'] or item['mac'], sites[item['site']]['description'],
                f"/client/{sites[item['site']]['slug']}/{item['mac']}", ' · '.join(str(item[field]) for field in ('ip', 'mac', 'network', 'essid') if item[field]))
        event_condition = "TRUE" if is_owner(user) else "item.event_type NOT LIKE 'TUNNEL%' AND coalesce(item.metadata->>'entity','') NOT LIKE 'tunnel:%'"
        for item in await matching('events', 'item.message,item.title,item.device,item.device_mac,item.client,item.client_mac,item.event_type,item.category,item.severity,item.source', 'item.ts DESC,item.id', event_condition):
            add('event', item['id'], item['message'], sites[item['site']]['description'], f"/events?site={sites[item['site']]['slug']}",
                f"{item['ts'].isoformat()} · {item['event_type']} · {item['severity']}")
        alert_condition = "$5::boolean OR item.entity_type<>'tunnel' OR item.entity_id=ANY($6::text[])"
        for item in await matching('alerts', 'item.message,item.entity_name,item.entity_id,item.kind,item.metric,item.severity,item.state',
                       'item.started_at DESC,item.id', alert_condition, (is_owner(user), visible_tunnels)):
            add('alert', item['id'], item['message'], sites[item['site']]['description'], '/alerts', f"{item['severity']} · {item['state']} · {item['metric']}")
        for item in await matching('configuration_changes', 'item.device_name,item.device_mac,item.before::text,item.after::text', 'item.ts DESC,item.id'):
            fields = sorted(field for field in item['before'].keys() | item['after'].keys() if item['before'].get(field) != item['after'].get(field))
            add('change', item['id'], item['device_name'] or item['device_mac'], sites[item['site']]['description'], '/changes',
                ' · '.join(field.replace('_', ' ') for field in fields))
        for item in await matching('maintenance_windows', 'item.title,item.repeat,item.timezone', 'item.starts_at DESC,item.id'):
            add('maintenance', item['id'], item['title'], sites[item['site']]['description'], '/maintenance', f"{item['starts_at'].isoformat()} · {item['repeat']}")
        tunnel_matches = [item for item in tunnels if term in ' '.join(str(item.get(field) or '') for field in ('id', 'name', 'description')).lower()]
        for item in tunnel_matches[offset:offset+limit]:
            add('tunnel', item['id'], item.get('name') or item['id'], '', f"/s2s?tunnel={quote(str(item['id']), safe='')}")
        return results

    return router
def snapshot(device: dict) -> dict:
    uplink = device.get('uplink') or {}
    return {
        'firmware': device.get('version'),
        'uplink': {key: uplink.get(key) for key in ('uplink_mac', 'uplink_remote_port', 'speed', 'wan_ip')},
        'radios': sorted([{'band': radio.get('radio'), 'channel': radio.get('channel'), 'width': radio.get('ht')}
                          for radio in device.get('radio_table') or []], key=lambda radio: radio['band'] or ''),
        'ports': sorted([{key: port.get(key) for key in ('port_idx', 'portconf_id', 'forward', 'speed')}
                         for port in device.get('port_table') or []], key=lambda port: port['port_idx'] or 0),
    }


async def record_change(ctx, site: str, device: dict, name: str) -> None:
    current = snapshot(device)
    async with ctx.pool.acquire() as conn:
        async with conn.transaction():
            if not await conn.fetchval('SELECT selected FROM sites WHERE name=$1', site):
                return
            previous = await conn.fetchrow('SELECT value FROM device_snapshots WHERE device_mac=$1 FOR UPDATE', device['mac'])
            if previous and previous['value'] != current:
                await conn.execute('INSERT INTO configuration_changes (device_mac, device_name, site, before, after) VALUES ($1,$2,$3,$4,$5)',
                                   device['mac'], name, site, previous['value'], current)
            await conn.execute("""INSERT INTO device_snapshots (device_mac, site, value) VALUES ($1,$2,$3)
                ON CONFLICT (device_mac) DO UPDATE SET value=EXCLUDED.value, site=EXCLUDED.site, updated_at=now()""", device['mac'], site, current)
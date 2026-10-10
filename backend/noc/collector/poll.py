"""Controller polling: sites, devices, WAN, clients, events, SD-WAN config, topology."""
import logging
import re
import time
from datetime import timedelta
from uuid import UUID

from ..config import settings
from ..db import kv_get, kv_set
from ..migrations import bind_environment_connection
from ..unifi import UniFiClient, sanitize
from .store import MetricBuffer, Site, State, from_epoch, num, utcnow
from .changes import record_change

log = logging.getLogger("noc.poll")

GW_TYPES = {"ugw", "uxg", "udm", "uxgpro", "udmpro", "ucg"}
STATE_LABEL = {0: "offline", 1: "online", 2: "pending adoption", 4: "upgrading", 5: "provisioning", 6: "heartbeat missed",
               7: "adopting", 9: "adoption failed", 10: "isolated", 11: "rf scanning"}
ONLINE_STATES = {1, 4, 5}
NOISY_KEYS = {"udapi_version", "fw_caps", "fw2_caps", "fw3_caps", "hw_caps", "gw_caps", "anon_id", "hash_id", "hardware_uuid",
              "guest_token", "config_network", "ethernet_overrides", "dns_shield_server_list_hash", "ids_ips_signature",
              "ids_ips_last_known_signature", "port_overrides", "manufacturer_id", "stat", "connect_request_ip",
              "connect_request_port", "inform_url", "inform_ip", "dhcp_excluded_ip_list", "fingerprint_override"}
SEVERITY = {"VERY_HIGH": "critical", "CRITICAL": "critical", "HIGH": "high", "MEDIUM": "medium", "LOW": "low"}
BAND = {"ng": "2.4 GHz", "na": "5 GHz", "6e": "6 GHz"}


def device_kind(d: dict) -> str:
    t = (d.get("type") or "").lower()
    if t in GW_TYPES:
        return "gateway"
    return {"usw": "switch", "uap": "ap"}.get(t, "other")


def device_temp(d: dict):
    vals = []
    for t in d.get("temperatures") or []:
        v = num(t.get("value"))
        if v is None:
            continue
        vals.append(v / 1000.0 if v > 1000 else v)
    gt = num(d.get("general_temperature"))
    if gt is not None and (d.get("has_temperature") or gt > 0):
        vals.append(gt)
    return max(vals) if vals else None


def slugify(desc: str, taken: set[str]) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", desc.lower().split()[-1] if desc.split() else desc.lower()).strip("-") or "site"
    slug, i = base, 2
    while slug in taken:
        slug, i = f"{base}{i}", i + 1
    return slug


class Ctx:
    def __init__(self, state: State, pool, unifi: UniFiClient):
        self.state = state
        self.pool = pool
        self.unifi = unifi
        self.flags: dict = {}

    async def event(self, site: str, event_type: str, message: str, severity: str = "info", device: str | None = None,
                    device_mac: str | None = None, device_type: str | None = None, metadata: dict | None = None, ts=None):
        ts = ts or utcnow()
        eid = f"noc-{event_type}-{site}-{device_mac or ''}-{int(ts.timestamp() * 1000)}"
        await self.pool.execute(
            """INSERT INTO events (id, ts, site, severity, device, device_mac, device_type, event_type, category, title, message, source, metadata)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,'NOC',$9,$10,'noc',$11) ON CONFLICT DO NOTHING""",
            eid, ts, site, severity, device, device_mac, device_type, event_type, event_type.replace("_", " ").title(), message, metadata or {},
        )


# ---------------------------------------------------------------- sites
async def discover_sites(ctx: Ctx) -> None:
    legacy, integ = await ctx.unifi.sites()
    async with ctx.pool.acquire() as conn:
        connection_id = await bind_environment_connection(conn)
    uuid_by_name = {s.get("internalReference"): s.get("id") for s in integ}
    existing = {s['name']: s for s in await ctx.pool.fetch("SELECT name, site_id, slug, selected FROM sites")}
    taken: set[str] = {s['slug'] for s in existing.values()}
    found: dict[str, Site] = {}
    for s in legacy:
        previous = existing.get(s['name'])
        if previous and previous['site_id'] != s['_id']:
            log.error("site identity changed for %s; refusing automatic adoption", s['name'])
            continue
        slug = previous['slug'] if previous else slugify(s["desc"], taken)
        taken.add(slug)
        site = Site(s["name"], s["_id"], uuid_by_name.get(s["name"]), s["desc"], slug)
        selected = bool(previous and previous['selected'])
        tz = None
        if selected:
            try:
                tz = (await ctx.unifi.sysinfo(site.name)).get("timezone")
            except Exception:
                pass
        await ctx.pool.execute(
            """INSERT INTO sites (name, site_id, uuid, description, slug, timezone, connection_id, updated_at) VALUES ($1,$2,$3,$4,$5,$6,$7::uuid,now())
               ON CONFLICT (name) DO UPDATE SET site_id=EXCLUDED.site_id, uuid=EXCLUDED.uuid, description=EXCLUDED.description,
               slug=EXCLUDED.slug, timezone=coalesce(EXCLUDED.timezone, sites.timezone), connection_id=EXCLUDED.connection_id, updated_at=now()""",
            site.name, site.site_id, site.uuid, site.description, site.slug, tz, UUID(connection_id),
        )
        if selected:
            found[site.name] = site
    missing = {s['name'] for s in existing.values() if s['selected']} - set(found)
    ctx.state.sites = found
    ctx.state.by_site_id = {s.site_id: n for n, s in found.items()}
    await kv_set(ctx.pool, "sites.missing", sorted(missing))
    try:
        await kv_set(ctx.pool, "controller.info", {"version": (await ctx.unifi.info()).get("applicationVersion"), "url": settings.controller_url})
    except Exception as exc:  # noqa: BLE001
        log.warning("controller info: %s", exc)
    log.info("monitoring sites: %s", ", ".join(f"{s.description} ({n})" for n, s in found.items()))


async def refresh_selection(ctx: Ctx) -> None:
    rows = await ctx.pool.fetch("SELECT name, site_id, uuid, description, slug FROM sites WHERE selected")
    found = {row['name']: Site(row['name'], row['site_id'], row['uuid'], row['description'], row['slug']) for row in rows}
    removed = set(ctx.state.sites) - set(found)
    removed = (set(ctx.flags.get("unselected_sites", [])) | removed) - set(found)
    ctx.flags["unselected_sites"] = sorted(removed)
    ctx.state.sites = found
    ctx.state.by_site_id = {site.site_id: name for name, site in found.items()}
    if removed:
        ctx.state.devices = {mac: device for mac, device in ctx.state.devices.items() if device.get('_site') not in removed}
        ctx.state.current = {key: value for key, value in ctx.state.current.items() if value.site not in removed}
        ctx.state.tunnels = {key: tunnel for key, tunnel in ctx.state.tunnels.items()
                             if all(member.get('site') in found for member in tunnel.get('members', []))}
        ctx.state.health = {key: value for key, value in ctx.state.health.items()
                    if key not in removed and (not key.startswith('tunnel:') or key.removeprefix('tunnel:') in ctx.state.tunnels)}
        ctx.state.sources = {key: value for key, value in ctx.state.sources.items()
                     if not any(key.endswith('.' + site) for site in removed)}
        ctx.state.current = {key: value for key, value in ctx.state.current.items()
                     if not any(value.entity_id.endswith('.' + site) for site in removed)}


async def process_site_refresh(ctx: Ctx) -> None:
    job = await kv_get(ctx.pool, 'sites.refresh_job')
    if not job:
        return
    await kv_set(ctx.pool, 'sites.refresh_state', {'state': 'checking', 'job_id': job['id']})
    try:
        await discover_sites(ctx)
        await kv_set(ctx.pool, 'sites.refresh_state', {'state': 'complete', 'job_id': job['id'], 'completed_at': time.time()})
    except Exception as error:
        await kv_set(ctx.pool, 'sites.refresh_state', {'state': 'error', 'job_id': job['id'], 'error': f'{type(error).__name__}: {error}'[:500]})
    finally:
        await ctx.pool.execute("DELETE FROM kv_state WHERE key='sites.refresh_job' AND value->>'id'=$1", job['id'])


# ---------------------------------------------------------------- devices + WAN
async def poll_devices(ctx: Ctx, site: str, buf: MetricBuffer) -> None:
    st = ctx.state
    now = utcnow()
    devices = await ctx.unifi.legacy(site, "stat/device")
    health = await ctx.unifi.legacy(site, "stat/health")
    hmap = {h.get("subsystem"): sanitize(h) for h in health}
    await kv_set(ctx.pool, f"unifi_health.{site}", hmap)

    seen = []
    counts = {"gateway": [0, 0], "switch": [0, 0], "ap": [0, 0], "other": [0, 0]}
    for d in devices:
        mac = d.get("mac")
        if not mac:
            continue
        seen.append(mac)
        kind = device_kind(d)
        name = d.get("name") or d.get("model") or mac
        state_code = d.get("state")
        online = state_code in ONLINE_STATES
        counts[kind][0 if online else 1] += 1
        eid = f"dev:{mac}"
        stats = d.get("system-stats") or {}
        sys_stats = d.get("sys_stats") or {}
        cpu, mem = num(stats.get("cpu")), num(stats.get("mem"))
        temp = device_temp(d)
        uplink = d.get("uplink") or {}

        # transition events
        prev = st.devices.get(mac)
        if prev is not None and prev.get("_online") != online:
            sev = "high" if kind in ("gateway", "switch") else "medium"
            await ctx.event(site, "DEVICE_OFFLINE" if not online else "DEVICE_ONLINE",
                            f"{name} is {'offline' if not online else 'back online'} (state: {STATE_LABEL.get(state_code, state_code)})",
                            sev if not online else "info", name, mac, kind)
        if prev is not None and prev.get("version") and d.get("version") and prev.get("version") != d.get("version"):
            await ctx.event(site, "FIRMWARE_CHANGED", f"{name} firmware {prev.get('version')} → {d.get('version')}", "info", name, mac, kind)

        buf.add(now, site, kind, eid, "online", 1 if online else 0)
        st.put_current(site, kind, eid, name, "online", 1 if online else 0)
        if online:
            for metric, val in (("cpu_pct", cpu), ("mem_pct", mem), ("temp_c", temp), ("uptime_s", num(d.get("uptime"))),
                                ("load1", num(sys_stats.get("loadavg_1"))), ("num_sta", num(d.get("num_sta"))),
                                ("satisfaction", num(d.get("satisfaction")))):
                if val is not None and not (metric == "satisfaction" and val < 0):
                    buf.add(now, site, kind, eid, metric, val)
                    st.put_current(site, kind, eid, name, metric, val)
            rx, tx = num(uplink.get("rx_bytes-r")), num(uplink.get("tx_bytes-r"))
            if kind != "gateway" and rx is not None and tx is not None:
                buf.add(now, site, kind, eid, "uplink_rx_bps", rx * 8)
                buf.add(now, site, kind, eid, "uplink_tx_bps", tx * 8)

        if kind == "gateway" and online:
            await _gateway_wan(ctx, site, d, name, hmap, buf, now)
        if online:
            await _ports(ctx, site, d, kind, name, buf, now)
        if kind == "ap" and online:
            _radios(st, site, d, name, buf, now)

        raw = {k: v for k, v in sanitize(d).items() if k not in NOISY_KEYS}
        raw["_state_label"] = STATE_LABEL.get(state_code, str(state_code))
        st.devices[mac] = {**raw, "_site": site, "_kind": kind, "_online": online, "_name": name}
        await ctx.pool.execute(
            """INSERT INTO devices (mac, site, name, device_type, model, model_name, version, ip, state, online, adopted, uptime, last_seen,
                   last_poll, uplink_mac, uplink_port, upgradable, upgrade_to, raw)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,now(),$14,$15,$16,$17,$18)
               ON CONFLICT (mac) DO UPDATE SET site=EXCLUDED.site, name=EXCLUDED.name, device_type=EXCLUDED.device_type, model=EXCLUDED.model,
                   model_name=EXCLUDED.model_name, version=EXCLUDED.version, ip=EXCLUDED.ip, state=EXCLUDED.state, online=EXCLUDED.online,
                   adopted=EXCLUDED.adopted, uptime=EXCLUDED.uptime, last_seen=EXCLUDED.last_seen, last_poll=now(),
                   uplink_mac=EXCLUDED.uplink_mac, uplink_port=EXCLUDED.uplink_port, upgradable=EXCLUDED.upgradable,
                   upgrade_to=EXCLUDED.upgrade_to, raw=EXCLUDED.raw""",
            mac, site, name, kind, d.get("model"), d.get("model_name") or d.get("shortname"), d.get("version"), d.get("ip"),
            state_code, online, d.get("adopted"), int(num(d.get("uptime")) or 0), from_epoch(d.get("last_seen")),
            uplink.get("uplink_mac"), uplink.get("uplink_remote_port"), d.get("upgradable"), d.get("upgrade_to_firmware"), raw,
        )
        if online:
            await record_change(ctx, site, d, name)
    if seen:
        gone = await ctx.pool.fetch("SELECT mac, name FROM devices WHERE site=$1 AND NOT (mac = ANY($2::text[]))", site, seen)
        for g in gone:
            await ctx.event(site, "DEVICE_REMOVED", f"{g['name']} is no longer reported by the controller", "medium", g["name"], g["mac"])
            await ctx.pool.execute("DELETE FROM devices WHERE mac=$1", g["mac"])
            st.devices.pop(g["mac"], None)

    sid = f"site:{site}"
    for kind, (on, off) in counts.items():
        if kind == "other" and not (on or off):
            continue
        buf.add(now, site, "site", sid, f"{kind}_online", on)
        buf.add(now, site, "site", sid, f"{kind}_offline", off)
        st.put_current(site, "site", sid, st.sites[site].description, f"{kind}_offline", off)
    wlan = hmap.get("wlan", {})
    lan = hmap.get("lan", {})
    for metric, val in (("wlan_users", wlan.get("num_user")), ("lan_users", lan.get("num_user")),
                        ("wlan_rx_bps", (num(wlan.get("rx_bytes-r")) or 0) * 8 if wlan else None),
                        ("wlan_tx_bps", (num(wlan.get("tx_bytes-r")) or 0) * 8 if wlan else None)):
        buf.add(now, site, "site", sid, metric, val)


async def _gateway_wan(ctx: Ctx, site: str, d: dict, name: str, hmap: dict, buf: MetricBuffer, now) -> None:
    st = ctx.state
    uplink = d.get("uplink") or {}
    wan_name = uplink.get("comment") or "WAN"
    eid = f"wan:{site}:{wan_name}"
    label = f"{st.sites[site].description} {wan_name}"
    alive = [bool(v.get("alive")) for v in (d.get("last_wan_interfaces") or {}).values()]
    www = hmap.get("www", {})
    up = 1 if (uplink.get("up") and (all(alive) if alive else True)) else 0
    rx, tx = num(uplink.get("rx_bytes-r")), num(uplink.get("tx_bytes-r"))
    us = (d.get("uptime_stats") or {}).get(wan_name) or (d.get("uptime_stats") or {}).get("WAN") or {}
    values = {
        "up": up,
        "latency_ms": num(uplink.get("latency")) if up else None,
        "rx_bps": rx * 8 if rx is not None else None,
        "tx_bps": tx * 8 if tx is not None else None,
        "availability_24h": num(us.get("availability")),
        "latency_avg_24h": num(us.get("latency_average")),
        "drops": num(www.get("drops")),
        "uptime_s": num(uplink.get("uptime")),
    }
    phys = d.get("wan1") or {}
    errs = st.rate(eid + ":phys", {"err": (num(phys.get("rx_errors")) or 0) + (num(phys.get("tx_errors")) or 0),
                                   "drop": (num(phys.get("rx_dropped")) or 0) + (num(phys.get("tx_dropped")) or 0)}) if phys else {}
    if "err" in errs:
        values["err_per_min"] = errs["err"] * 60
        values["drop_per_min"] = errs["drop"] * 60
    if values["rx_bps"] is not None and num(uplink.get("max_speed")):
        values["util_pct"] = max(values["rx_bps"], values["tx_bps"] or 0) / (num(uplink["max_speed"]) * 1e6) * 100
    for metric, val in values.items():
        if val is not None:
            buf.add(now, site, "wan", eid, metric, val)
            st.put_current(site, "wan", eid, label, metric, val)
    for mon in us.get("monitors") or []:
        mid = f"wanmon:{site}:{mon.get('target')}:{mon.get('type')}"
        buf.add(now, site, "wan_monitor", mid, "latency_ms", mon.get("latency_average"))
        buf.add(now, site, "wan_monitor", mid, "availability", mon.get("availability"))

    ip = uplink.get("ip")
    ctx.flags[f"wanif:{site}"] = {"entity_id": eid, "ifname": uplink.get("name"), "label": label, "gateway_mac": d.get("mac"), "gateway": name}
    key = f"wan:{site}"
    prev = ctx.flags.get(key)
    if prev is not None:
        if prev["up"] != up:
            if not up:
                await ctx.event(site, "WAN_DOWN", f"{label} is DOWN", "critical", name, d.get("mac"), "gateway")
                ctx.flags[key + ":down_since"] = time.time()
            else:
                since = ctx.flags.pop(key + ":down_since", None)
                dur = f" after {int(time.time() - since)} s" if since else ""
                await ctx.event(site, "WAN_UP", f"{label} is back UP{dur}", "info", name, d.get("mac"), "gateway",
                                {"outage_s": int(time.time() - since) if since else None})
        if ip and prev.get("ip") and ip != prev["ip"]:
            await ctx.event(site, "WAN_IP_CHANGED", f"{label} public IP changed {prev['ip']} → {ip}", "medium", name, d.get("mac"), "gateway")
    ctx.flags[key] = {"up": up, "ip": ip or (prev or {}).get("ip")}
    geo = (d.get("active_geo_info") or {}).get(wan_name) or (d.get("active_geo_info") or {}).get("WAN") or {}
    await kv_set(ctx.pool, f"wan.{site}", {
        "entity_id": eid, "name": label, "wan_name": wan_name, "gateway_mac": d.get("mac"), "gateway": name, "ip": ip,
        "up": bool(up), "isp": geo.get("isp_name") or hmap.get("wan", {}).get("isp_name"), "asn": geo.get("asn"),
        "city": geo.get("city"), "ifname": uplink.get("name"), "physical_speed": phys.get("speed"), "max_speed": uplink.get("max_speed"),
        "monitors": us.get("monitors") or [], "alerting_monitors": us.get("alerting_monitors") or [], "time_period": us.get("time_period"),
        "health_status": hmap.get("wan", {}).get("status"), "www_status": hmap.get("www", {}).get("status"),
        "speedtest": {k: d.get("speedtest-status", {}).get(k) for k in ("xput_download", "xput_upload", "latency", "rundate")},
        "ts": time.time(),
    })


async def _ports(ctx: Ctx, site: str, d: dict, kind: str, dname: str, buf: MetricBuffer, now) -> None:
    st = ctx.state
    mac = d["mac"]
    poe_total = 0.0
    poe_seen = False
    for p in d.get("port_table") or []:
        idx = p.get("port_idx")
        if idx is None:
            continue
        eid = f"port:{mac}:{idx}"
        pname = f"{dname} / {p.get('name') or f'Port {idx}'}"
        up = 1 if p.get("up") else 0
        speed = num(p.get("speed")) or 0
        rx, tx = num(p.get("rx_bytes-r")), num(p.get("tx_bytes-r"))
        vals = {"up": up}
        if up:
            vals["speed_mbps"] = speed
            if rx is not None and tx is not None:
                vals["rx_bps"], vals["tx_bps"] = rx * 8, tx * 8
                if speed > 0:
                    vals["util_pct"] = max(rx, tx) * 8 / (speed * 1e6) * 100
        counters = {}
        for metric, rx_key, tx_key in (("err", "rx_errors", "tx_errors"), ("drop", "rx_dropped", "tx_dropped")):
            rx_count, tx_count = num(p.get(rx_key)), num(p.get(tx_key))
            if rx_count is not None and tx_count is not None:
                counters[metric] = rx_count + tx_count
        r = st.rate(eid + ":ctr", counters)
        if "err" in r:
            vals["err_per_min"] = r["err"] * 60
        if "drop" in r:
            vals["drop_per_min"] = r["drop"] * 60
        dl = st.delta(eid + ":flap", {"flap": num(p.get("link_down_count")), "stp": num(p.get("stp_state_change_count"))})
        if "flap" in dl:
            st.window_add(eid + ":flaps", dl["flap"])
            vals["flaps_15m"] = sum(st.window(eid + ":flaps", 900))
        if p.get("port_poe") and p.get("poe_enable"):
            pw = num(p.get("poe_power"))
            if pw is not None:
                vals["poe_w"] = pw
                poe_total += pw
                poe_seen = True
        for metric, val in vals.items():
            buf.add(now, site, "port", eid, metric, val)
            st.put_current(site, "port", eid, pname, metric, val)
        fk = f"port:{eid}"
        prev_up = ctx.flags.get(fk)
        if prev_up is not None and prev_up != up and kind == "switch":
            sev = "medium" if p.get("is_uplink") else "low"
            await ctx.event(site, "PORT_DOWN" if not up else "PORT_UP", f"{pname} link {'down' if not up else 'up'}"
                            + (f" ({int(speed)} Mbps)" if up and speed else ""), sev if not up else "info", dname, mac, kind, {"port": idx})
        ctx.flags[fk] = up
    if poe_seen:
        eid = f"dev:{mac}"
        buf.add(now, site, kind, eid, "poe_w", poe_total)
        budget = num(d.get("total_max_power"))
        if budget:
            pct = poe_total / budget * 100
            buf.add(now, site, kind, eid, "poe_used_pct", pct)
            st.put_current(site, kind, eid, dname, "poe_used_pct", pct)


def _radios(st: State, site: str, d: dict, dname: str, buf: MetricBuffer, now) -> None:
    mac = d["mac"]
    for r in d.get("radio_table_stats") or []:
        radio = r.get("radio")
        if not radio:
            continue
        eid = f"radio:{mac}:{radio}"
        rname = f"{dname} {BAND.get(radio, radio)}"
        total, srx, stx = num(r.get("cu_total")), num(r.get("cu_self_rx")), num(r.get("cu_self_tx"))
        vals = {
            "cu_total": total, "cu_self_rx": srx, "cu_self_tx": stx,
            "cu_other": max(total - srx - stx, 0) if total is not None and srx is not None and stx is not None else None,
            "tx_retries_pct": num(r.get("tx_retries_pct")), "num_sta": num(r.get("num_sta")),
            "channel": num(r.get("channel")), "tx_power": num(r.get("tx_power")),
            "satisfaction": num(r.get("satisfaction")) if (num(r.get("satisfaction")) or -1) >= 0 else None,
        }
        for metric, val in vals.items():
            if val is not None:
                buf.add(now, site, "radio", eid, metric, val)
                st.put_current(site, "radio", eid, rname, metric, val)


# ---------------------------------------------------------------- clients
async def poll_clients(ctx: Ctx, site: str, buf: MetricBuffer) -> None:
    st = ctx.state
    now = utcnow()
    rows = await ctx.unifi.legacy(site, "stat/sta")
    macs, wifi, wired, poor = [], 0, 0, 0
    for c in rows:
        mac = c.get("mac")
        if not mac:
            continue
        macs.append(mac)
        is_wired = bool(c.get("is_wired"))
        wired += is_wired
        wifi += not is_wired
        signal = num(c.get("signal"))
        if not is_wired and signal is not None and signal < -75:
            poor += 1
        name = c.get("name") or c.get("hostname") or c.get("oui") or mac
        eid = f"client:{site}:{mac}"
        rx, tx = num(c.get("rx_bytes-r")), num(c.get("tx_bytes-r"))
        vals = {"rx_bps": rx * 8 if rx is not None else None, "tx_bps": tx * 8 if tx is not None else None,
                "satisfaction": num(c.get("satisfaction")) if (num(c.get("satisfaction")) or -1) >= 0 else None}
        if not is_wired:
            vals.update({"signal_dbm": signal, "rssi": num(c.get("rssi")), "noise_dbm": num(c.get("noise")),
                         "tx_rate_kbps": num(c.get("tx_rate")), "rx_rate_kbps": num(c.get("rx_rate")),
                         "retries_pct": num(c.get("wifi_tx_retries_percentage"))})
        for metric, val in vals.items():
            if val is not None:
                buf.add(now, site, "client", eid, metric, val)
                if metric in ("signal_dbm", "retries_pct"):
                    st.put_current(site, "client", eid, name, metric, val)
        raw = sanitize({k: c.get(k) for k in ("mac", "name", "hostname", "ip", "oui", "is_wired", "network", "network_id", "essid", "radio",
                                              "radio_proto", "channel", "channel_width", "ap_mac", "sw_mac", "sw_port", "signal", "rssi",
                                              "noise", "tx_rate", "rx_rate", "tx_bytes", "rx_bytes", "satisfaction", "uptime", "os_name",
                                              "dev_vendor", "dev_family", "nss", "is_11r", "wired_rate_mbps", "uplink_mac", "last_uplink_name",
                                              "wifi_tx_retries_percentage", "tx_retries", "powersave_enabled", "fixed_ip", "use_fixedip",
                                              "gw_mac", "is_guest", "qos_policy_applied", "anomalies", "satisfaction_reason") if k in c})
        await ctx.pool.execute(
            """INSERT INTO clients (site, mac, name, hostname, ip, oui, is_wired, online, network, vlan, essid, radio, channel, ap_mac, sw_mac,
                   sw_port, signal, rssi, noise, first_seen, last_seen, assoc_time, uptime, dev_category, raw, updated_at)
               VALUES ($1,$2,$3,$4,$5,$6,$7,true,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20,$21,$22,$23,$24,now())
               ON CONFLICT (site, mac) DO UPDATE SET name=EXCLUDED.name, hostname=EXCLUDED.hostname, ip=EXCLUDED.ip, oui=EXCLUDED.oui,
                   is_wired=EXCLUDED.is_wired, online=true, network=EXCLUDED.network, vlan=EXCLUDED.vlan, essid=EXCLUDED.essid,
                   radio=EXCLUDED.radio, channel=EXCLUDED.channel, ap_mac=EXCLUDED.ap_mac, sw_mac=EXCLUDED.sw_mac, sw_port=EXCLUDED.sw_port,
                   signal=EXCLUDED.signal, rssi=EXCLUDED.rssi, noise=EXCLUDED.noise, first_seen=coalesce(EXCLUDED.first_seen, clients.first_seen),
                   last_seen=EXCLUDED.last_seen, assoc_time=EXCLUDED.assoc_time, uptime=EXCLUDED.uptime, dev_category=EXCLUDED.dev_category,
                   raw=EXCLUDED.raw, updated_at=now()""",
            site, mac, name, c.get("hostname"), c.get("ip") or c.get("last_ip"), c.get("oui"), is_wired, c.get("network"),
            int(c["vlan"]) if isinstance(c.get("vlan"), (int, float)) else None, c.get("essid"), c.get("radio"),
            int(c["channel"]) if isinstance(c.get("channel"), (int, float)) else None, c.get("ap_mac"), c.get("sw_mac"),
            int(c["sw_port"]) if isinstance(c.get("sw_port"), (int, float)) else None,
            int(signal) if signal is not None else None, int(num(c.get("rssi"))) if num(c.get("rssi")) is not None else None,
            int(num(c.get("noise"))) if num(c.get("noise")) is not None else None, from_epoch(c.get("first_seen")),
            from_epoch(c.get("last_seen")), from_epoch(c.get("assoc_time")), int(num(c.get("uptime")) or 0),
            int(c["dev_cat"]) if isinstance(c.get("dev_cat"), (int, float)) else None, raw,
        )
    await ctx.pool.execute("UPDATE clients SET online=false, updated_at=now() WHERE site=$1 AND online AND NOT (mac = ANY($2::text[]))", site, macs)
    sid = f"site:{site}"
    for metric, val in (("clients_total", len(macs)), ("clients_wifi", wifi), ("clients_wired", wired), ("clients_poor_signal", poor)):
        buf.add(now, site, "site", sid, metric, val)
        st.put_current(site, "site", sid, st.sites[site].description, metric, val)


async def poll_known_clients(ctx: Ctx, site: str) -> None:
    rows = await ctx.unifi.legacy(site, "rest/user")
    for u in rows:
        mac = u.get("mac")
        if not mac:
            continue
        await ctx.pool.execute(
            """INSERT INTO clients (site, mac, name, hostname, oui, is_wired, online, first_seen, last_seen, raw)
               VALUES ($1,$2,$3,$4,$5,$6,false,$7,$8,'{}'::jsonb)
               ON CONFLICT (site, mac) DO UPDATE SET name=coalesce(EXCLUDED.name, clients.name),
                   last_seen=greatest(clients.last_seen, EXCLUDED.last_seen) WHERE NOT clients.online""",
            site, mac, u.get("name") or u.get("hostname") or u.get("oui") or mac, u.get("hostname"), u.get("oui"),
            u.get("is_wired"), from_epoch(u.get("first_seen")), from_epoch(u.get("last_seen")),
        )


# ---------------------------------------------------------------- events
def _render(template: str | None, params: dict) -> str:
    def rep(m):
        p = params.get(m.group(1))
        if isinstance(p, dict):
            return str(p.get("name") or p.get("hostname") or p.get("id") or m.group(0))
        return str(p) if p is not None else m.group(0)
    return re.sub(r"\{([A-Z0-9_]+)\}", rep, template or "")


async def poll_events(ctx: Ctx, site: str) -> int:
    last = await kv_get(ctx.pool, f"events.cursor.{site}")
    now_ms = int(time.time() * 1000)
    start = max(int(last) - 120_000, now_ms - 7 * 86400_000) if last else now_ms - 24 * 3600_000
    inserted, newest, page = 0, int(last or 0), 0
    while page < 15:
        d = await ctx.unifi.v2(site, "system-log/all", {"timestampFrom": start, "timestampTo": now_ms, "pageSize": 200, "pageNumber": page})
        for e in d.get("data", []):
            params = e.get("parameters") or {}
            dev = params.get("DEVICE") or params.get("DEVICE_TO") or params.get("GATEWAY") or {}
            cli = params.get("CLIENT") or {}
            dev_mac = dev.get("id") if isinstance(dev, dict) else None
            ts = from_epoch(e.get("timestamp")) or utcnow()
            newest = max(newest, int(e.get("timestamp") or 0))
            kind = ctx.state.devices.get(dev_mac or "", {}).get("_kind")
            res = await ctx.pool.execute(
                """INSERT INTO events (id, ts, site, severity, device, device_mac, device_type, event_type, category, title, message, client, client_mac, source, metadata)
                   VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,'unifi',$14) ON CONFLICT DO NOTHING""",
                e.get("id") or f"u-{site}-{e.get('timestamp')}-{e.get('key')}", ts, site, SEVERITY.get(e.get("severity") or "", "info"),
                dev.get("name") if isinstance(dev, dict) else None, dev_mac, kind,
                e.get("event") or e.get("key") or "UNKNOWN", e.get("category"), _render(e.get("title_raw"), params),
                _render(e.get("message_raw"), params) or e.get("key") or "", (cli.get("name") or cli.get("hostname")) if isinstance(cli, dict) else None,
                cli.get("id") if isinstance(cli, dict) else None,
                {"key": e.get("key"), "subcategory": e.get("subcategory"), "type": e.get("type"), "status": e.get("status"),
                 "parameters": sanitize(params)},
            )
            inserted += res.endswith(" 1")
        if page + 1 >= int(d.get("total_page_count") or 0):
            break
        page += 1
    if newest:
        await kv_set(ctx.pool, f"events.cursor.{site}", newest)
    return inserted


async def event_rates(ctx: Ctx, site: str, buf: MetricBuffer) -> None:
    row = await ctx.pool.fetchrow(
        """SELECT count(*) FILTER (WHERE event_type LIKE 'CLIENT_DISCONNECTED%') AS disc,
                  count(*) FILTER (WHERE event_type = 'CLIENT_ROAMED') AS roam,
                  count(*) FILTER (WHERE event_type LIKE 'CLIENT_CONNECTED%') AS conn
           FROM events WHERE site=$1 AND source='unifi' AND ts > now() - interval '15 minutes'""", site)
    now = utcnow()
    sid = f"site:{site}"
    for metric, val in (("disconnects_15m", row["disc"]), ("roams_15m", row["roam"]), ("connects_15m", row["conn"])):
        buf.add(now, site, "site", sid, metric, val)
        ctx.state.put_current(site, "site", sid, ctx.state.sites[site].description, metric, val)


# ---------------------------------------------------------------- SD-WAN / site-to-site
async def poll_s2s(ctx: Ctx, buf: MetricBuffer) -> None:
    st = ctx.state
    tunnels: dict[str, dict] = {}
    for site in st.sites:
        data = await ctx.unifi.v2(site, "magicsitetositevpn/configs")
        for cfg in data or []:
            routers = {}
            for dev in cfg.get("devices") or []:
                for s in dev.get("sites") or []:
                    nets = s.get("networks") or []
                    routers[s.get("siteId")] = {"router_ip": nets[0].get("routerIp") if nets else None,
                                                "subnets": [n.get("subnet") for n in nets], "networks": [n.get("networkName") for n in nets]}
            for conn in cfg.get("connections") or []:
                members = conn.get("sites") or []
                names = [st.by_site_id.get(m.get("siteId")) for m in members]
                if not members or any(n is None for n in names):
                    continue  # a tunnel to any non-monitored site is out of scope
                t = tunnels.setdefault(conn["connectionId"], {
                    "id": conn["connectionId"], "name": cfg.get("name"), "technology": "UniFi Site Magic SD-WAN (WireGuard mesh)",
                    "config_id": cfg.get("id"), "tunnel_subnet": cfg.get("tunnelSubnet"), "paused": cfg.get("pause"),
                    "members": [], "status": {}})
                if not t["members"]:
                    for m, n in zip(members, names):
                        t["members"].append({"site": n, "site_desc": st.sites[n].description, "tunnel_ip": (m.get("tunnelIp") or "").split("/")[0],
                                             "wan_ip": m.get("wanIp"), "port": m.get("port"), **routers.get(m.get("siteId"), {})})
            for sst in cfg.get("status") or []:
                sname = st.by_site_id.get(sst.get("siteId"))
                if not sname:
                    continue
                for c in sst.get("connections") or []:
                    if c.get("connectionId") in tunnels:
                        tunnels[c["connectionId"]]["status"][sname] = {
                            "connected": bool(c.get("connected")), "association_time": c.get("associationTime"),
                            "errors": c.get("errors") or [], "warnings": sst.get("warnings") or [], "site_errors": sst.get("errors") or []}
    now = utcnow()
    local = st.probe_origin(settings.probe_local_site)
    for tid, t in tunnels.items():
        statuses = t["status"].values()
        complete = all(member['site'] in t['status'] for member in t['members'])
        t["connected"] = False if t['paused'] or any(not status['connected'] for status in statuses) else True if complete else None
        t["local_site"] = local if any(m["site"] == local for m in t["members"]) else t["members"][0]["site"]
        t["remote_site"] = next((m["site"] for m in t["members"] if m["site"] != t["local_site"]), None)
        eid = f"tunnel:{tid}"
        label = " ↔ ".join(st.sites[m["site"]].description for m in t["members"])
        t["label"] = label
        connected_value = None if t['connected'] is None else int(t['connected'])
        buf.add(now, t["local_site"], "tunnel", eid, "connected", connected_value)
        st.put_current(t["local_site"], "tunnel", eid, label, "connected", connected_value)
        prev = st.tunnels.get(tid)
        if prev:
            if prev.get("connected") is not None and t['connected'] is not None and prev.get("connected") != t["connected"]:
                await ctx.event(t["local_site"], "TUNNEL_DOWN" if not t["connected"] else "TUNNEL_UP",
                                f"Site-to-site {label} {'DISCONNECTED' if not t['connected'] else 'connected'} (controller status)",
                                "critical" if not t["connected"] else "info", metadata={"tunnel": tid})
            for sname, s in t["status"].items():
                ps = prev.get("status", {}).get(sname)
                # The controller recomputes associationTime with +-30 s jitter on every read; only a forward jump is a re-association.
                if ps and ps.get("association_time") and s.get("association_time") and s["association_time"] - ps["association_time"] > 120:
                    st.window_add(eid + ":reconnects", 1)
                    await ctx.event(sname, "TUNNEL_RECONNECT", f"Site-to-site {label} re-associated ({st.sites[sname].description})",
                                    "medium", metadata={"tunnel": tid, "previous": ps["association_time"], "current": s["association_time"]})
            pm = {m["site"]: m for m in prev.get("members", [])}
            for m in t["members"]:
                old = pm.get(m["site"], {}).get("wan_ip")
                if old and m.get("wan_ip") and old != m["wan_ip"]:
                    await ctx.event(m["site"], "TUNNEL_ENDPOINT_CHANGED", f"{m['site_desc']} tunnel endpoint WAN IP {old} → {m['wan_ip']}",
                                    "medium", metadata={"tunnel": tid})
        reconnects = len(st.window(eid + ":reconnects", 3600))
        buf.add(now, t["local_site"], "tunnel", eid, "reconnects_1h", reconnects)
        st.put_current(t["local_site"], "tunnel", eid, label, "reconnects_1h", reconnects)
        t["ts"] = time.time()
        # carry over live-derived fields
        if prev:
            for k in ("live", "ifname"):
                if k in prev and k not in t:
                    t[k] = prev[k]
    st.tunnels = tunnels
    await kv_set(ctx.pool, "tunnels", list(tunnels.values()))


# ---------------------------------------------------------------- slow: topology, networks, wan history
async def poll_topology(ctx: Ctx, site: str) -> None:
    topo = await ctx.unifi.v2(site, "topology")
    await kv_set(ctx.pool, f"topology.{site}", sanitize(topo))
    nets = await ctx.unifi.legacy(site, "rest/networkconf")
    await kv_set(ctx.pool, f"networks.{site}", [
        {k: n.get(k) for k in ("_id", "name", "purpose", "vlan", "vlan_enabled", "ip_subnet", "enabled", "wan_type", "networkgroup")}
        for n in nets])


async def poll_wan_history(ctx: Ctx, site: str, buf: MetricBuffer) -> None:
    """UniFi's own 5-minute WAN statistics (latency, packet loss, rates, outages)."""
    dash = await ctx.unifi.v2(site, "aggregated-dashboard")
    wan = await kv_get(ctx.pool, f"wan.{site}") or {}
    cursor = int(await kv_get(ctx.pool, f"wanhist.cursor.{site}", 0) or 0)
    complete_before = (time.time() - 360) * 1000
    newest = cursor
    groups = (dash.get("wan_activity") or {}).get("activity_by_network_group") or {}
    for group, g in groups.items():
        eid = f"wan:{site}:{group}" if wan.get("wan_name") != group else wan.get("entity_id", f"wan:{site}:{group}")
        for h in g.get("history") or []:
            ts_ms = h.get("timestamp") or 0
            if ts_ms <= cursor or ts_ms > complete_before:
                continue
            ts = from_epoch(ts_ms)
            buf.add(ts, site, "wan", eid, "ua_latency_ms", h.get("avg_latency_ms"))
            buf.add(ts, site, "wan", eid, "ua_latency_max_ms", h.get("max_latency_ms"))
            buf.add(ts, site, "wan", eid, "loss_pct", h.get("avg_packet_loss_pct"))
            buf.add(ts, site, "wan", eid, "ua_rx_bps", h.get("avg_rx_rate_bps"))
            buf.add(ts, site, "wan", eid, "ua_tx_bps", h.get("avg_tx_rate_bps"))
            newest = max(newest, ts_ms)
    for h in (dash.get("internet") or {}).get("health_history") or []:
        ts_ms = h.get("timestamp") or 0
        if ts_ms <= cursor or ts_ms > complete_before:
            continue
        eid = wan.get("entity_id", f"wan:{site}:WAN")
        ts = from_epoch(ts_ms)
        buf.add(ts, site, "wan", eid, "downtime", 1 if h.get("wan_downtime") else 0)
        buf.add(ts, site, "wan", eid, "high_latency_flag", 1 if h.get("high_latency") else 0)
        buf.add(ts, site, "wan", eid, "packet_loss_flag", 1 if h.get("packet_loss") else 0)
        newest = max(newest, ts_ms)
    # The most recent complete 5-minute loss value feeds the alert engine.
    latest = None
    for group, g in groups.items():
        hist = [h for h in (g.get("history") or []) if (h.get("timestamp") or 0) <= complete_before]
        if hist:
            latest = hist[-1]
    if latest is not None and wan.get("entity_id"):
        ctx.state.put_current(site, "wan", wan["entity_id"], wan.get("name"), "loss_pct", num(latest.get("avg_packet_loss_pct")))
    if newest > cursor:
        await kv_set(ctx.pool, f"wanhist.cursor.{site}", newest)
    await kv_set(ctx.pool, f"dashboard.{site}", sanitize({k: dash.get(k) for k in ("wan_routability_info", "isp_metrics", "wifi_doctor", "dashboard_meta")}))


async def discover_capabilities(ctx: Ctx) -> None:
    """Record which metrics the controller actually exposes per device, so the UI never shows invented values."""
    caps = {}
    for mac, d in ctx.state.devices.items():
        kind = d.get("_kind")
        stats = d.get("system-stats") or {}
        uplink = d.get("uplink") or {}
        c = {
            "cpu": stats.get("cpu") not in (None, ""), "memory": stats.get("mem") not in (None, ""),
            "temperature": device_temp(d) is not None, "load": bool((d.get("sys_stats") or {}).get("loadavg_1")),
            "uptime": d.get("uptime") is not None, "ports": bool(d.get("port_table")),
            "poe": any(p.get("port_poe") for p in d.get("port_table") or []), "poe_budget": num(d.get("total_max_power")) is not None,
            "radios": bool(d.get("radio_table_stats")), "radio_noise": any("noise" in r for r in d.get("radio_table_stats") or []),
            "channel_utilization": any("cu_total" in r for r in d.get("radio_table_stats") or []),
            "retries": any("tx_retries_pct" in r for r in d.get("radio_table_stats") or []),
            "fan": d.get("fan_level") is not None,
        }
        if kind == "gateway":
            c.update({"wan_latency": uplink.get("latency") is not None, "wan_throughput": uplink.get("rx_bytes-r") is not None,
                      "wan_uptime_monitors": bool((d.get("uptime_stats") or {})), "dhcp_leases": d.get("ipv4_active_leases") is not None,
                      "nat_table": False, "firewall_counters": False, "routing_table": False, "dns_stats": False})
        caps[mac] = {"name": d.get("_name"), "site": d.get("_site"), "kind": kind, "model": d.get("model"), "capabilities": c}
    await kv_set(ctx.pool, "capabilities", caps)


async def housekeeping(ctx: Ctx) -> None:
    keep = timedelta(seconds=settings.log_retention)
    await ctx.pool.execute("DELETE FROM events WHERE ts < now() - $1::interval", keep)
    await ctx.pool.execute("DELETE FROM alerts WHERE state='resolved' AND resolved_at < now() - interval '365 days'")
    await ctx.pool.execute("DELETE FROM sessions WHERE expires_at < now()")
    await ctx.pool.execute("DELETE FROM auth_events WHERE ts < now() - interval '180 days'")
    await ctx.pool.execute("DELETE FROM clients WHERE NOT online AND last_seen < now() - $1::interval", keep)
    row = await ctx.pool.fetchrow(
        """SELECT pg_database_size(current_database()) AS db_bytes,
                  (SELECT count(*) FROM events) AS events,
                  (SELECT count(*) FROM alerts) AS alerts,
                  (SELECT hypertable_size('metrics')) AS metrics_bytes,
                  (SELECT count(*) FROM timescaledb_information.chunks WHERE hypertable_name='metrics') AS chunks,
                  (SELECT approximate_row_count('metrics')) AS metric_rows""")
    await kv_set(ctx.pool, "db_stats", {**dict(row), "ts": time.time()})

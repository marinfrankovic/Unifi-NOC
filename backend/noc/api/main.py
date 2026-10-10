"""UniFi NOC REST API (FastAPI). All endpoints except /api/healthz and /api/auth/login require a session."""
import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ..config import settings
from ..db import connect, kv_get
from ..entities import entity_device_mac
from ..collector.health import overall_health
from . import auth
from .access import can_admin_site, can_read_site, is_owner, require_site_admin
from .users import routes as user_routes
from .monitoring import routes as monitoring_routes
from .setup import routes as setup_routes
from .insights import routes as insight_routes
from .operations_settings import routes as operation_setting_routes
from .account import routes as account_routes
from .series import RANGES, latest, series, to_dt, window

log = logging.getLogger("noc.api")
STATE = {"pool": None, "started": time.time(), "requests": 0, "errors": 0, "last_error": None}


def pool():
    return STATE["pool"]


current_user, admin_user = auth.make_dependencies(pool)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    STATE["pool"] = await connect(max_size=10)
    # Schema is owned by the collector; wait for it instead of racing.
    while not await STATE["pool"].fetchval("SELECT to_regclass('public.users') IS NOT NULL"):
        log.info("waiting for schema")
        await asyncio.sleep(2)
    await auth.bootstrap_admin(STATE["pool"])
    yield
    await STATE["pool"].close()


app = FastAPI(title="UniFi NOC API", version="1.0.0", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
app.include_router(user_routes(pool, current_user))
app.include_router(monitoring_routes(pool, current_user))
app.include_router(setup_routes(pool, current_user))
app.include_router(account_routes(pool,current_user))
app.include_router(insight_routes(pool, current_user, lambda user: site_map(user),
    lambda user: api_devices(site=None, type=None, user=user), lambda user: api_alerts(state='firing', limit=200, user=user),
    lambda user: tunnels_summary(user)))
app.include_router(operation_setting_routes(pool, current_user, lambda user: site_map(user)))


@app.middleware("http")
async def count_requests(request: Request, call_next):
    STATE["requests"] += 1
    try:
        resp = await call_next(request)
    except Exception as exc:  # noqa: BLE001
        STATE["errors"] += 1
        STATE["last_error"] = {"ts": time.time(), "path": request.url.path, "error": f"{type(exc).__name__}: {exc}"[:300]}
        log.exception("unhandled error on %s", request.url.path)
        return JSONResponse({"detail": "Internal server error"}, status_code=500)
    if resp.status_code >= 500:
        STATE["errors"] += 1
        STATE["last_error"] = {"ts": time.time(), "path": request.url.path, "status": resp.status_code}
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _iso(v):
    return v.isoformat() if isinstance(v, datetime) else v


def rowdict(r) -> dict:
    return {k: _iso(v) for k, v in dict(r).items()}


async def site_map(user: dict) -> dict[str, dict]:
    rows = await pool().fetch("SELECT id::text AS id, name, site_id, uuid, connection_id::text, selected, description, slug, timezone FROM sites WHERE selected ORDER BY description")
    return {r["name"]: dict(r) for r in rows if can_read_site(user, r.get("id", ""))}


async def resolve_site(value: str | None, user: dict) -> str | None:
    """Accept slug ('am'), short name ('demo0001') or description; reject anything not monitored."""
    if not value or value == "all":
        return None
    for name, s in (await site_map(user)).items():
        if value.lower() in (name.lower(), s["slug"].lower(), s["description"].lower(), s.get("id", "").lower()):
            return name
    raise HTTPException(404, "Unknown or unmonitored site")


async def resolve_sites(value: str | None, user: dict) -> list[str]:
    name = await resolve_site(value, user)
    return [name] if name else list(await site_map(user))


async def metric_scope(user: dict) -> tuple[list[str], list[str]]:
    sites = list(await site_map(user))
    if is_owner(user):
        sites.append("_all")
    denied = []
    for tunnel in await kv_get(pool(), "tunnels") or []:
        if not all(member.get("site") in sites for member in tunnel.get("members", [])):
            denied.append(f"tunnel:{tunnel['id']}")
    return sites, denied


async def readable_tunnels(user: dict) -> list[str] | None:
    return None if is_owner(user) else [f"tunnel:{t['id']}" for t in await tunnels_summary(user)]


def freshness(ts: float | None, interval: float) -> dict:
    if not ts:
        return {"state": "NO_DATA", "age_s": None}
    age = time.time() - ts
    return {"state": "LIVE" if age <= interval * 3 + 5 else "STALE", "age_s": round(age, 1), "last_update": int(ts * 1000)}


# ------------------------------------------------------------------ auth
class LoginBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    remember_me: bool = False


@app.post("/api/auth/login")
async def api_login(body: LoginBody, request: Request, response: Response):
    if request.headers.get("x-requested-with") != "noc-ui":
        raise HTTPException(403, "Missing anti-CSRF header")
    return await auth.login(pool(), request, response, body.username, body.password, body.remember_me)


@app.post("/api/auth/logout")
async def api_logout(request: Request, response: Response):
    await auth.logout(pool(), request, response)
    return {"ok": True}


@app.get("/api/auth/me")
async def api_me(user=Depends(current_user)):
    return user


@app.get("/api/healthz")
async def healthz():
    await pool().fetchval("SELECT 1")
    return {"ok": True}


# ------------------------------------------------------------------ status / self-monitoring
async def sources_status() -> list[dict]:
    rows = await pool().fetch("SELECT key, value, updated_at FROM kv_state WHERE key LIKE 'source.%' ORDER BY key")
    inactive = await pool().fetch("SELECT name FROM sites WHERE NOT selected")
    out = []
    for r in rows:
        v = r["value"]
        if any((v.get("name") or '').endswith('.' + site['name']) for site in inactive):
            continue
        f = freshness(v.get("last_ok"), v.get("interval") or 60)
        out.append({**v, "freshness": f["state"], "age_s": f["age_s"]})
    return out


@app.get("/api/status")
async def api_status(user=Depends(current_user)):
    p = pool()
    hb = await kv_get(p, "collector.heartbeat") or {}
    hb_f = freshness(hb.get("ts"), (hb.get("intervals") or {}).get("live", settings.live_poll_interval))
    if not is_owner(user):
        return {"now": int(time.time() * 1000), "collector": {"state": "OK" if hb_f["state"] == "LIVE" else "DATA COLLECTION ERROR",
                "freshness": hb_f}, "sources": [], "stale_sources": []}
    srcs = await sources_status()
    web = {"state": "UNKNOWN"}
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(settings.web_health_url)
            web = {"state": "OK" if r.status_code == 200 else f"HTTP {r.status_code}"}
    except httpx.HTTPError as exc:
        web = {"state": "ERROR", "error": str(exc)[:200]}
    db_ok = True
    try:
        await p.fetchval("SELECT 1")
    except Exception:  # noqa: BLE001
        db_ok = False
    jobs = await p.fetch("""SELECT j.job_id, j.proc_name, js.last_run_status, js.last_successful_finish, js.total_failures
                            FROM timescaledb_information.jobs j LEFT JOIN timescaledb_information.job_stats js USING (job_id)
                            WHERE j.hypertable_name IN ('metrics','metrics_5m','metrics_1h') OR j.proc_name LIKE 'policy_%'""")
    stale = [s["name"] for s in srcs if s["freshness"] != "LIVE"]
    collector_state = "OK" if hb_f["state"] == "LIVE" and not [s for s in srcs if s.get("consecutive_errors", 0) >= 3] else \
        ("DATA COLLECTION ERROR" if hb_f["state"] != "LIVE" or any(s.get("consecutive_errors", 0) >= 3 for s in srcs) else "DEGRADED")
    return {
        "now": int(time.time() * 1000),
        "collector": {"state": collector_state, "heartbeat": hb, "freshness": hb_f},
        "api": {"state": "OK", "uptime_s": round(time.time() - STATE["started"]), "requests": STATE["requests"], "errors": STATE["errors"],
                "last_error": STATE["last_error"]},
        "frontend": web,
        "database": {"state": "OK" if db_ok else "ERROR", **(await kv_get(p, "db_stats") or {}),
                     "jobs": [rowdict(j) for j in jobs]},
        "controller": await kv_get(p, "controller.info") or {},
        "sources": srcs,
        "stale_sources": stale,
        "missing_sites": await kv_get(p, "sites.missing") or [],
    }


# ------------------------------------------------------------------ overview / sites
async def site_summary(name: str, s: dict, user: dict) -> dict:
    p = pool()
    health = await kv_get(p, f"health.{name}") or {}
    if not is_owner(user):
        health = {**health, "reasons": [{**reason, "text": "External connectivity problem", "entity": None}
                  if (reason.get("entity") or "").startswith("tunnel:") else reason for reason in health.get("reasons", [])]}
    wan = await kv_get(p, f"wan.{name}") or {}
    devs = await p.fetch("SELECT device_type, online, count(*) AS n FROM devices WHERE site=$1 GROUP BY 1, 2", name)
    counts = {}
    for r in devs:
        c = counts.setdefault(r["device_type"], {"online": 0, "offline": 0})
        c["online" if r["online"] else "offline"] += r["n"]
    cl = await p.fetchrow("SELECT count(*) FILTER (WHERE online) AS online, count(*) FILTER (WHERE online AND is_wired) AS wired, "
                          "count(*) FILTER (WHERE online AND NOT is_wired) AS wifi FROM clients WHERE site=$1", name)
    al = await p.fetch("SELECT severity, count(*) AS n FROM alerts WHERE site=$1 AND state='firing' GROUP BY 1", name)
    gw = await p.fetchrow("SELECT mac, name, model, version, online, uptime FROM devices WHERE site=$1 AND device_type='gateway' LIMIT 1", name)
    wan_now = (await latest(p, [wan["entity_id"]], 120)).get(wan["entity_id"], {}) if wan.get("entity_id") else {}
    gw_now = (await latest(p, [f"dev:{gw['mac']}"], 120)).get(f"dev:{gw['mac']}", {}) if gw else {}
    incidents = await p.fetch(
        """SELECT id, ts, severity, event_type, message, device FROM events WHERE site=$1 AND ts > now() - interval '24 hours'
           AND (severity IN ('critical','high') OR event_type IN ('WAN_DOWN','WAN_UP','TUNNEL_DOWN','TUNNEL_UP','TUNNEL_RECONNECT','DEVICE_OFFLINE',
                'DEVICE_ONLINE','ALERT_FIRED','ALERT_RESOLVED','ANOMALY_DETECTED','DEVICE_UNREACHABLE','MULTIPLE_DEVICES_UNREACHABLE'))
           AND ($2 OR (event_type NOT LIKE 'TUNNEL%' AND coalesce(metadata->>'entity', '') NOT LIKE 'tunnel:%'))
           ORDER BY ts DESC LIMIT 8""", name, is_owner(user))
    return {
        **s, "health": health, "health_freshness": freshness(health.get("ts"), settings.poll_interval),
        "wan": {**wan, "now": wan_now, "freshness": freshness(wan.get("ts"), settings.poll_interval)},
        "gateway": {**rowdict(gw), "now": gw_now} if gw else None,
        "devices": counts, "clients": dict(cl), "alerts": {r["severity"]: r["n"] for r in al},
        "incidents": [rowdict(i) for i in incidents],
    }


async def tunnels_summary(user: dict) -> list[dict]:
    p = pool()
    sites = await site_map(user)
    out = []
    for t in await kv_get(p, "tunnels") or []:
        if not t.get("members") or not all(member.get("site") in sites for member in t["members"]):
            continue
        live = await kv_get(p, f"tunnel_live.{t['id']}") or {}
        t = {k: v for k, v in t.items() if not k.startswith("_") and k != "live"}
        config_freshness = freshness(t.get('ts'), settings.poll_interval)
        live_freshness = freshness(live.get('ts'), settings.live_poll_interval)
        if config_freshness['state'] != 'LIVE' and live_freshness['state'] == 'LIVE':
            live_freshness = {**config_freshness, 'state': 'STALE'}
        out.append({**t, "live": live, "freshness": live_freshness, "config_freshness": config_freshness})
    return out


@app.get("/api/overview")
async def api_overview(user=Depends(current_user)):
    p = pool()
    sites = await site_map(user)
    summaries = [await site_summary(n, s, user) for n, s in sites.items()]
    scores = {s["name"]: {**s["health"], "health_score": s["health"].get("health_score") if s["health_freshness"]["state"] == "LIVE" else None} for s in summaries}
    overall = overall_health(scores, {}, {name: s["description"] for name, s in sites.items()})
    overall["ts"] = min((s["health"].get("ts") or 0 for s in summaries), default=0)
    allowed, denied = await metric_scope(user)
    visible_tunnels = await readable_tunnels(user)
    alerts = await p.fetch("SELECT * FROM alerts WHERE state='firing' AND site=ANY($1::text[]) AND NOT(entity_id=ANY($2::text[])) AND ($3::text[] IS NULL OR entity_type<>'tunnel' OR entity_id=ANY($3)) ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, started_at DESC LIMIT 20", allowed, denied, visible_tunnels)
    return {"overall": {**overall, "freshness": freshness(overall.get("ts"), settings.poll_interval)}, "sites": summaries,
            "tunnels": await tunnels_summary(user), "alerts": [rowdict(a) for a in alerts], "include_global_history": is_owner(user)}


@app.get("/api/sites")
async def api_sites(user=Depends(current_user)):
    return list((await site_map(user)).values())


@app.get("/api/sites/{site}")
async def api_site(site: str, user=Depends(current_user)):
    name = await resolve_site(site, user)
    if not name:
        raise HTTPException(400, "site required")
    s = (await site_map(user))[name]
    summary = await site_summary(name, s, user)
    p = pool()
    devs = await p.fetch("SELECT mac, name, device_type, model, version, ip, online, state, uptime, last_seen FROM devices WHERE site=$1 ORDER BY device_type, name", name)
    now = await latest(p, [f"dev:{d['mac']}" for d in devs], 120)
    summary["device_list"] = [{**rowdict(d), "now": now.get(f"dev:{d['mac']}", {})} for d in devs]
    summary["unifi_health"] = (await kv_get(p, f"unifi_health.{name}") or {}) if is_owner(user) else {}
    summary["tunnels"] = [t for t in await tunnels_summary(user) if any(m["site"] == name for m in t.get("members", []))]
    return summary


# ------------------------------------------------------------------ devices
@app.get("/api/devices")
async def api_devices(site: str | None = None, type: str | None = None, user=Depends(current_user)):
    name = await resolve_sites(site, user)
    p = pool()
    rows = await p.fetch(
        """SELECT d.mac, d.site, s.slug, s.description AS site_name, d.name, d.device_type, d.model, d.model_name, d.version, d.ip, d.online, d.state,
                  d.uptime, d.last_seen, d.last_poll, d.uplink_mac, d.uplink_port, d.upgradable, d.upgrade_to,
                  d.raw->>'_state_label' AS state_label, (d.raw->>'num_sta')::int AS num_sta
           FROM devices d JOIN sites s ON s.name=d.site WHERE d.site=ANY($1::text[]) AND ($2::text IS NULL OR d.device_type=$2)
           ORDER BY s.description, d.device_type, d.name""", name, type)
    now = await latest(p, [f"dev:{r['mac']}" for r in rows], 120)
    alerts = await p.fetch("SELECT entity_id, severity FROM alerts WHERE state='firing' AND site=ANY($1::text[])", name)
    sev = {}
    for a in alerts:
        mac = entity_device_mac(a["entity_id"])
        if mac:
            sev[mac] = "critical" if "critical" in (sev.get(mac), a["severity"]) else a["severity"]
    return [{**rowdict(r), "now": now.get(f"dev:{r['mac']}", {}), "alert": sev.get(r["mac"]),
             "freshness": freshness(r["last_poll"].timestamp() if r["last_poll"] else None, settings.poll_interval)} for r in rows]


@app.get("/api/devices/{mac}")
async def api_device(mac: str, user=Depends(current_user)):
    p = pool()
    allowed = list(await site_map(user))
    d = await p.fetchrow("SELECT d.*, s.slug, s.description AS site_name FROM devices d JOIN sites s ON s.name=d.site WHERE d.mac=$1 AND d.site=ANY($2::text[])", mac.lower(), allowed)
    if not d:
        raise HTTPException(404, "Device not found")
    raw = d["raw"] or {}
    ports = raw.get("port_table") or []
    radios = raw.get("radio_table_stats") or []
    eids = [f"dev:{mac}"] + [f"port:{mac}:{x.get('port_idx')}" for x in ports] + [f"radio:{mac}:{r.get('radio')}" for r in radios]
    now = await latest(p, eids, 120)
    clients = await p.fetch(
        "SELECT mac, name, ip, sw_port, is_wired, signal, radio, essid, online FROM clients WHERE site=$1 AND online AND (sw_mac=$2 OR ap_mac=$2) ORDER BY name",
        d["site"], mac)
    by_port = {}
    for c in clients:
        if c["sw_port"] is not None:
            by_port.setdefault(c["sw_port"], []).append({"mac": c["mac"], "name": c["name"], "ip": c["ip"]})
    downlinks = await p.fetch("SELECT mac, name, device_type, uplink_port FROM devices WHERE uplink_mac=$1 AND site=$2", mac, d["site"])
    for dl in downlinks:
        if dl["uplink_port"] is not None:
            by_port.setdefault(dl["uplink_port"], []).append({"mac": dl["mac"], "name": dl["name"], "device": True})
    caps = (await kv_get(p, "capabilities") or {}).get(mac, {})
    alerts = await p.fetch("SELECT * FROM alerts WHERE (entity_id=$1 OR entity_id LIKE $2 OR entity_id LIKE $3) ORDER BY state='firing' DESC, started_at DESC LIMIT 50",
                           f"dev:{mac}", f"port:{mac}:%", f"radio:{mac}:%")
    events = await p.fetch("SELECT * FROM events WHERE device_mac=$1 AND site=$2 AND ($3 OR event_type NOT LIKE 'TUNNEL%') ORDER BY ts DESC LIMIT 50", mac, d["site"], is_owner(user))
    wan = None
    if d["device_type"] == "gateway":
        wan = await kv_get(p, f"wan.{d['site']}")
    return {**{k: _iso(v) for k, v in dict(d).items() if k != "raw"}, "raw": raw, "now": now, "port_clients": by_port,
            "clients": [rowdict(c) for c in clients], "capabilities": caps.get("capabilities", {}),
            "alerts": [rowdict(a) for a in alerts], "events": [rowdict(e) for e in events], "wan": wan,
            "freshness": freshness(d["last_poll"].timestamp() if d["last_poll"] else None, settings.poll_interval)}


# ------------------------------------------------------------------ clients
@app.get("/api/clients")
async def api_clients(site: str | None = None, online: bool | None = True, q: str | None = None, ap: str | None = None,
                      switch: str | None = None, network: str | None = None, band: str | None = None, conn: str | None = None,
                      limit: int = Query(500, le=2000), user=Depends(current_user)):
    name = await resolve_sites(site, user)
    p = pool()
    rows = await p.fetch(
        """SELECT c.site, s.slug, s.description AS site_name, c.mac, c.name, c.hostname, c.ip, c.oui, c.is_wired, c.online, c.network, c.vlan,
                  c.essid, c.radio, c.channel, c.ap_mac, c.sw_mac, c.sw_port, c.signal, c.rssi, c.noise, c.first_seen, c.last_seen, c.assoc_time,
                  c.uptime, ap.name AS ap_name, sw.name AS sw_name, (c.raw->>'tx_rate')::float AS tx_rate, (c.raw->>'rx_rate')::float AS rx_rate,
                  (c.raw->>'satisfaction')::float AS satisfaction, (c.raw->>'wifi_tx_retries_percentage')::float AS retries_pct, c.raw->>'radio_proto' AS proto
           FROM clients c JOIN sites s ON s.name=c.site LEFT JOIN devices ap ON ap.mac=c.ap_mac LEFT JOIN devices sw ON sw.mac=c.sw_mac
           WHERE c.site=ANY($1::text[]) AND ($2::bool IS NULL OR c.online=$2)
             AND ($3::text IS NULL OR c.name ILIKE $3 OR c.hostname ILIKE $3 OR c.mac ILIKE $3 OR c.ip ILIKE $3 OR c.oui ILIKE $3)
             AND ($4::text IS NULL OR c.ap_mac=$4) AND ($5::text IS NULL OR c.sw_mac=$5) AND ($6::text IS NULL OR c.network=$6)
             AND ($7::text IS NULL OR c.radio=$7) AND ($8::text IS NULL OR ($8='wired' AND c.is_wired) OR ($8='wifi' AND NOT c.is_wired))
           ORDER BY c.online DESC, c.name LIMIT $9""",
        name, online, f"%{q}%" if q else None, ap, switch, network, band, conn, limit)
    eids = [f"client:{r['site']}:{r['mac']}" for r in rows if r["online"]]
    now = await latest(p, eids, 120, ["rx_bps", "tx_bps", "signal_dbm", "retries_pct", "satisfaction"])
    ev = await p.fetch(
        """SELECT site, client_mac, count(*) FILTER (WHERE event_type='CLIENT_ROAMED') AS roams,
                  count(*) FILTER (WHERE event_type LIKE 'CLIENT_DISCONNECTED%') AS disconnects
           FROM events WHERE site=ANY($1::text[]) AND client_mac IS NOT NULL AND ts > now() - interval '24 hours' GROUP BY 1, 2""", name)
    evm = {(e["site"], e["client_mac"]): e for e in ev}
    out = []
    for r in rows:
        n = now.get(f"client:{r['site']}:{r['mac']}", {})
        e = evm.get((r["site"], r["mac"]))
        flags = []
        if r["online"] and not r["is_wired"] and r["signal"] is not None and r["signal"] < -75:
            flags.append("poor_signal")
        if (n.get("rx_bps") or 0) + (n.get("tx_bps") or 0) > 20e6:
            flags.append("high_bandwidth")
        if e and e["roams"] >= 10:
            flags.append("frequent_roaming")
        if e and e["disconnects"] >= 10:
            flags.append("frequent_disconnects")
        if (r["retries_pct"] or 0) >= 30 and r["online"] and not r["is_wired"]:
            flags.append("high_retries")
        out.append({**rowdict(r), "now": n, "roams_24h": e["roams"] if e else 0, "disconnects_24h": e["disconnects"] if e else 0, "flags": flags})
    return out


@app.get("/api/clients/{site}/{mac}")
async def api_client(site: str, mac: str, user=Depends(current_user)):
    name = await resolve_site(site, user)
    p = pool()
    r = await p.fetchrow("""SELECT c.*, s.slug, s.description AS site_name, ap.name AS ap_name, sw.name AS sw_name FROM clients c JOIN sites s ON s.name=c.site
                            LEFT JOIN devices ap ON ap.mac=c.ap_mac LEFT JOIN devices sw ON sw.mac=c.sw_mac WHERE c.site=$1 AND c.mac=$2""", name, mac.lower())
    if not r:
        raise HTTPException(404, "Client not found")
    events = await p.fetch("SELECT * FROM events WHERE site=$1 AND client_mac=$2 ORDER BY ts DESC LIMIT 100", name, mac.lower())
    alerts = await p.fetch("SELECT * FROM alerts WHERE entity_id=$1 ORDER BY started_at DESC LIMIT 20", f"client:{name}:{mac.lower()}")
    now = (await latest(p, [f"client:{name}:{mac.lower()}"], 120)).get(f"client:{name}:{mac.lower()}", {})
    return {**{k: _iso(v) for k, v in dict(r).items()}, "now": now, "events": [rowdict(e) for e in events], "alerts": [rowdict(a) for a in alerts],
            "entity_id": f"client:{name}:{mac.lower()}"}


# ------------------------------------------------------------------ WAN
@app.get("/api/wan")
async def api_wan(range: str = "24h", site: str | None = None, user=Depends(current_user)):
    p = pool()
    out = []
    start, end = window(range, None, None)
    selected = await resolve_sites(site, user)
    for name, s in (await site_map(user)).items():
        if name not in selected:
            continue
        wan = await kv_get(p, f"wan.{name}") or {}
        eid = wan.get("entity_id")
        now = (await latest(p, [eid], 120)).get(eid, {}) if eid else {}
        outages = await p.fetch("SELECT ts, event_type, message, metadata FROM events WHERE site=$1 AND event_type IN ('WAN_DOWN','WAN_UP','WAN_IP_CHANGED') "
                                "AND ts > now() - interval '30 days' ORDER BY ts DESC LIMIT 50", name)
        avail = None
        if eid:
            row = await p.fetchrow("SELECT count(*) AS n, sum(value) AS down FROM metrics WHERE entity_id=$1 AND metric='downtime' AND ts >= $2 AND ts <= $3",
                                   eid, to_dt(start), to_dt(end))
            if row and row["n"]:
                avail = {"buckets": row["n"], "down_buckets": int(row["down"] or 0), "availability_pct": round(100 * (1 - (row["down"] or 0) / row["n"]), 3),
                         "source": "UniFi 5-minute internet health"}
        out.append({"site": name, "slug": s["slug"], "site_name": s["description"], **wan, "now": now, "outages": [rowdict(o) for o in outages],
                    "availability": avail, "freshness": freshness(wan.get("ts"), settings.poll_interval)})
    return out


# ------------------------------------------------------------------ site-to-site
@app.get("/api/s2s")
async def api_s2s(user=Depends(current_user)):
    p = pool()
    runtime=await kv_get(p,'connection.runtime') or {}
    ssh_enabled=runtime.get('gateway_ssh_enabled',settings.gateway_ssh_enabled)
    probe_enabled=runtime.get('probe_enabled',settings.probe_enabled)
    tunnels = await tunnels_summary(user)
    for t in tunnels:
        t["events"] = [rowdict(e) for e in await p.fetch(
            "SELECT * FROM events WHERE (metadata->>'tunnel'=$1 OR metadata->>'entity'=$2) ORDER BY ts DESC LIMIT 50",
            t["id"], f"tunnel:{t['id']}")]
        t["alerts"] = [rowdict(a) for a in await p.fetch("SELECT * FROM alerts WHERE entity_id=$1 ORDER BY state='firing' DESC, started_at DESC LIMIT 30",
                                                          f"tunnel:{t['id']}")]
        t["entity_id"] = f"tunnel:{t['id']}"
        keys = {}
        for m in t.get("members", []):
            hk = await kv_get(p, f"gw_hostkey.{m['site']}")
            if hk:
                keys[m["site"]] = {"fingerprint": hk.get("fingerprint"), "host": hk.get("host"), "algorithm": hk.get("algorithm")}
        t["gateway_hostkeys"] = keys
        t["capabilities"] = {
            "status": "UniFi controller (magicsitetositevpn)",
            "traffic": "Gateway WireGuard interface counters via SSH" if ssh_enabled else "NOT AVAILABLE (gateway SSH disabled)",
            "latency_loss_jitter": f"ICMP probes from collector through the tunnel ({settings.probe_count} pings / {settings.live_poll_interval} s)"
            if probe_enabled else "NOT AVAILABLE (probes disabled)",
            "handshake": "WireGuard latest-handshake via gateway SSH" if ssh_enabled else "NOT AVAILABLE",
        }
    return tunnels


@app.get("/api/s2s/{tid}/live")
async def api_s2s_live(tid: str, seconds: int = Query(300, ge=30, le=3600), user=Depends(current_user)):
    if not any(t["id"] == tid for t in await tunnels_summary(user)):
        raise HTTPException(404, "Connection not found")
    p = pool()
    eid = f"tunnel:{tid}"
    rows = await p.fetch("""SELECT metric, ts, value FROM metrics WHERE entity_id=$1 AND ts > now() - make_interval(secs => $2)
                            AND metric IN ('rx_bps','tx_bps','total_bps','latency_ms','jitter_ms','loss_pct','loss_pct_1m','remote_rx_bps','remote_tx_bps')
                            ORDER BY ts""", eid, seconds)
    out = {}
    for r in rows:
        out.setdefault(r["metric"], []).append([int(r["ts"].timestamp() * 1000), round(r["value"], 3)])
    live = await kv_get(p, f"tunnel_live.{tid}") or {}
    return {"series": out, "live": live, "freshness": freshness(live.get("ts"), settings.live_poll_interval), "interval_s": settings.live_poll_interval}


# ------------------------------------------------------------------ metrics
@app.get("/api/metrics")
async def api_metrics(entity: str, metric: str, range: str | None = "1h", frm: str | None = Query(None, alias="from"), to: str | None = None,
                      res: str | None = None, user=Depends(current_user)):
    ents = [e for e in entity.split(",") if e][:60]
    mets = [m for m in metric.split(",") if m][:20]
    if not ents or not mets:
        raise HTTPException(400, "entity and metric are required")
    start, end = window(range, frm, to)
    allowed, denied = await metric_scope(user)
    return await series(pool(), ents, mets, start, end, res, allowed, denied, await readable_tunnels(user))


@app.get("/api/metrics/heatmap")
async def api_heatmap(entity_type: str, metric: str, site: str | None = None, range: str = "24h", user=Depends(current_user)):
    name = await resolve_sites(site, user)
    _, denied = await metric_scope(user)
    visible_tunnels = await readable_tunnels(user)
    start, end = window(range, None, None)
    span = end - start
    view, bucket = ("metrics_5m", "1 hour") if span <= 2 * 86400 else ("metrics_1h", "6 hours")
    rows = await pool().fetch(
        f"""SELECT entity_id, time_bucket('{bucket}', bucket) AS t, sum(avg * n) / nullif(sum(n), 0) AS v FROM {view}
            WHERE entity_type=$1 AND metric=$2 AND site=ANY($3::text[]) AND NOT(entity_id=ANY($6::text[]))
            AND ($7::text[] IS NULL OR entity_type<>'tunnel' OR entity_id=ANY($7)) AND bucket >= $4 AND bucket <= $5 GROUP BY 1, 2 ORDER BY 1, 2""",
        entity_type, metric, name, to_dt(start), to_dt(end), denied, visible_tunnels)
    out = {}
    for r in rows:
        out.setdefault(r["entity_id"], []).append([int(r["t"].timestamp() * 1000), None if r["v"] is None else round(r["v"], 2)])
    return {"bucket": bucket, "series": out}


@app.get("/api/metrics/top")
async def api_top(entity_type: str, metric: str, site: str | None = None, range: str = "1h", limit: int = Query(10, le=50), user=Depends(current_user)):
    name = await resolve_sites(site, user)
    _, denied = await metric_scope(user)
    visible_tunnels = await readable_tunnels(user)
    start, end = window(range, None, None)
    rows = await pool().fetch(
          """SELECT entity_id, avg(value) AS avg, max(value) AS max FROM metrics WHERE entity_type=$1 AND metric=$2 AND site=ANY($3::text[])
               AND NOT(entity_id=ANY($7::text[])) AND ($8::text[] IS NULL OR entity_type<>'tunnel' OR entity_id=ANY($8))
               AND ts >= $4 AND ts <= $5 GROUP BY 1 ORDER BY 2 DESC NULLS LAST LIMIT $6""", entity_type, metric, name, to_dt(start), to_dt(end), limit, denied, visible_tunnels)
    return [{"entity_id": r["entity_id"], "avg": round(r["avg"], 2), "max": round(r["max"], 2)} for r in rows]


# ------------------------------------------------------------------ events
@app.get("/api/events")
async def api_events(site: str | None = None, severity: str | None = None, min_severity: str | None = None, type: str | None = None,
                     device: str | None = None, client: str | None = None, q: str | None = None, source: str | None = None,
                     category: str | None = None, range: str | None = "24h", frm: str | None = Query(None, alias="from"), to: str | None = None,
                     limit: int = Query(100, le=1000), offset: int = Query(0, ge=0), user=Depends(current_user)):
    name = await resolve_sites(site, user)
    if is_owner(user) and not site:
        name.append("_all")
    start, end = window(range, frm, to)
    order = ["critical", "high", "medium", "low", "info"]
    sev_list = severity.split(",") if severity else (order[: order.index(min_severity) + 1] if min_severity in order else None)
    args = [name, sev_list, f"%{type}%" if type else None, device, client, q, source, category, to_dt(start), to_dt(end), is_owner(user)]
    where = """site=ANY($1::text[]) AND ($11 OR (event_type NOT LIKE 'TUNNEL%' AND coalesce(metadata->>'entity', '') NOT LIKE 'tunnel:%')) AND ($2::text[] IS NULL OR severity = ANY($2)) AND ($3::text IS NULL OR event_type ILIKE $3)
               AND ($4::text IS NULL OR device_mac=$4 OR device ILIKE '%' || $4 || '%') AND ($5::text IS NULL OR client_mac=$5 OR client ILIKE '%' || $5 || '%')
               AND ($6::text IS NULL OR to_tsvector('simple', coalesce(message,'') || ' ' || coalesce(device,'') || ' ' || coalesce(client,'')) @@ plainto_tsquery('simple', $6)
                    OR message ILIKE '%' || $6 || '%')
               AND ($7::text IS NULL OR source=$7) AND ($8::text IS NULL OR category=$8) AND ts >= $9 AND ts <= $10"""
    p = pool()
    total = await p.fetchval(f"SELECT count(*) FROM events WHERE {where}", *args)
    rows = await p.fetch(f"""SELECT e.*, s.slug, s.description AS site_name FROM events e LEFT JOIN sites s ON s.name=e.site WHERE {where.replace('site=ANY', 'e.site=ANY')}
                             ORDER BY ts DESC LIMIT {int(limit)} OFFSET {int(offset)}""", *args)
    types = await p.fetch("SELECT event_type, count(*) AS n FROM events WHERE site=ANY($1::text[]) AND ($2 OR (event_type NOT LIKE 'TUNNEL%' AND coalesce(metadata->>'entity', '') NOT LIKE 'tunnel:%')) AND ts > now() - interval '7 days' GROUP BY 1 ORDER BY 2 DESC LIMIT 60", name, is_owner(user))
    return {"total": total, "items": [rowdict(r) for r in rows], "types": [dict(t) for t in types]}


# ------------------------------------------------------------------ alerts
@app.get("/api/alerts")
async def api_alerts(state: str = "firing", site: str | None = None, kind: str | None = None, severity: str | None = None,
                     range: str | None = "30d", limit: int = Query(200, le=2000), user=Depends(current_user)):
    name = await resolve_sites(site, user)
    if is_owner(user) and not site:
        name.append("_all")
    _, denied = await metric_scope(user)
    visible_tunnels = await readable_tunnels(user)
    start, end = window(range, None, None)
    rows = await pool().fetch(
        """SELECT a.*, s.slug, s.description AS site_name, extract(epoch FROM coalesce(a.resolved_at, now()) - a.started_at)::int AS duration_s
           FROM alerts a LEFT JOIN sites s ON s.name=a.site
           WHERE ($1='all' OR a.state=$1) AND a.site=ANY($2::text[]) AND NOT(a.entity_id=ANY($7::text[])) AND ($3::text IS NULL OR a.kind=$3) AND ($4::text IS NULL OR a.severity=$4)
             AND ($8::text[] IS NULL OR a.entity_type<>'tunnel' OR a.entity_id=ANY($8)) AND (a.state='firing' OR a.started_at >= $5)
           ORDER BY a.state='firing' DESC, CASE a.severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, a.started_at DESC LIMIT $6""",
        state, name, kind, severity, to_dt(start), limit, denied, visible_tunnels)
    sites = await site_map(user)
    return [{**rowdict(r), "can_ack": is_owner(user) or can_admin_site(user, sites.get(r["site"], {}).get("id", ""))} for r in rows]


@app.post("/api/alerts/{aid}/ack")
async def api_ack(aid: int, user=Depends(current_user)):
    alert = await pool().fetchrow("SELECT site, entity_id FROM alerts WHERE id=$1", aid)
    sites = await site_map(user)
    if not alert or (alert["site"] not in sites and not (is_owner(user) and alert["site"] == "_all")):
        raise HTTPException(404, "Alert not found")
    _, denied = await metric_scope(user)
    if alert["entity_id"] in denied:
        raise HTTPException(404, "Alert not found")
    visible_tunnels = await readable_tunnels(user)
    if visible_tunnels is not None and alert['entity_id'].startswith('tunnel:') and alert['entity_id'] not in visible_tunnels:
        raise HTTPException(404, "Alert not found")
    if alert["site"] in sites:
        require_site_admin(user, sites[alert["site"]]["id"])
    r = await pool().execute("UPDATE alerts SET acked_by=$2, acked_at=now() WHERE id=$1", aid, user["username"])
    if r.endswith(" 0"):
        raise HTTPException(404, "Alert not found")
    return {"ok": True}


class RuleUpdate(BaseModel):
    warn: float | None = None
    crit: float | None = None
    for_s: int | None = Field(None, ge=0, le=86400)
    clear_for_s: int | None = Field(None, ge=0, le=86400)
    hysteresis: float | None = Field(None, ge=0)
    enabled: bool | None = None


@app.get("/api/alert-rules")
async def api_rules(site: str | None = None, user=Depends(current_user)):
    name = await resolve_site(site, user)
    rules = [rowdict(r) for r in await pool().fetch("SELECT * FROM alert_rules ORDER BY entity_type, id")]
    if name:
        overrides = {row['rule_id']: row['values'] for row in await pool().fetch('SELECT rule_id, values FROM site_rule_overrides WHERE site=$1', name)}
        rules = [{**rule, **overrides.get(rule['id'], {}), 'site_override': rule['id'] in overrides} for rule in rules]
    return rules


@app.put("/api/alert-rules/{rid}")
async def api_rule_update(rid: str, body: RuleUpdate, site: str | None = None, user=Depends(current_user)):
    p = pool()
    name = await resolve_site(site, user)
    if name:
        require_site_admin(user, (await site_map(user))[name]['id'])
    else:
        from .access import require_owner
        require_owner(user)
    rule = await p.fetchrow("SELECT * FROM alert_rules WHERE id=$1", rid)
    if not rule:
        raise HTTPException(404, "Rule not found")
    data = body.model_dump(exclude_unset=True)
    if not data:
        return rowdict(rule)
    if any(key in data and data[key] is None for key in ('for_s','clear_for_s','hysteresis','enabled')):
        raise HTTPException(400, 'Timing, hysteresis and enabled settings cannot be null')
    if "warn" in data and "crit" in data and data["warn"] is None and data["crit"] is None:
        raise HTTPException(400, "At least one threshold is required")
    if name:
        if rule['entity_type'] == 'noc':
            raise HTTPException(403, 'Platform rules are managed by the dashboard owner')
        previous = await p.fetchval('SELECT values FROM site_rule_overrides WHERE site=$1 AND rule_id=$2', name, rid) or {}
        overrides = {**previous, **data}
        await p.execute('INSERT INTO site_rule_overrides (site,rule_id,values) VALUES ($1,$2,$3) ON CONFLICT (site,rule_id) DO UPDATE SET values=EXCLUDED.values, updated_at=now()', name, rid, overrides)
        await p.execute("INSERT INTO admin_audit (actor,action,target,site_id) SELECT $1,'site_rule_change',$2,id FROM sites WHERE name=$3", user['username'], rid, name)
        return {**rowdict(rule), **overrides, 'site_override': True}
    sets = ", ".join(f"{k}=${i + 2}" for i, k in enumerate(data))
    r = await p.fetchrow(f"UPDATE alert_rules SET {sets}, updated_at=now() WHERE id=$1 RETURNING *", rid, *data.values())
    await p.execute("""INSERT INTO events (id, ts, site, severity, event_type, category, title, message, source, metadata)
                       VALUES ($1, now(), '_all', 'info', 'ALERT_RULE_CHANGED', 'NOC', 'Alert rule changed', $2, 'noc', $3)""",
                    f"noc-rule-{rid}-{time.time_ns()}", f"{user['username']} changed alert rule {rid}: {data}", {"rule": rid, "changes": data})
    return rowdict(r)


# ------------------------------------------------------------------ topology
@app.get("/api/topology")
async def api_topology(site: str | None = None, clients: bool = True, user=Depends(current_user)):
    name = await resolve_sites(site, user)
    p = pool()
    sites = await site_map(user)
    devs = await p.fetch("SELECT mac, site, name, device_type, model, online, last_poll, uplink_mac, uplink_port, ip FROM devices WHERE site=ANY($1::text[])", name)
    firing = await p.fetch("SELECT entity_id, severity FROM alerts WHERE state='firing' AND site=ANY($1::text[])", name)
    sev = {}
    for a in firing:
        key = entity_device_mac(a["entity_id"]) or a["entity_id"]
        sev[key] = "critical" if "critical" in (sev.get(key), a["severity"]) else a["severity"]
    nodes, edges = [], []
    dev_by_mac = {d["mac"]: d for d in devs}
    for sname in name:
        s = sites[sname]
        wan = await kv_get(p, f"wan.{sname}") or {}
        health = await kv_get(p, f"health.{sname}") or {}
        inet_id = f"internet:{sname}"
        stale = freshness(wan.get("ts"), settings.poll_interval)["state"] != "LIVE"
        health_stale = freshness(health.get("ts"), settings.poll_interval)["state"] != "LIVE"
        wan_status = "unknown" if stale else "healthy" if wan.get("up") else "offline"
        nodes.append({"id": inet_id, "type": "internet", "site": sname, "label": f"Internet ({wan.get('isp') or 'ISP'})",
                  "status": wan_status, "detail": wan.get("ip")})
        nodes.append({"id": f"site:{sname}", "type": "site", "site": sname, "label": s["description"], "status":
                  "unknown" if health_stale else {"HEALTHY": "healthy", "WARNING": "warning", "CRITICAL": "critical"}.get(health.get("status"), "unknown"),
                  "detail": "Data stale" if health_stale else f"Health {health.get('health_score')}" if health.get("health_score") is not None else "No data", "slug": s["slug"]})
        for d in devs:
            if d["site"] != sname:
                continue
            device_stale = freshness(d["last_poll"].timestamp() if d["last_poll"] else None, settings.poll_interval)["state"] != "LIVE"
            st = "unknown" if device_stale else "offline" if not d["online"] else sev.get(d["mac"], "healthy")
            nodes.append({"id": d["mac"], "type": d["device_type"], "site": sname, "label": d["name"], "model": d["model"], "status": st, "detail": d["ip"]})
            if d["device_type"] == "gateway":
                edges.append({"source": f"site:{sname}", "target": inet_id, "kind": "wan", "status": wan_status})
                edges.append({"source": d["mac"], "target": f"site:{sname}", "kind": "site"})
            elif d["uplink_mac"] and d["uplink_mac"] in dev_by_mac:
                edges.append({"source": d["mac"], "target": d["uplink_mac"], "kind": "wired", "label": f"port {d['uplink_port']}" if d["uplink_port"] else None,
                              "status": "unknown" if device_stale else "healthy" if d["online"] else "offline"})
            else:
                gw = next((g for g in devs if g["site"] == sname and g["device_type"] == "gateway"), None)
                if gw and gw["mac"] != d["mac"]:
                    edges.append({"source": d["mac"], "target": gw["mac"], "kind": "inferred", "status": "unknown", "label": "uplink unknown"})
        if clients:
            cl = await p.fetch("SELECT mac, name, is_wired, ap_mac, sw_mac, sw_port, signal FROM clients WHERE site=$1 AND online", sname)
            for c in cl:
                parent = c["ap_mac"] if not c["is_wired"] else c["sw_mac"]
                if parent not in dev_by_mac:
                    continue
                cid = f"client:{sname}:{c['mac']}"
                poor = not c["is_wired"] and c["signal"] is not None and c["signal"] < -75
                nodes.append({"id": cid, "type": "client", "site": sname, "label": c["name"], "status": "unknown" if health_stale else sev.get(cid, "warning" if poor else "healthy"),
                              "detail": f"{c['signal']} dBm" if c["signal"] is not None else (f"port {c['sw_port']}" if c["sw_port"] else ""), "mac": c["mac"]})
                edges.append({"source": cid, "target": parent, "kind": "wifi" if not c["is_wired"] else "wired"})
    for t in await tunnels_summary(user):
        members = t.get("members", [])
        if len(members) == 2:
            gws = [next((d["mac"] for d in devs if d["site"] == m["site"] and d["device_type"] == "gateway"), None) for m in members]
            if all(gws):
                live = t.get("live") or {}
                status = {"CONNECTED": "healthy", "DEGRADED": "warning", "DOWN": "offline"}.get(live.get("status"), "unknown")
                if t["freshness"]["state"] != "LIVE":
                    status = "unknown"
                edges.append({"source": gws[0], "target": gws[1], "kind": "tunnel", "status": status, "tunnel": t["id"],
                              "label": f"SD-WAN {live.get('status', 'UNKNOWN')}" + (f" {live['latency_ms']:.0f} ms" if live.get("latency_ms") is not None else "")})
    return {"nodes": nodes, "edges": edges}


# ------------------------------------------------------------------ diagnostics
@app.get("/api/diagnostics")
async def api_diagnostics(user=Depends(admin_user)):
    p = pool()
    runtime=await kv_get(p,'connection.runtime') or {}
    sites = await site_map(user)
    per_site = {}
    for n in sites:
        hk = await kv_get(p, f"gw_hostkey.{n}") or {}
        per_site[n] = {
            "unifi_health": await kv_get(p, f"unifi_health.{n}"), "wan": await kv_get(p, f"wan.{n}"), "networks": await kv_get(p, f"networks.{n}"),
            "dashboard": await kv_get(p, f"dashboard.{n}"),
            "gateway_hostkey": {k: hk.get(k) for k in ("host", "fingerprint", "algorithm", "first_seen", "mode")} if hk else None,
            "events_cursor": await kv_get(p, f"events.cursor.{n}"),
        }
    return {
        "sites": sites, "per_site": per_site, "capabilities": await kv_get(p, "capabilities") or {},
        "tunnels_raw": await kv_get(p, "tunnels") or [], "controller": await kv_get(p, "controller.info") or {},
        "settings": {"controller_url": settings.controller_url, "monitored_sites": settings.monitored_sites, "poll_interval": settings.poll_interval,
                     "client_poll_interval": settings.client_poll_interval, "event_poll_interval": settings.event_poll_interval,
                     "slow_poll_interval": settings.slow_poll_interval, "live_poll_interval": settings.live_poll_interval,
                     "gateway_ssh_enabled": settings.gateway_ssh_enabled, "probe_enabled": settings.probe_enabled,
                     "probe_local_site": settings.probe_local_site, "metrics_retention_s": settings.metrics_retention,
                     "metrics_retention_5m_s": settings.metrics_retention_5m, "metrics_retention_1h_s": settings.metrics_retention_1h,
                     "log_retention_s": settings.log_retention, "ranges": list(RANGES), **runtime},
        "auth_events": [rowdict(r) for r in await p.fetch("SELECT ts, username, ip, event FROM auth_events ORDER BY ts DESC LIMIT 50")],
    }


@app.post("/api/diagnostics/hostkey/{site}/reset")
async def api_hostkey_reset(site: str, user=Depends(admin_user)):
    name = await resolve_site(site, user)
    p = pool()
    old = await kv_get(p, f"gw_hostkey.{name}")
    await p.execute("DELETE FROM kv_state WHERE key=$1", f"gw_hostkey.{name}")
    await p.execute("""INSERT INTO events (id, ts, site, severity, event_type, category, title, message, source, metadata)
                       VALUES ($1, now(), $2, 'medium', 'GATEWAY_HOSTKEY_RESET', 'NOC', 'Gateway host key reset', $3, 'noc', '{}'::jsonb)""",
                    f"noc-hkreset-{name}-{time.time_ns()}", name,
                    f"{user['username']} cleared pinned gateway host key {old.get('fingerprint') if old else ''}; next connection re-pins (trust on first use)")
    return {"ok": True}

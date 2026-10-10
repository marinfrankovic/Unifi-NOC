"""Network health scoring. Every point deducted has a recorded reason; no data -> UNKNOWN, never a guess."""
import time

from ..config import settings
from .store import State


def _status(score):
    if score is None:
        return "UNKNOWN"
    return "HEALTHY" if score >= 90 else "WARNING" if score >= 70 else "CRITICAL"


def site_health(st: State, site: str, wan_eid: str | None) -> dict:
    now = time.time()
    src = st.sources.get(f"devices.{site}") or {}
    max_age = settings.poll_interval * 3 + 10
    if not src.get("last_ok") or now - src["last_ok"] > max_age:
        age = int(now - src["last_ok"]) if src.get("last_ok") else None
        return {"health_score": None, "status": "UNKNOWN", "stale": True, "coverage": {"measured": 0, "total": 1}, "unavailable_checks": ["Controller inventory"],
                "reasons": [{"text": "DATA STALE: no successful controller poll" + (f" for {age} s" if age else ""), "impact": 0, "severity": "critical"}]}
    score = 100.0
    reasons = []

    def deduct(points, text, severity="warning", entity=None):
        nonlocal score
        score -= points
        reasons.append({"text": text, "impact": round(points), "severity": severity, "entity": entity})

    cur = [c for c in st.current.values() if c.site == site and now - c.ts <= max_age * 2]
    by = {}
    for c in cur:
        by.setdefault((c.entity_type, c.metric), []).append(c)

    if wan_eid:
        up = st.get_current(wan_eid, "up", max_age)
        if up == 0:
            deduct(40, "WAN is down", "critical", wan_eid)
        else:
            lat = st.get_current(wan_eid, "latency_ms", max_age)
            if lat is not None and lat >= 150:
                deduct(15, f"WAN latency {lat:.0f} ms", "critical", wan_eid)
            elif lat is not None and lat >= 60:
                deduct(8, f"WAN latency elevated ({lat:.0f} ms)", "warning", wan_eid)
            loss = st.get_current(wan_eid, "loss_pct", 900)
            if loss is not None and loss >= 5:
                deduct(20, f"WAN packet loss {loss:.1f}% (UniFi 5-min)", "critical", wan_eid)
            elif loss is not None and loss >= 1:
                deduct(8, f"WAN packet loss {loss:.1f}% (UniFi 5-min)", "warning", wan_eid)
            av = st.get_current(wan_eid, "availability_24h", max_age)
            if av is not None and av < 99.5:
                deduct(5, f"WAN availability {av:.2f}% over 24 h", "warning", wan_eid)

    offline_pts = {"gateway": (40, 40), "switch": (15, 30), "ap": (10, 30)}
    for kind, (each, cap) in offline_pts.items():
        down = [c for c in by.get((kind, "online"), []) if c.value < 1]
        if down:
            deduct(min(each * len(down), cap), f"{len(down)} {kind}{'s' if len(down) > 1 else ''} offline: " + ", ".join(c.entity_name for c in down[:4]),
                   "critical", down[0].entity_id)
    for kind in ("gateway", "switch", "ap"):
        for c in by.get((kind, "cpu_pct"), []):
            if c.value >= 90:
                deduct(6, f"{c.entity_name} CPU {c.value:.0f}%", "warning", c.entity_id)
        for c in by.get((kind, "mem_pct"), []):
            if c.value >= 92:
                deduct(4, f"{c.entity_name} memory {c.value:.0f}%", "warning", c.entity_id)
        for c in by.get((kind, "temp_c"), []):
            if c.value >= 85:
                deduct(8, f"{c.entity_name} temperature {c.value:.0f} °C", "warning", c.entity_id)

    err_ports = [c for c in by.get(("port", "err_per_min"), []) if c.value >= 10]
    if err_ports:
        deduct(min(5 * len(err_ports), 10), "Interface errors on " + ", ".join(c.entity_name for c in err_ports[:3]), "warning", err_ports[0].entity_id)
    hot_ports = [c for c in by.get(("port", "util_pct"), []) if c.value >= 90]
    if hot_ports:
        deduct(min(4 * len(hot_ports), 8), "High port utilization: " + ", ".join(f"{c.entity_name} {c.value:.0f}%" for c in hot_ports[:3]), "warning", hot_ports[0].entity_id)
    flap = [c for c in by.get(("port", "flaps_15m"), []) if c.value >= 3]
    if flap:
        deduct(min(5 * len(flap), 10), "Port flapping: " + ", ".join(c.entity_name for c in flap[:3]), "warning", flap[0].entity_id)

    busy = [c for c in by.get(("radio", "cu_total"), []) if c.value >= 70]
    if busy:
        deduct(min(5 * len(busy), 10), "High channel utilization: " + ", ".join(f"{c.entity_name} {c.value:.0f}%" for c in busy[:3]), "warning", busy[0].entity_id)
    retry = [c for c in by.get(("radio", "tx_retries_pct"), []) if c.value >= 25]
    if retry:
        deduct(min(5 * len(retry), 10), "High Wi-Fi retries: " + ", ".join(f"{c.entity_name} {c.value:.0f}%" for c in retry[:3]), "warning", retry[0].entity_id)

    disc = st.get_current(f"site:{site}", "disconnects_15m", 300)
    if disc is not None and disc >= 20:
        deduct(5, f"{int(disc)} client disconnects in 15 min", "warning")
    poor = st.get_current(f"site:{site}", "clients_poor_signal", 120)
    if poor is not None and poor >= 3:
        deduct(3, f"{int(poor)} Wi-Fi clients with poor signal (< -75 dBm)", "info")

    for tid, t in st.tunnels.items():
        if any(m["site"] == site for m in t.get("members", [])):
            th = st.health.get(f"tunnel:{tid}") or {}
            if th.get("status") == "DOWN":
                deduct(25, f"Site-to-site {t.get('label')} DOWN", "critical", f"tunnel:{tid}")
            elif th.get("status") == "DEGRADED":
                deduct(10, f"Site-to-site {t.get('label')} degraded ({th.get('health_score')})", "warning", f"tunnel:{tid}")

    final = max(0, round(score))
    checks = {"Controller inventory": True}
    if wan_eid:
        checks.update({f"WAN {metric}": st.get_current(wan_eid, metric, 900 if metric == 'loss_pct' else max_age) is not None
                       for metric in ('up', 'latency_ms', 'loss_pct', 'availability_24h')})
    for kind in ('gateway', 'switch', 'ap'):
        devices = [item for item in st.devices.values() if item.get('_site') == site and item.get('_kind') == kind]
        if devices:
            checks.update({f"{kind} {metric}": bool(by.get((kind, metric))) for metric in ('online', 'cpu_pct', 'mem_pct')})
            temperature_supported = any(item.get('has_temperature') or item.get('temperatures') for item in devices) or any(
                metric.site == site and metric.entity_type == kind and metric.metric == 'temp_c' for metric in st.current.values())
            if temperature_supported:
                checks[f"{kind} temp_c"] = bool(by.get((kind, 'temp_c')))
    if any(kind == 'port' for kind, metric in by):
        checks.update({f"Port {metric}": bool(by.get(('port', metric))) for metric in ('err_per_min', 'util_pct', 'flaps_15m')})
    if any(kind == 'radio' for kind, metric in by):
        checks.update({f"Radio {metric}": bool(by.get(('radio', metric))) for metric in ('cu_total', 'tx_retries_pct')})
    checks.update({f"Clients {metric}": st.get_current(f'site:{site}', metric, 120) is not None for metric in ('disconnects_15m', 'clients_poor_signal')})
    for tid, tunnel in st.tunnels.items():
        if any(member['site'] == site for member in tunnel.get('members', [])):
            health = st.health.get(f'tunnel:{tid}') or {}
            checks[f"Tunnel {tunnel.get('label', tid)}"] = health.get('status') not in (None, 'UNKNOWN') and health.get('coverage', {}).get('measured', 0) == health.get('coverage', {}).get('total', 1)
    unavailable = [name for name, available in checks.items() if not available]
    status = _status(final)
    if any(reason['severity'] == 'critical' for reason in reasons):
        status = 'CRITICAL'
    elif status == 'HEALTHY' and any(reason['severity'] == 'warning' for reason in reasons):
        status = 'WARNING'
    return {"health_score": final, "status": "UNKNOWN" if status == 'HEALTHY' and unavailable else status, "stale": False, "reasons": reasons,
            "coverage": {"measured": len(checks) - len(unavailable), "total": len(checks)}, "unavailable_checks": unavailable}


def overall_health(site_scores: dict[str, dict], tunnel_scores: dict[str, dict], names: dict[str, str]) -> dict:
    vals = [h["health_score"] for h in site_scores.values() if h.get("health_score") is not None]
    reasons = []
    coverage = {"measured": len(vals), "total": len(site_scores)}
    for s, h in site_scores.items():
        for r in h.get("reasons", []):
            reasons.append({**r, "text": f"{names.get(s, s)}: {r['text']}"})
    statuses = [_status(value) for value in vals]
    for h in [*site_scores.values(), *tunnel_scores.values()]:
        if h.get("status") in ("CRITICAL", "DOWN"):
            statuses.append("CRITICAL")
        elif h.get("status") in ("WARNING", "DEGRADED"):
            statuses.append("WARNING")
    worst = "CRITICAL" if "CRITICAL" in statuses else "WARNING" if "WARNING" in statuses else "HEALTHY"
    missing = [names.get(site, site) for site, h in site_scores.items() if h.get("health_score") is None]
    if missing:
        reasons.append({"text": "No current data for: " + ", ".join(missing), "impact": 0, "severity": "critical"})
    complete = bool(vals) and not missing
    return {"health_score": round(sum(vals) / len(vals)) if complete else None,
            "status": "UNKNOWN" if worst == "HEALTHY" and (not complete or any(h.get('status') == 'UNKNOWN' for h in site_scores.values())) else worst,
            "reasons": reasons, "coverage": coverage}

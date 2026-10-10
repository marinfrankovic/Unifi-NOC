"""Alert engine (debounce + hysteresis) and lightweight statistical anomaly detection."""
import logging
import operator
import time
from dataclasses import dataclass, field

from ..config import settings
from .poll import Ctx
from .store import utcnow

log = logging.getLogger("noc.alerts")

OPS = {">": operator.gt, "<": operator.lt, ">=": operator.ge, "<=": operator.le}
DEVICE_KINDS = ("gateway", "switch", "ap")

# id, name, entity_type, metric, op, warn, crit, for_s, clear_for_s, hysteresis, unit, description
DEFAULT_RULES = [
    ("wan_down", "WAN down", "wan", "up", "<", None, 1, 30, 60, 0, "", "Gateway reports the WAN uplink down"),
    ("wan_latency", "WAN latency high", "wan", "latency_ms", ">", 60, 150, 120, 120, 10, "ms", "Gateway WAN latency"),
    ("wan_packet_loss", "WAN packet loss", "wan", "loss_pct", ">", 2, 10, 0, 300, 0.5, "%", "UniFi 5-minute WAN packet loss"),
    ("wan_errors", "WAN interface errors", "wan", "err_per_min", ">", 10, 100, 120, 300, 0, "/min", "Physical WAN port errors"),
    ("gateway_offline", "Gateway offline", "gateway", "online", "<", None, 1, 45, 60, 0, "", "Gateway not connected to controller"),
    ("switch_offline", "Switch offline", "switch", "online", "<", None, 1, 60, 60, 0, "", "Switch not connected to controller"),
    ("ap_offline", "Access point offline", "ap", "online", "<", None, 1, 60, 60, 0, "", "AP not connected to controller"),
    ("device_cpu", "High CPU", "device", "cpu_pct", ">", 85, 95, 300, 300, 5, "%", "Device CPU utilization"),
    ("device_memory", "High memory", "device", "mem_pct", ">", 90, 96, 300, 300, 2, "%", "Device memory utilization"),
    ("device_temperature", "High temperature", "device", "temp_c", ">", 80, 90, 300, 300, 3, "°C", "Device temperature"),
    ("poe_budget", "High PoE consumption", "switch", "poe_used_pct", ">", 80, 95, 300, 300, 3, "%", "PoE budget used"),
    ("port_utilization", "High interface utilization", "port", "util_pct", ">", 80, 95, 300, 300, 5, "%", "Port throughput vs negotiated speed"),
    ("port_errors", "Interface errors", "port", "err_per_min", ">", 10, 100, 120, 300, 0, "/min", "RX+TX error rate"),
    ("port_flapping", "Port flapping", "port", "flaps_15m", ">=", 3, 6, 0, 900, 0, "", "Link-down transitions in 15 minutes"),
    ("wifi_utilization", "High Wi-Fi channel utilization", "radio", "cu_total", ">", 70, 85, 600, 600, 5, "%", "Channel airtime utilization"),
    ("wifi_retries", "High Wi-Fi retries", "radio", "tx_retries_pct", ">", 20, 35, 600, 600, 3, "%", "Transmit retry percentage"),
    ("client_signal", "Poor client signal", "client", "signal_dbm", "<", -75, -82, 900, 600, 3, "dBm", "Wi-Fi client signal strength"),
    ("client_disconnects", "Excessive client disconnects", "site", "disconnects_15m", ">", 20, 50, 0, 900, 0, "", "Client disconnect events in 15 minutes"),
    ("tunnel_down", "Site-to-site tunnel down", "tunnel", "connected", "<", None, 1, 30, 60, 0, "", "Controller reports the SD-WAN tunnel disconnected"),
    ("tunnel_loss", "Site-to-site packet loss", "tunnel", "loss_pct_1m", ">", 2, 10, 60, 180, 0.5, "%", "ICMP loss through the tunnel (1 min window)"),
    ("tunnel_latency", "Site-to-site latency", "tunnel", "latency_ms", ">", 120, 200, 120, 180, 10, "ms", "ICMP round trip through the tunnel"),
    ("tunnel_jitter", "Site-to-site jitter", "tunnel", "jitter_ms", ">", 20, 50, 120, 180, 3, "ms", "ICMP jitter through the tunnel"),
    ("tunnel_reconnects", "Repeated tunnel reconnects", "tunnel", "reconnects_1h", ">=", 2, 5, 0, 600, 0, "", "Re-associations / interface resets in 1 hour"),
    ("tunnel_handshake", "Stale WireGuard handshake", "tunnel", "handshake_age_s", ">", 300, 600, 60, 60, 0, "s", "Time since last WireGuard handshake"),
    ("tunnel_health", "Site-to-site health degraded", "tunnel", "health_score", "<", 80, 50, 120, 180, 5, "", "Composite SITE-TO-SITE HEALTH score"),
    ("collector_stale", "Data collection error", "noc", "stale_ratio", ">", None, 3, 0, 30, 0, "x", "Collector source has not succeeded for >3 poll intervals"),
    ("gateway_hostkey", "Gateway SSH host key changed", "noc", "mismatch", ">=", None, 1, 0, 60, 0, "", "Pinned host key no longer matches"),
]


async def seed_rules(pool) -> None:
    for r in DEFAULT_RULES:
        await pool.execute(
            """INSERT INTO alert_rules (id, name, entity_type, metric, op, warn, crit, for_s, clear_for_s, hysteresis, unit, description)
               VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12) ON CONFLICT (id) DO NOTHING""", *r)
    for rid, ov in (settings.alert_thresholds or {}).items():
        fields = {k: v for k, v in ov.items() if k in ("warn", "crit", "for_s", "clear_for_s", "hysteresis", "enabled")}
        if not fields:
            continue
        sets = ", ".join(f"{k}=${i + 2}" for i, k in enumerate(fields))
        await pool.execute(f"UPDATE alert_rules SET {sets}, updated_at=now() WHERE id=$1", rid, *fields.values())
        log.info("applied ALERT_THRESHOLDS override for %s: %s", rid, fields)


@dataclass
class Track:
    level: str | None = None          # active severity
    alert_id: int | None = None
    pending_level: str | None = None
    pending_since: float | None = None
    clear_since: float | None = None
    peak: float | None = None
    missing_since: float | None = None
    meta: dict = field(default_factory=dict)


class AlertEngine:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.tracks: dict[tuple[str, str], Track] = {}
        self.rules: list[dict] = []
        self.rules_loaded = 0.0

    async def load(self) -> None:
        rows = await self.ctx.pool.fetch("SELECT * FROM alerts WHERE state='firing' AND kind='threshold'")
        for r in rows:
            self.tracks[(r["rule_id"], r["entity_id"])] = Track(level=r["severity"], alert_id=r["id"], peak=r["peak_value"])

    async def _rules(self) -> list[dict]:
        if time.time() - self.rules_loaded > 30:
            self.rules = [dict(r) for r in await self.ctx.pool.fetch("SELECT * FROM alert_rules WHERE enabled")]
            overrides = await self.ctx.pool.fetch('SELECT site, rule_id, values FROM site_rule_overrides')
            for rule in self.rules:
                rule['_site_overrides'] = {row['site']: row['values'] for row in overrides if row['rule_id'] == rule['id']}
            self.rules_loaded = time.time()
        return self.rules

    @staticmethod
    def _matches(rule_type: str, etype: str) -> bool:
        return rule_type == etype or (rule_type == "device" and etype in DEVICE_KINDS)

    async def evaluate(self) -> None:
        st = self.ctx.state
        now = time.time()
        rules = await self._rules()
        active_rule_ids = {r["id"] for r in rules}
        seen = set()
        max_age = max(settings.poll_interval, settings.client_poll_interval) * 3 + 15
        for base_rule in rules:
            cmp = OPS[base_rule["op"]]
            for (eid, metric), c in list(st.current.items()):
                if metric != base_rule["metric"] or not self._matches(base_rule["entity_type"], c.entity_type):
                    continue
                rule = {**base_rule, **base_rule.get('_site_overrides', {}).get(c.site, {})}
                if not rule.get('enabled', True):
                    key = (rule['id'], eid)
                    track = self.tracks.pop(key, None)
                    if track and track.level:
                        await self._resolve(track, None, 'site rule disabled')
                    seen.add(key)
                    continue
                if now - c.ts > (900 if metric == "loss_pct" else max_age):
                    continue  # stale values never fire or clear alerts
                key = (rule["id"], eid)
                seen.add(key)
                level = None
                if rule["crit"] is not None and cmp(c.value, rule["crit"]):
                    level = "critical"
                elif rule["warn"] is not None and cmp(c.value, rule["warn"]):
                    level = "warning"
                await self._step(rule, key, c, level, now)
        # Tracks whose metric disappeared (device offline, client left) resolve after 10 minutes without data.
        for key, tr in list(self.tracks.items()):
            if key in seen:
                tr.missing_since = None
                continue
            if tr.level is None:
                self.tracks.pop(key, None)
                continue
            if key[0] not in active_rule_ids:
                await self._resolve(tr, None, "rule disabled")
                self.tracks.pop(key, None)
                continue
            tr.missing_since = tr.missing_since or now
            if now - tr.missing_since > 600:
                await self._resolve(tr, None, "metric no longer reported")
                self.tracks.pop(key, None)

    def _threshold(self, rule, level):
        return rule["crit"] if level == "critical" else rule["warn"]

    async def _step(self, rule, key, c, level, now) -> None:
        tr = self.tracks.setdefault(key, Track())
        if level:
            tr.clear_since = None
            if tr.level is None:
                if tr.pending_since is None:
                    tr.pending_since = now
                tr.pending_level = "critical" if "critical" in (level, tr.pending_level) else level
                if now - tr.pending_since >= rule["for_s"]:
                    await self._fire(rule, tr, c, level)
            else:
                worse = rule["op"] in (">", ">=") and c.value > (tr.peak or c.value) or rule["op"] in ("<", "<=") and c.value < (tr.peak or c.value)
                if worse:
                    tr.peak = c.value
                new_level = level if level == "critical" or tr.level != "critical" else tr.level
                if level == "warning" and tr.level == "critical":
                    # de-escalate only once below the critical threshold by the hysteresis margin
                    crit = rule["crit"]
                    margin_ok = (c.value <= crit - rule["hysteresis"]) if rule["op"] in (">", ">=") else (c.value >= crit + rule["hysteresis"])
                    new_level = "warning" if margin_ok else "critical"
                await self._update(rule, tr, c, new_level)
        else:
            tr.pending_since = None
            tr.pending_level = None
            if tr.level is None:
                return
            thr = rule["warn"] if rule["warn"] is not None else rule["crit"]
            ok = (c.value <= thr - rule["hysteresis"]) if rule["op"] in (">", ">=") else (c.value >= thr + rule["hysteresis"])
            if rule["op"] == ">=" and rule["hysteresis"] == 0:
                ok = c.value < thr
            if rule["op"] == "<=" and rule["hysteresis"] == 0:
                ok = c.value > thr
            if not ok:
                return
            tr.clear_since = tr.clear_since or now
            if now - tr.clear_since >= rule["clear_for_s"]:
                await self._resolve(tr, c.value)
                self.tracks.pop(key, None)

    def _message(self, rule, c, level) -> str:
        unit = rule.get("unit") or ""
        thr = self._threshold(rule, level)
        if rule["metric"] in ("online", "up", "connected") and c.value < 1:
            return f"{c.entity_name}: {rule['name']}"
        return f"{c.entity_name}: {rule['name']} — {c.value:.1f}{unit} {rule['op']} {thr:g}{unit}"

    async def _fire(self, rule, tr: Track, c, level) -> None:
        tr.level, tr.peak = level, c.value
        thr = self._threshold(rule, level)
        msg = self._message(rule, c, level)
        started = utcnow()
        row = await self.ctx.pool.fetchrow(
            """INSERT INTO alerts (kind, rule_id, site, entity_type, entity_id, entity_name, metric, condition, threshold, value, peak_value,
                   severity, state, message, started_at, updated_at, metadata)
               VALUES ('threshold',$1,$2,$3,$4,$5,$6,$7,$8,$9,$9,$10,'firing',$11,$12,$12,$13)
               ON CONFLICT (kind, coalesce(rule_id, ''), entity_id, metric) WHERE state = 'firing'
               DO UPDATE SET severity=EXCLUDED.severity, value=EXCLUDED.value, updated_at=EXCLUDED.updated_at
               RETURNING id""",
            rule["id"], c.site, c.entity_type, c.entity_id, c.entity_name, rule["metric"], f"{rule['metric']} {rule['op']} {thr:g}",
            thr, c.value, level, msg, started, {"for_s": rule["for_s"], "unit": rule.get("unit")},
        )
        tr.alert_id = row["id"]
        await self.ctx.event(c.site, "ALERT_FIRED", f"[{level.upper()}] {msg}", "critical" if level == "critical" else "high",
                             metadata={"alert_id": tr.alert_id, "rule": rule["id"], "entity": c.entity_id})
        log.info("alert fired: %s", msg)

    async def _update(self, rule, tr: Track, c, level) -> None:
        if tr.alert_id is None:
            return
        changed = level != tr.level
        tr.level = level
        thr = self._threshold(rule, level)
        await self.ctx.pool.execute(
            "UPDATE alerts SET value=$2, peak_value=$3, severity=$4, threshold=$5, condition=$6, message=$7, updated_at=now() WHERE id=$1",
            tr.alert_id, c.value, tr.peak, level, thr, f"{rule['metric']} {rule['op']} {thr:g}", self._message(rule, c, level))
        if changed:
            await self.ctx.event(c.site, "ALERT_SEVERITY_CHANGED", f"[{level.upper()}] {self._message(rule, c, level)}", "medium",
                                 metadata={"alert_id": tr.alert_id})

    async def _resolve(self, tr: Track, value, note: str | None = None) -> None:
        if tr.alert_id is None:
            return
        row = await self.ctx.pool.fetchrow(
            """UPDATE alerts SET state='resolved', resolved_at=now(), updated_at=now(), value=coalesce($2, value),
                   metadata = metadata || jsonb_build_object('resolution', $3::text) WHERE id=$1 AND state='firing' RETURNING site, message""",
            tr.alert_id, value, note or "condition cleared")
        if row:
            await self.ctx.event(row["site"], "ALERT_RESOLVED", f"[RESOLVED] {row['message']}" + (f" ({note})" if note else ""), "info",
                                 metadata={"alert_id": tr.alert_id})


# ------------------------------------------------------------------ anomaly detection
# (entity_type, metric, direction, min_abs_delta, label, unit, scale)
ANOMALY_SPECS = [
    ("wan", "latency_ms", "up", 10, "WAN latency", "ms", 1),
    ("wan", "rx_bps", "both", 5e6, "WAN download traffic", "Mbps", 1e-6),
    ("wan", "tx_bps", "both", 5e6, "WAN upload traffic", "Mbps", 1e-6),
    ("tunnel", "latency_ms", "up", 15, "Site-to-site latency", "ms", 1),
    ("tunnel", "loss_pct_1m", "up", 1, "Site-to-site packet loss", "%", 1),
    ("tunnel", "rx_bps", "both", 2e6, "Site-to-site traffic (RX)", "Mbps", 1e-6),
    ("tunnel", "tx_bps", "both", 2e6, "Site-to-site traffic (TX)", "Mbps", 1e-6),
    ("radio", "tx_retries_pct", "up", 8, "Wi-Fi retries", "%", 1),
    ("site", "disconnects_15m", "up", 8, "Client disconnects", "/15 min", 1),
]


async def detect_anomalies(ctx: Ctx) -> None:
    pool = ctx.pool
    for etype, metric, direction, floor, label, unit, scale in ANOMALY_SPECS:
        rows = await pool.fetch(
            """WITH base AS (
                   SELECT entity_id, site, avg AS v FROM metrics_5m
                   WHERE entity_type=$1 AND metric=$2 AND bucket > now() - interval '7 days' AND bucket < now() - interval '1 hour'),
               stats AS (
                   SELECT entity_id, max(site) AS site, count(*) AS n,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY v) AS med,
                          percentile_cont(0.1) WITHIN GROUP (ORDER BY v) AS p10,
                          percentile_cont(0.9) WITHIN GROUP (ORDER BY v) AS p90
                   FROM base GROUP BY entity_id),
               mad AS (
                   SELECT b.entity_id, percentile_cont(0.5) WITHIN GROUP (ORDER BY abs(b.v - s.med)) AS mad
                   FROM base b JOIN stats s USING (entity_id) GROUP BY b.entity_id),
               cur AS (
                   SELECT entity_id, avg(value) AS cur, count(*) AS cn FROM metrics
                   WHERE entity_type=$1 AND metric=$2 AND ts > now() - interval '15 minutes' GROUP BY entity_id)
               SELECT s.*, m.mad, c.cur, c.cn FROM stats s JOIN mad m USING (entity_id) JOIN cur c USING (entity_id)
               WHERE s.n >= 144""", etype, metric)
        for r in rows:
            spread = max(1.4826 * (r["mad"] or 0), floor / 3)
            z = (r["cur"] - r["med"]) / spread if spread else 0
            delta = r["cur"] - r["med"]
            is_anom = abs(z) >= 4 and abs(delta) >= floor and (direction == "both" or (direction == "up" and delta > 0) or (direction == "down" and delta < 0))
            rule_id = f"anomaly:{metric}"
            firing = await pool.fetchrow("SELECT id, metadata FROM alerts WHERE kind='anomaly' AND state='firing' AND rule_id=$1 AND entity_id=$2", rule_id, r["entity_id"])
            name = ctx.state.current.get((r["entity_id"], metric))
            ename = name.entity_name if name else r["entity_id"]
            fmt = (lambda v: f"{v * scale:.1f}")
            if is_anom and not firing:
                kind = "unusually high" if delta > 0 else "sudden drop"
                msg = (f"{label.upper()} ANOMALY ({kind}) — {ename}: {fmt(r['cur'])} {unit}, normal {fmt(r['p10'])}–{fmt(r['p90'])} {unit} "
                       f"(7-day baseline). Statistical deviation, not a confirmed failure.")
                await pool.execute(
                    """INSERT INTO alerts (kind, rule_id, site, entity_type, entity_id, entity_name, metric, condition, threshold, value, peak_value,
                           severity, state, message, started_at, updated_at, metadata)
                       VALUES ('anomaly',$1,$2,$3,$4,$5,$6,$7,$8,$9,$9,'info','firing',$10,now(),now(),$11) ON CONFLICT DO NOTHING""",
                    rule_id, r["site"], etype, r["entity_id"], ename, metric, f"|z| >= 4 vs 7-day median", r["med"], r["cur"], msg,
                    {"median": r["med"], "p10": r["p10"], "p90": r["p90"], "mad": r["mad"], "z": round(z, 2), "baseline_points": r["n"], "unit": unit, "scale": scale})
                await ctx.event(r["site"], "ANOMALY_DETECTED", msg, "medium", metadata={"entity": r["entity_id"], "metric": metric, "z": round(z, 2)})
            elif firing:
                meta = firing["metadata"] or {}
                calm = abs(z) < 2
                meta["calm"] = (meta.get("calm", 0) + 1) if calm else 0
                if meta["calm"] >= 2:
                    await pool.execute("UPDATE alerts SET state='resolved', resolved_at=now(), updated_at=now(), value=$2, metadata=$3 WHERE id=$1",
                                       firing["id"], r["cur"], meta)
                    await ctx.event(r["site"], "ANOMALY_CLEARED", f"{label} back within normal range — {ename}: {fmt(r['cur'])} {unit}", "info")
                else:
                    await pool.execute("UPDATE alerts SET value=$2, peak_value=greatest(peak_value,$2), updated_at=now(), metadata=$3 WHERE id=$1",
                                       firing["id"], r["cur"], meta)

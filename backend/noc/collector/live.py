"""Near-real-time collection (default 5 s): gateway interface counters over SSH and ICMP probes through the tunnel.

Gateway SSH is read-only (`cat /proc/net/dev`, `wg show`), uses the device SSH credentials that are already
configured on the controller, and pins each gateway host key (trust on first use, alert on change).
"""
import asyncio
import logging
import statistics
import time

import asyncssh
from icmplib import async_ping

from ..config import settings
from ..db import kv_get, kv_set
from .poll import Ctx
from .store import MetricBuffer, utcnow

log = logging.getLogger("noc.live")

CMD = ("date +%s; echo @@; cat /proc/net/dev; echo @@; wg show all latest-handshakes 2>/dev/null; echo @@; "
       "wg show all endpoints 2>/dev/null; echo @@; cat /proc/loadavg")
KEEP_IFACES = ("ppp", "wgsts", "eth", "br0")


class HostKeyMismatch(Exception):
    pass


def parse_sample(text: str) -> dict:
    parts = text.split("@@")
    if len(parts) < 5:
        raise ValueError("unexpected gateway output")
    gw_ts = int(parts[0].strip() or 0)
    ifaces = {}
    for line in parts[1].splitlines():
        if ":" not in line or "|" in line:
            continue
        name, data = line.split(":", 1)
        name = name.strip()
        f = data.split()
        if len(f) < 16 or not name.startswith(KEEP_IFACES):
            continue
        v = [int(x) for x in f[:16]]
        ifaces[name] = {"rx_bytes": v[0], "rx_packets": v[1], "rx_errs": v[2], "rx_drop": v[3],
                        "tx_bytes": v[8], "tx_packets": v[9], "tx_errs": v[10], "tx_drop": v[11]}
    hs: dict[str, list] = {}
    for line in parts[2].splitlines():
        f = line.split()
        if len(f) == 3 and f[2].isdigit():
            hs.setdefault(f[0], []).append((f[1], int(f[2])))
    eps: dict[str, list] = {}
    for line in parts[3].splitlines():
        f = line.split()
        if len(f) == 3:
            eps.setdefault(f[0], []).append((f[1], f[2].rsplit(":", 1)[0].strip("[]")))
    load = parts[4].split()
    return {"gw_ts": gw_ts, "ifaces": ifaces, "handshakes": hs, "endpoints": eps, "load1": float(load[0]) if load else None}


class GatewayLink:
    def __init__(self, ctx: Ctx, site: str):
        self.ctx = ctx
        self.site = site
        self.conn: asyncssh.SSHClientConnection | None = None
        self.creds: tuple[str, str] | None = None
        self.creds_at = 0.0
        self.retry_at = 0.0
        self.host: str | None = None
        self.last_error: str | None = None

    def gateway_host(self) -> str | None:
        st = self.ctx.state
        for t in st.tunnels.values():
            for m in t.get("members", []):
                if m["site"] == self.site and m.get("router_ip"):
                    return m["router_ip"]
        for d in st.devices.values():
            if d.get("_site") == self.site and d.get("_kind") == "gateway":
                for n in d.get("reported_networks") or []:
                    if str(n.get("name", "")).startswith("br") and n.get("ip"):
                        return n["ip"]
        return None

    async def _trusted_key(self, host: str):
        key_name = f"gw_hostkey.{self.site}"
        stored = await kv_get(self.ctx.pool, key_name)
        if stored and stored.get("host") == host and stored.get("key"):
            return asyncssh.import_public_key(stored["key"]), stored
        key = await asyncio.wait_for(asyncssh.get_server_host_key(host, 22, server_host_key_algs=["ssh-ed25519"]), timeout=10)
        if key is None:
            raise ConnectionError("gateway did not present a host key")
        stored = {"host": host, "key": key.export_public_key().decode().strip(), "fingerprint": key.get_fingerprint(),
                  "algorithm": key.get_algorithm(), "first_seen": time.time(), "mode": "trust-on-first-use"}
        await kv_set(self.ctx.pool, key_name, stored)
        await self.ctx.event(self.site, "GATEWAY_HOSTKEY_TRUSTED", f"Pinned gateway SSH host key {stored['fingerprint']} for {host} (trust on first use)", "info")
        return key, stored

    async def ensure(self) -> bool:
        if self.conn is not None and not self.conn.is_closed():
            return True
        if time.time() < self.retry_at:
            return False
        self.host = self.gateway_host()
        if not self.host:
            self.last_error = "gateway LAN address unknown"
            return False
        try:
            if not self.creds or time.time() - self.creds_at > 3600:
                self.creds = await self.ctx.unifi.mgmt_ssh_credentials(self.site)
                self.creds_at = time.time()
            if not self.creds:
                self.last_error = "device SSH is not enabled/configured on the controller"
                self.retry_at = time.time() + 600
                return False
            key, stored = await self._trusted_key(self.host)
            self.conn = await asyncio.wait_for(asyncssh.connect(
                self.host, username=self.creds[0], password=self.creds[1], known_hosts=([key], [], []),
                server_host_key_algs=[stored.get("algorithm") or "ssh-ed25519"], client_keys=None, agent_path=None,
                keepalive_interval=30, keepalive_count_max=3, connect_timeout=8), timeout=12)
            self.last_error = None
            self.ctx.state.put_current(self.site, "noc", f"noc:hostkey:{self.site}", f"{self.site} gateway host key", "mismatch", 0)
            log.info("gateway SSH connected: %s (%s)", self.site, self.host)
            return True
        except asyncssh.HostKeyNotVerifiable as exc:
            self.last_error = f"HOST KEY MISMATCH: {exc}"
            self.retry_at = time.time() + 600
            await self.ctx.event(self.site, "GATEWAY_HOSTKEY_MISMATCH",
                                 f"Gateway {self.host} presented an SSH host key that does not match the pinned key. Live counters disabled until verified.",
                                 "critical")
            self.ctx.state.put_current(self.site, "noc", f"noc:hostkey:{self.site}", f"{self.site} gateway host key", "mismatch", 1)
            return False
        except (OSError, asyncssh.Error, asyncio.TimeoutError, ConnectionError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            self.retry_at = time.time() + 30
            return False

    async def sample(self) -> dict | None:
        if not await self.ensure():
            return None
        try:
            r = await asyncio.wait_for(self.conn.run(CMD, check=False), timeout=8)
            return parse_sample(r.stdout or "")
        except (asyncssh.Error, asyncio.TimeoutError, OSError, ValueError) as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:300]
            try:
                self.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self.conn = None
            return None


class LiveCollector:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.links: dict[str, GatewayLink] = {}
        self.samples: dict[str, dict] = {}

    def link(self, site: str) -> GatewayLink:
        if site not in self.links:
            self.links[site] = GatewayLink(self.ctx, site)
        return self.links[site]

    async def run_once(self) -> dict:
        st = self.ctx.state
        buf = MetricBuffer()
        now = utcnow()
        mono = time.monotonic()
        results = {}
        if settings.gateway_ssh_enabled:
            sites = list(st.sites)
            samples = await asyncio.gather(*(self.link(s).sample() for s in sites))
            for site, sample in zip(sites, samples):
                results[site] = {"ok": sample is not None, "host": self.link(site).host, "error": self.link(site).last_error}
                if sample:
                    self._process_gateway(site, sample, buf, now, mono)
        probe_res = await self._probes(buf, now) if settings.probe_enabled else {}
        for tid, t in st.tunnels.items():
            await self._tunnel_rollup(tid, t, buf, now, probe_res)
        await buf.flush(self.ctx.pool)
        return {"gateways": results, "probes": {k: {kk: v[kk] for kk in ("rtt_ms", "loss_pct")} for k, v in probe_res.items()}}

    def _process_gateway(self, site: str, s: dict, buf: MetricBuffer, now, mono: float) -> None:
        st = self.ctx.state
        wanif = self.ctx.flags.get(f"wanif:{site}") or {}
        self.samples[site] = {"ts": time.time(), **s}
        for name, c in s["ifaces"].items():
            prev_rx = self.samples.get(f"{site}:{name}:prev")
            if prev_rx is not None and c["rx_bytes"] < prev_rx:
                self.samples[f"{site}:{name}:reset"] = time.time()
            self.samples[f"{site}:{name}:prev"] = c["rx_bytes"]
            r = st.rate(f"gwif:{site}:{name}", {k: float(v) for k, v in c.items()}, mono)
            if "rx_bytes" not in r or "tx_bytes" not in r:
                continue
            eid = f"gwif:{site}:{name}"
            vals = {"rx_bps": r.get("rx_bytes", 0) * 8, "tx_bps": r.get("tx_bytes", 0) * 8,
                    "rx_pps": r.get("rx_packets"), "tx_pps": r.get("tx_packets"),
                    "err_per_min": (r.get("rx_errs", 0) + r.get("tx_errs", 0)) * 60, "drop_per_min": (r.get("rx_drop", 0) + r.get("tx_drop", 0)) * 60}
            for m, v in vals.items():
                buf.add(now, site, "gw_iface", eid, m, v)
            if wanif.get("ifname") == name:
                buf.add(now, site, "wan", wanif["entity_id"], "live_rx_bps", vals["rx_bps"])
                buf.add(now, site, "wan", wanif["entity_id"], "live_tx_bps", vals["tx_bps"])
                st.put_current(site, "wan", wanif["entity_id"], wanif.get("label"), "live_rx_bps", vals["rx_bps"])
                st.put_current(site, "wan", wanif["entity_id"], wanif.get("label"), "live_tx_bps", vals["tx_bps"])
                self.samples[f"{site}:wanlive"] = {"ts": time.time(), "rx_bps": vals["rx_bps"], "tx_bps": vals["tx_bps"]}
            self.samples[f"{site}:{name}:rate"] = {"ts": time.time(), **vals}

    def _tunnel_iface(self, site: str, peer_wan_ip: str | None) -> tuple[str | None, int | None]:
        s = self.samples.get(site)
        if not s:
            return None, None
        for ifname, eps in s["endpoints"].items():
            if ifname.startswith("wgsts") and len(eps) == 1 and peer_wan_ip and eps[0][1] == peer_wan_ip:
                peers = s['handshakes'].get(ifname, [])
                if len(peers) > 1 or (peers and peers[0][0] != eps[0][0]):
                    continue
                hs = peers[0][1] if peers else None
                return ifname, hs
        return None, None

    async def _probes(self, buf: MetricBuffer, now) -> dict:
        st = self.ctx.state
        targets = {}
        for t in st.tunnels.values():
            if t.get("local_site") and st.sites.get(t["local_site"], None) and \
                    t["local_site"] == st.probe_origin(settings.probe_local_site):
                for m in t["members"]:
                    if m["site"] == t["remote_site"]:
                        for ip in (m.get("tunnel_ip"), m.get("router_ip")):
                            if ip:
                                targets[ip] = t["local_site"]
        if not targets:
            return {}

        async def one(ip):
            try:
                h = await asyncio.wait_for(async_ping(ip, count=settings.probe_count, interval=0.2, timeout=1, privileged=False),
                                           timeout=settings.probe_count * 0.2 + 3)
                return ip, h
            except Exception as exc:  # noqa: BLE001
                log.debug("probe %s failed: %s", ip, exc)
                return ip, None

        out = {}
        for ip, h in await asyncio.gather(*(one(ip) for ip in targets)):
            if h is None:
                continue
            site = targets[ip]
            eid = f"probe:{ip}"
            loss = h.packet_loss * 100
            st.window_add(eid + ":sent", h.packets_sent)
            st.window_add(eid + ":recv", h.packets_received)
            sent1, recv1 = sum(st.window(eid + ":sent", 60)), sum(st.window(eid + ":recv", 60))
            sent5, recv5 = sum(st.window(eid + ":sent", 300)), sum(st.window(eid + ":recv", 300))
            res = {"rtt_ms": h.avg_rtt if h.is_alive else None, "rtt_min_ms": h.min_rtt if h.is_alive else None,
                   "rtt_max_ms": h.max_rtt if h.is_alive else None, "jitter_ms": h.jitter if h.packets_received > 1 else None,
                   "loss_pct": loss, "loss_pct_1m": (1 - recv1 / sent1) * 100 if sent1 else None,
                   "loss_pct_5m": (1 - recv5 / sent5) * 100 if sent5 else None, "sent": h.packets_sent}
            for m, v in res.items():
                if m != "sent":
                    buf.add(now, site, "probe", eid, m, v)
            out[ip] = res
        return out

    async def _tunnel_rollup(self, tid: str, t: dict, buf: MetricBuffer, now, probes: dict) -> None:
        if len(t.get("members", [])) != 2:
            return
        st = self.ctx.state
        eid = f"tunnel:{tid}"
        local, remote = t.get("local_site"), t.get("remote_site")
        members = {m["site"]: m for m in t.get("members", [])}
        live = {"ts": time.time(), "interval_s": settings.live_poll_interval}
        # traffic from the local gateway's WireGuard interface (rx = remote -> local)
        for side, peer in ((local, remote), (remote, local)):
            if not side:
                continue
            ifname, hs = self._tunnel_iface(side, (members.get(peer) or {}).get("wan_ip"))
            sample = self.samples.get(side)
            rate = self.samples.get(f"{side}:{ifname}:rate") if ifname else None
            prefix = "" if side == local else "remote_"
            if ifname:
                live[prefix + "ifname"] = ifname
            if hs and sample and time.time() - sample['ts'] < settings.live_poll_interval * 3:
                age = max(sample["gw_ts"] - hs, 0)
                live[prefix + "handshake_age_s"] = age
                buf.add(now, local, "tunnel", eid, prefix + "handshake_age_s", age)
                if side == local:
                    st.put_current(local, "tunnel", eid, t.get("label"), "handshake_age_s", age)
            if rate and time.time() - rate["ts"] < settings.live_poll_interval * 3:
                live[prefix + "rx_bps"], live[prefix + "tx_bps"] = rate["rx_bps"], rate["tx_bps"]
                live[prefix + "err_per_min"] = rate["err_per_min"]
                buf.add(now, local, "tunnel", eid, prefix + "rx_bps", rate["rx_bps"])
                buf.add(now, local, "tunnel", eid, prefix + "tx_bps", rate["tx_bps"])
                if side == local:
                    buf.add(now, local, "tunnel", eid, "total_bps", rate["rx_bps"] + rate["tx_bps"])
                    buf.add(now, local, "tunnel", eid, "err_per_min", rate["err_per_min"])
                    st.put_current(local, "tunnel", eid, t.get("label"), "rx_bps", rate["rx_bps"])
                    st.put_current(local, "tunnel", eid, t.get("label"), "tx_bps", rate["tx_bps"])
            reset_at = self.samples.get(f"{side}:{ifname}:reset") if ifname else None
            if reset_at and reset_at > (t.get("_last_reset_seen") or 0):
                t["_last_reset_seen"] = reset_at
                st.window_add(eid + ":reconnects", 1)
                await self.ctx.event(side, "TUNNEL_IFACE_RESET", f"WireGuard interface {ifname} counters reset on {st.sites[side].description} gateway (tunnel re-created)",
                                     "medium", metadata={"tunnel": tid})
        if "rx_bps" not in live and "remote_rx_bps" in live:
            # local gateway unavailable: mirror the remote gateway's view (its rx = traffic towards it = our tx)
            live["rx_bps"], live["tx_bps"], live["source_side"] = live["remote_tx_bps"], live["remote_rx_bps"], "remote"
            live["err_per_min"] = live.get("remote_err_per_min")
            for metric, value in (("rx_bps", live["rx_bps"]), ("tx_bps", live["tx_bps"]),
                                  ("total_bps", live["rx_bps"] + live["tx_bps"]), ("err_per_min", live["err_per_min"])):
                buf.add(now, local, "tunnel", eid, metric, value)
                st.put_current(local, "tunnel", eid, t.get("label"), metric, value)
        elif "rx_bps" in live:
            live["source_side"] = "local"
        if live.get('source_side'):
            live['source_gateway'] = remote if live['source_side'] == 'remote' else local
            buf.add(now, local, 'tunnel', eid, 'source_remote', 1 if live['source_side'] == 'remote' else 0)
        # latency / loss / jitter from ICMP probes through the tunnel
        remote_m = members.get(remote) or {}
        p = probes.get(remote_m.get("tunnel_ip")) or probes.get(remote_m.get("router_ip"))
        if p:
            live["probe_target"] = remote_m.get("tunnel_ip") if probes.get(remote_m.get("tunnel_ip")) else remote_m.get("router_ip")
            for m in ("rtt_ms", "jitter_ms", "loss_pct", "loss_pct_1m", "loss_pct_5m"):
                if p.get(m) is not None:
                    key = "latency_ms" if m == "rtt_ms" else m
                    live[key] = p[m]
                    buf.add(now, local, "tunnel", eid, key, p[m])
                    if key in ("latency_ms", "jitter_ms", "loss_pct_1m", "loss_pct_5m"):
                        st.put_current(local, "tunnel", eid, t.get("label"), key, p[m])
            if p.get("jitter_ms") is not None:
                st.window_add(eid + ":jitter", p["jitter_ms"])
        config_stale = time.time() - (t.get('ts') or 0) > settings.poll_interval * 3 + 5
        live["connected"] = None if config_stale else t.get("connected")
        live["controller_state"] = "UNKNOWN" if live["connected"] is None else "CONNECTED" if live["connected"] else "DOWN"
        live["probe_state"] = "DISABLED" if not settings.probe_enabled else "REACHABLE" if live.get("latency_ms") is not None else "NO_REPLY" if live.get("loss_pct_1m") is not None else "UNAVAILABLE"
        ssh_error = next((link.last_error for site, link in getattr(self, "links", {}).items() if site in members and link.last_error), None)
        live["measurements"] = {}
        for metric in ("rx_bps", "tx_bps", "latency_ms", "loss_pct_1m", "jitter_ms", "handshake_age_s", "err_per_min"):
            ssh = metric in {"rx_bps", "tx_bps", "handshake_age_s", "err_per_min"}
            enabled = settings.gateway_ssh_enabled if ssh else settings.probe_enabled
            available = live.get(metric) is not None
            state = 'AVAILABLE' if available else 'DISABLED' if not enabled else 'UNAVAILABLE'
            reason = 'Available' if available else 'Collection disabled' if not enabled else ssh_error if ssh and ssh_error else 'Interface cannot be attributed to this peer' if ssh else 'No ICMP replies' if live['probe_state'] == 'NO_REPLY' else 'Probe origin or target unavailable'
            samples = [self.samples.get(site) for site in members]
            if ssh and enabled and not available and not ssh_error:
                if any(sample and time.time() - sample['ts'] >= settings.live_poll_interval * 3 for sample in samples):
                    state, reason = 'STALE', 'Gateway counters stale'
                elif live.get('ifname') or live.get('remote_ifname'):
                    state, reason = 'WARMING_UP', 'Waiting for two interface counter samples'
            live["measurements"][metric] = {"state": state, "reason": reason,
                                            "source": live.get('source_gateway') or 'Gateway SSH' if ssh else "collector ICMP", "updated_at": live["ts"] if available else None}
        live["coverage"] = {"measured": sum(item["state"] == "AVAILABLE" for item in live["measurements"].values()), "total": len(live["measurements"])}
        live["reconnects_1h"] = len(st.window(eid + ":reconnects", 3600))
        h = {"health_score": None, "status": "UNKNOWN", "reasons": [{"text": "Controller connectivity status is stale", "impact": 0}]} if config_stale else tunnel_health(t, live, st.window(eid + ":latency_base", 3600))
        if h['status'] == 'CONNECTED' and not live['coverage']['measured']:
            h.update(health_score=None, status='UNKNOWN', reasons=[{'text': 'Controller connected; no additional health measurements available', 'impact': 0}])
        if live.get("latency_ms") is not None:
            st.window_add(eid + ":latency_base", live["latency_ms"])
        live.update(h)
        h["coverage"] = live["coverage"]
        buf.add(now, local, "tunnel", eid, "health_score", h["health_score"])
        st.put_current(local, "tunnel", eid, t.get("label"), "health_score", h["health_score"])
        t["live"] = live
        st.health[f"tunnel:{tid}"] = h
        await kv_set(self.ctx.pool, f"tunnel_live.{tid}", live)


def tunnel_health(t: dict, live: dict, latency_hist: list[float]) -> dict:
    """SITE-TO-SITE HEALTH (0-100) from measured data only. Reasons explain every deduction."""
    reasons = []
    score = 100.0
    if t.get("connected") is False:
        return {"health_score": 0, "status": "DOWN", "reasons": [{"text": "Controller reports the tunnel as disconnected", "impact": 100}]}
    if t.get("paused"):
        return {"health_score": 0, "status": "DOWN", "reasons": [{"text": "SD-WAN configuration is paused", "impact": 100}]}
    loss = live.get("loss_pct_1m")
    have_data = False

    def deduct(points, text):
        nonlocal score
        score -= points
        reasons.append({"text": text, "impact": round(points)})

    if loss is not None:
        have_data = True
        if loss >= 10:
            deduct(40, f"Packet loss {loss:.1f}% (1 min)")
        elif loss >= 5:
            deduct(25, f"Packet loss {loss:.1f}% (1 min)")
        elif loss >= 1:
            deduct(10, f"Packet loss {loss:.1f}% (1 min)")
    lat = live.get("latency_ms")
    if lat is not None:
        have_data = True
        base = statistics.median(latency_hist) if len(latency_hist) >= 60 else None
        if lat >= 200:
            deduct(25, f"Latency {lat:.0f} ms")
        elif lat >= 120:
            deduct(15, f"Latency {lat:.0f} ms")
        elif base and lat > base * 1.6 and lat - base > 15:
            deduct(8, f"Latency {lat:.0f} ms (1 h median {base:.0f} ms)")
    jit = live.get("jitter_ms")
    if jit is not None:
        if jit >= 50:
            deduct(15, f"Jitter {jit:.0f} ms")
        elif jit >= 20:
            deduct(7, f"Jitter {jit:.0f} ms")
    hs = live.get("handshake_age_s")
    if hs is not None:
        have_data = True
        if hs > 600:
            deduct(30, f"Last WireGuard handshake {int(hs)} s ago")
        elif hs > 300:
            deduct(10, f"Last WireGuard handshake {int(hs)} s ago")
    rc = live.get("reconnects_1h") or 0
    if rc:
        deduct(min(rc * 5, 20), f"{rc} tunnel reconnect(s) in the last hour")
    if live.get("err_per_min"):
        deduct(5, f"Interface errors {live['err_per_min']:.0f}/min")
    if t.get("connected") is None and not have_data:
        return {"health_score": None, "status": "UNKNOWN", "reasons": [{"text": "No status or measurements available", "impact": 0}]}
    score = max(0, round(score))
    status = "CONNECTED" if score >= 80 else "DEGRADED"
    return {"health_score": score, "status": status, "reasons": reasons}

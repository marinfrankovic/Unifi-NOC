import { PageTitle } from "../components/ui";
import { Fragment, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api, Tunnel } from "../api";
import { fmtBps, fmtDuration, fmtMs, fmtNum, fmtTime } from "../format";
import { cssVar, EChart, escapeHtml, TimeSeries } from "../components/charts";
import { Card, ErrorBox, FreshBadge, HealthRing, Kpi, Loading, Pill, RangePicker, Sev, Table, tone, useRange } from "../components/ui";
import { useTheme } from "../theme";
import { Link, useSearchParams } from "react-router-dom";

export default function S2S() {
  const [range, setRange] = useRange("6h");
  const [params, setParams] = useSearchParams();
  const selected = params.get('tunnel') || '';
  const q = useQuery({ queryKey: ["s2s"], queryFn: () => api<Tunnel[]>("/s2s"), refetchInterval: 10000 });
  if (q.isError) return <ErrorBox error={q.error} />;
  if (!q.data) return <Loading />;
  if (!q.data.length)
    return (
      <Card title="Site-to-site">
        <div className="empty">No visible site-to-site connections. Unsupported or unavailable discovery does not confirm that no connection exists.</div>
      </Card>
    );
  return (
    <>
      <div className="page-head">
        <div><PageTitle>Site-to-Site / SD-WAN</PageTitle><div className="sub">Inter-site tunnel status, health, live traffic and history</div></div>
        <div className="right"><select aria-label="Tunnel" value={selected} onChange={event => { const next = new URLSearchParams(params); if (event.target.value) next.set('tunnel', event.target.value); else next.delete('tunnel'); setParams(next); }}><option value="">All tunnels</option>{q.data.map(tunnel => <option key={tunnel.id} value={tunnel.id}>{tunnel.label}</option>)}</select><RangePicker value={range} onChange={setRange} /></div>
      </div>
      {selected && !q.data.some(tunnel => tunnel.id === selected) && <div className="empty">Selected tunnel is unavailable or outside your site access.</div>}
      {q.data.filter(tunnel => !selected || tunnel.id === selected).map((t) => t.members.length === 2 ? <TunnelView key={t.id} t={t} range={range} /> :
        <Card key={t.id} title={t.label}><div className="col">
          <Pill status={t.config_freshness.state !== "LIVE" || t.connected === null ? "UNKNOWN" : t.connected ? "CONNECTED" : "DOWN"} />
          <span>{t.technology}</span><span>{t.members.map(member => member.site_desc).join(' · ')}</span>
          <div className="na">Pair-specific traffic is unavailable: group interface counters cannot be attributed to individual site pairs.</div>
        </div></Card>)}
    </>
  );
}

function short(t: Tunnel, site: string) {
  return t.members.find((m) => m.site === site)?.site_desc || site;
}

function TunnelView({ t, range }: { t: Tunnel; range: any }) {
  const live = t.live || {};
  const stale = t.freshness.state !== "LIVE";
  const status = stale ? "UNKNOWN" : live.status || "UNKNOWN";
  const L = short(t, t.local_site), R = short(t, t.remote_site);
  const eid = `tunnel:${t.id}`;
  const now = Date.now() / 1000;
  return (
    <>
      <div className="grid tunnel-summary-grid">
        <Card title="Site-to-site health" className={tone(status) === "crit" ? "alert-crit" : tone(status) === "warn" ? "alert-warn" : ""}>
          <div className="col" style={{ alignItems: "center" }}>
            <div className="small muted" style={{ letterSpacing: "0.1em" }}>{t.members.map((m) => m.site_desc.toUpperCase()).join("  ↔  ")}</div>
            <HealthRing score={stale ? null : live.health_score} size={130} />
            <span className={`statusword ${tone(status)}`}>{status}</span>
            {live.coverage && <span className="small muted">{live.coverage.measured}/{live.coverage.total} measurements available</span>}
            <div className="row"><Pill status={stale ? 'UNKNOWN' : live.controller_state}>Controller: {stale ? 'UNKNOWN' : live.controller_state || 'UNKNOWN'}</Pill><Pill status={stale ? 'UNKNOWN' : live.probe_state}>Probe: {stale ? 'UNKNOWN' : live.probe_state || 'UNKNOWN'}</Pill></div>
            {stale && <span className="stale">DATA STALE · last update {fmtTime(t.freshness.last_update, true)}</span>}
          </div>
          <div className="sep" />
          {!stale && (live.reasons?.length ? (
            <ul className="reasons">{live.reasons.map((r: any, i: number) => <li key={i}><span className="dot warn" />{r.text}<span className="imp">−{r.impact}</span></li>)}</ul>
          ) : <div className="small muted">{status === 'CONNECTED' ? 'All measured checks within thresholds.' : 'No additional health reasons reported.'}</div>)}
          <div className="sep" />
          <div className="kv">
            <span>Controller status</span><span className="controller-sites">{Object.entries(t.status).map(([s, v]) => <Pill key={s} status={t.config_freshness.state !== 'LIVE' || v.connected == null ? 'UNKNOWN' : v.connected ? "CONNECTED" : "DOWN"}>{short(t, s)} {t.config_freshness.state !== 'LIVE' || v.connected == null ? 'unknown' : v.connected ? "connected" : "down"}</Pill>)}</span>
            <span>Latency</span><span>{fmtNum(live.latency_ms, 1, " ms")}</span>
            <span>Jitter</span><span>{fmtNum(live.jitter_ms, 1, " ms")}</span>
            <span>Packet loss 1 m / 5 m</span><span>{fmtNum(live.loss_pct_1m, 1, "%")} / {fmtNum(live.loss_pct_5m, 1, "%")}</span>
            <span>WireGuard handshake</span><span>{live.handshake_age_s !== undefined ? `${live.handshake_age_s} s ago (${L})` : "—"}{live.remote_handshake_age_s !== undefined ? ` · ${live.remote_handshake_age_s} s ago (${R})` : ""}</span>
            <span>Reconnects (1 h)</span><span>{live.reconnects_1h ?? "—"}</span>
            <span>Interface errors</span><span>{fmtNum(live.err_per_min, 0, "/min")}</span>
          </div>
        </Card>
        <LiveTraffic t={t} L={L} R={R} />
      </div>

      <Card title="Connection details">
        <div className="kv" style={{ marginBottom: 12 }}>
          <span>Technology</span><span>{t.technology}</span>
          <span>Configuration</span><span>{t.name} {t.paused ? <Pill t="warn">paused</Pill> : null}</span>
          <span>Tunnel subnet</span><span className="mono">{t.tunnel_subnet}</span>
          <span>Interface</span><span className="mono">{live.ifname || "—"} ({L}) · {live.remote_ifname || "—"} ({R})</span>
          <span>Probe target</span><span className="mono">{live.probe_target || "—"} (collector ICMP; configured origin: {L})</span>
        </div>
        <Table rows={t.members} rowKey={(m) => m.site}
          cols={[
            { key: "site_desc", label: "Site" },
            { key: "wan_ip", label: "WAN endpoint", render: (m) => <span className="mono">{m.wan_ip}:{m.port}</span> },
            { key: "tunnel_ip", label: "Tunnel IP", render: (m) => <span className="mono">{m.tunnel_ip}</span> },
            { key: "router_ip", label: "Gateway LAN", render: (m) => <span className="mono">{m.router_ip}</span> },
            { key: "subnets", label: "Advertised subnets", render: (m) => <span className="mono">{(m.subnets || []).join(", ")}</span> },
            { key: "assoc", label: "Tunnel uptime (controller)", render: (m) => { const s = t.status[m.site]; return s?.association_time ? `${fmtDuration(now - s.association_time)} (since ${fmtTime(s.association_time * 1000, true)})` : "—"; } },
            { key: "hostkey", label: "Gateway SSH host key (pinned)", render: (m) => <span className="mono small">{t.gateway_hostkeys?.[m.site]?.fingerprint || "—"}</span> },
          ]} />
      </Card>

      <div className="grid g2">
        <Card title="Availability">
          <TimeSeries range={range} height={200} events={{ types: "TUNNEL", min_severity: "low" }}
            series={[{ entity: eid, metric: "connected", label: "Connected (controller)", step: true, color: "#22c55e", area: true }]}
            axes={[{ fmt: (v) => (v >= 0.5 ? "up" : "down"), min: 0, max: 1 }]} />
        </Card>
        <Card title="Health score">
          <TimeSeries range={range} height={200} events={{ types: "TUNNEL", min_severity: "low" }}
            series={[{ entity: eid, metric: "health_score", label: "Site-to-site health" }]} axes={[{ fmt: (v) => v.toFixed(0), min: 0, max: 100 }]} />
        </Card>
      </div>
      <div className="grid g2">
        <Card title="Latency & jitter">
          <TimeSeries range={range} events={{ types: "TUNNEL", min_severity: "low" }}
            series={[{ entity: eid, metric: "latency_ms", label: "Latency" }, { entity: eid, metric: "jitter_ms", label: "Jitter", color: "#f59e0b" }]}
            axes={[{ fmt: fmtMs }]} />
        </Card>
        <Card title="Packet loss">
          <TimeSeries range={range} events={{ types: "TUNNEL", min_severity: "low" }}
            series={[{ entity: eid, metric: "loss_pct_1m", label: "Loss (1 min window)", color: "#ef4444", area: true }, { entity: eid, metric: "loss_pct_5m", label: "Loss (5 min window)", color: "#fb923c" }]}
            axes={[{ fmt: (v) => `${v.toFixed(1)}%`, min: 0 }]} />
        </Card>
      </div>
      <div className="grid g2">
        <Card title="Traffic">
          <TimeSeries range={range} events={{ types: "TUNNEL", min_severity: "low" }}
            series={[{ entity: eid, metric: "rx_bps", label: `${R} → ${L} (RX)`, area: true }, { entity: eid, metric: "tx_bps", label: `${L} → ${R} (TX)`, area: true },
              { entity: eid, metric: "total_bps", label: "Total", color: "#94a3b8" }]}
            axes={[{ fmt: (v) => fmtBps(v, 0) }]} />
        </Card>
        <Card title="Reliability">
          <TimeSeries range={range} events={{ types: "TUNNEL", min_severity: "low" }}
            series={[{ entity: eid, metric: "reconnects_1h", label: "Reconnects (1 h)", color: "#ef4444", step: true }, { entity: eid, metric: "err_per_min", label: "Interface errors/min", color: "#f59e0b" },
              { entity: eid, metric: "handshake_age_s", label: "Handshake age (s)", axis: 1, color: "#a78bfa" }]}
            axes={[{ fmt: (v) => v.toFixed(0), min: 0, minInterval: 1 }, { fmt: (v) => `${v.toFixed(0)} s`, min: 0 }]} />
        </Card>
      </div>

      <div className="grid g2">
        <Card title="Tunnel events">
          <Table rows={t.events || []} rowKey={(e) => e.id} maxHeight={360} empty="No tunnel events recorded yet"
            cols={[{ key: "ts", label: "Time", render: (e) => fmtTime(e.ts, true) }, { key: "severity", label: "Sev", render: (e) => <Sev s={e.severity} /> },
              { key: "event_type", label: "Type", render: (e) => <span className="tag">{e.event_type}</span> }, { key: "message", label: "Message" }]} />
        </Card>
        <Card title="Tunnel alerts">
          <Table rows={t.alerts || []} rowKey={(a) => String(a.id)} maxHeight={360} empty="No alerts for this tunnel"
            cols={[{ key: "started_at", label: "Started", render: (a) => fmtTime(a.started_at, true) }, { key: "severity", label: "Sev", render: (a) => <Sev s={a.severity} /> },
              { key: "state", label: "State", render: (a) => <Pill status={a.state === "firing" ? "FIRING" : "RESOLVED"}>{a.state}</Pill> }, { key: "message", label: "Message" }]} />
        </Card>
      </div>

      <Card title="Data sources for this view">
        {live.measurements && <Table rows={Object.entries(live.measurements).map(([metric, detail]) => ({ metric, ...(detail as any) }))} rowKey={row => row.metric} cols={[{ key: 'metric', label: 'Measurement' }, { key: 'state', label: 'State' }, { key: 'reason', label: 'Reason' }, { key: 'source', label: 'Source' }]} />}
        <div className="row"><Link to="/connection">Connection settings</Link><Link to="/diagnostics">Diagnostics</Link></div>
        <div className="kv">
          {Object.entries(t.capabilities || {}).map(([k, v]) => (<Fragment key={k}><span>{k.replace(/_/g, " ")}</span><span>{v}</span></Fragment>))}
          <span>Not available from UniFi</span><span>Per-tunnel throughput, latency and loss are not exposed by the controller API; the values above are measured directly as listed.</span>
        </div>
      </Card>
    </>
  );
}

function LiveTraffic({ t, L, R }: { t: Tunnel; L: string; R: string }) {
  const q = useQuery({ queryKey: ["s2s-live", t.id], queryFn: () => api<any>(`/s2s/${t.id}/live?seconds=300`), refetchInterval: 5000 });
  const { theme } = useTheme();
  const live = q.data?.live || t.live || {};
  const f = q.data?.freshness || t.freshness;
  const stale = f.state !== "LIVE";
  const option = useMemo(() => {
    const s = q.data?.series || {};
    const text = cssVar("--muted"), grid = cssVar("--grid");
    const nowMs = Date.now();
    return {
      animation: false, grid: { left: 8, right: 16, top: 30, bottom: 8, containLabel: true },
      legend: { type: 'scroll', top: 0, left: 0, right: 0, textStyle: { color: text }, pageTextStyle: { color: text } },
      tooltip: { trigger: "axis", formatter: (ps: any[]) => fmtTime(ps[0].axisValue) + "<br/>" + ps.map((p) => `${p.marker}${escapeHtml(p.seriesName)}: <b>${fmtBps(p.value[1])}</b>`).join("<br/>") },
      xAxis: { type: "time", min: nowMs - 300000, max: nowMs, axisLabel: { color: text }, splitLine: { show: false } },
      yAxis: { type: "value", axisLabel: { color: text, formatter: (v: number) => fmtBps(v, 0) }, splitLine: { lineStyle: { color: grid } } },
      series: [
        { name: `${R} → ${L} (RX)`, type: "line", showSymbol: false, data: s.rx_bps || [], areaStyle: { opacity: 0.15 }, lineStyle: { width: 1.8 }, color: "#38bdf8" },
        { name: `${L} → ${R} (TX)`, type: "line", showSymbol: false, data: s.tx_bps || [], areaStyle: { opacity: 0.15 }, lineStyle: { width: 1.8 }, color: "#a78bfa" },
      ],
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.data, theme]);
  const avail = live.rx_bps != null && live.tx_bps != null;
  return (
    <Card title="Live inter-site traffic" right={<FreshBadge f={f} interval={q.data?.interval_s || live.interval_s} />} className={stale ? "alert-warn" : ""}>
      {stale && <div className="banner warn" style={{ marginBottom: 10 }}>DATA STALE <span className="sub">Last update {fmtTime(f.last_update, true)} — values below are not current.</span></div>}
      <div className="kpis" style={{ marginBottom: 8, opacity: stale ? 0.45 : 1 }}>
        <Kpi label={`RX  ${R} → ${L}`} value={avail ? fmtBps(live.rx_bps) : "—"} />
        <Kpi label={`TX  ${L} → ${R}`} value={avail ? fmtBps(live.tx_bps) : "—"} />
        <Kpi label="Total" value={avail ? fmtBps((live.rx_bps || 0) + (live.tx_bps || 0)) : "—"} />
        <Kpi label="Latency" value={fmtNum(live.latency_ms, 1)} unit="ms" />
        <Kpi label="Loss (1 m)" value={fmtNum(live.loss_pct_1m, 1)} unit="%" t={(live.loss_pct_1m ?? 0) >= 1 ? "warn" : undefined} />
        <Kpi label="Jitter" value={fmtNum(live.jitter_ms, 1)} unit="ms" />
      </div>
      {!avail && !stale && <div className="na" style={{ marginBottom: 8 }}>{live.measurements?.rx_bps?.reason || 'Traffic unavailable: no fresh, attributable interface counters'}</div>}
      <EChart option={option} height={250} />
      <div className="small muted">Refresh: {q.data?.interval_s || 5} seconds · source: {live.source_side === "remote" ? `${R} gateway (local gateway unreachable)` : `${L} gateway WireGuard interface ${live.ifname || ""}`} · rolling 5 minutes</div>
    </Card>
  );
}

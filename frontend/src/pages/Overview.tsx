import { PageTitle } from "../components/ui";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, Tunnel } from "../api";
import { ago, fmtBps, fmtDuration, fmtMs, fmtNum, fmtTime } from "../format";
import { TimeSeries } from "../components/charts";
import { Card, ErrorBox, FreshBadge, HealthRing, Loading, Pill, Sev, tone } from "../components/ui";

export default function Overview() {
  const q = useQuery({ queryKey: ["overview"], queryFn: () => api<any>("/overview"), refetchInterval: 10000 });
  const nav = useNavigate();
  if (q.isError) return <ErrorBox error={q.error} />;
  if (!q.data) return <Loading />;
  const { overall, sites, tunnels, alerts } = q.data;
  const overallLive = overall.freshness.state === "LIVE";
  const overallScore = overallLive ? overall.health_score : null;
  const t: Tunnel | undefined = tunnels[0];
  const left = sites[0], right = sites[1];
  const incidents = sites.flatMap((s: any) => s.incidents.map((i: any) => ({ ...i, site_name: s.description }))).sort((a: any, b: any) => (a.ts < b.ts ? 1 : -1)).slice(0, 10);
  const firing = alerts.filter((a: any) => a.kind !== "anomaly");
  const anomalies = alerts.filter((a: any) => a.kind === "anomaly");
  if (!sites.length) return <Card title="Monitoring"><div className="empty">No monitored sites are available to this account.</div></Card>;
  return (
    <>
      <div className="page-head">
        <div>
          <PageTitle>Network Overview</PageTitle>
          <div className="sub">Monitoring {sites.map((s: any) => s.description).join(" and ")} only</div>
        </div>
        <div className="right"><FreshBadge f={overall.freshness} /></div>
      </div>

      <div className="hero">
        <Card title="Network health">
          <div className="col" style={{ alignItems: "center" }}>
            <HealthRing score={overallScore} size={150} />
            <Pill status={overallLive ? overall.status : "UNKNOWN"}>{overallLive ? overall.status : "DATA STALE"}</Pill>
            {overall.coverage && <span className="small muted">Current data: {overall.coverage.measured}/{overall.coverage.total} sites</span>}
          </div>
          <div className="sep" />
          {overall.reasons?.length ? (
            <ul className="reasons">
              {overall.reasons.slice(0, 8).map((r: any, i: number) => (
                <li key={i}><span className={`dot ${r.severity === "critical" ? "crit" : r.severity === "info" ? "unk" : "warn"}`} />{r.text}{r.impact ? <span className="imp">−{r.impact}</span> : null}</li>
              ))}
            </ul>
          ) : overallScore !== null ? <div className="small muted">No deductions: every measured check is within its threshold.</div> : <div className="stale">{overallLive ? "NO DATA" : "DATA STALE"}</div>}
        </Card>

        <Card title={sites.length === 2 ? "Sites and inter-site link" : "Monitored sites"} pad>
          {sites.length !== 2 ? <div className="overview-sites">
            {sites.map((s: any) => <SiteCard key={s.name} s={s} onClick={() => nav(`/site/${s.slug}`)} />)}
          </div> :
          <div className="netmap">
            <div className="internet"><span className="dot unk" /> INTERNET</div>
            <div className="wires">
              <svg preserveAspectRatio="none" viewBox="0 0 100 28">
                <path d="M50 0 V8 H18 V28" fill="none" stroke={`var(--${left?.wan?.up ? "ok" : "crit"})`} strokeWidth="0.6" vectorEffect="non-scaling-stroke" />
                <path d="M50 0 V8 H82 V28" fill="none" stroke={`var(--${right?.wan?.up ? "ok" : "crit"})`} strokeWidth="0.6" vectorEffect="non-scaling-stroke" />
              </svg>
            </div>
            {left ? <SiteCard s={left} onClick={() => nav(`/site/${left.slug}`)} /> : <div />}
            <LinkPanel t={t} onClick={() => nav(t ? `/s2s?tunnel=${encodeURIComponent(t.id)}` : '/s2s')} />
            {right ? <SiteCard s={right} onClick={() => nav(`/site/${right.slug}`)} /> : <div />}
          </div>}
          {sites.length !== 2 && tunnels.length > 0 && <section className="overview-links" aria-label="Inter-site connections"><h3>Inter-site connections</h3>
            {tunnels.map((link: Tunnel) => <div key={link.id} className="overview-link"><strong>{link.label}</strong>
              <Pill status={link.config_freshness.state !== "LIVE" || link.connected === null ? "UNKNOWN" : link.connected ? "CONNECTED" : "DOWN"} />
              <button onClick={() => nav(`/s2s?tunnel=${encodeURIComponent(link.id)}`)}>Connection details</button></div>)}
          </section>}
        </Card>
      </div>

      <div className="grid g4">
        <Card title="WAN" right={<a onClick={() => nav("/wan")} style={{ cursor: "pointer" }}>details</a>}>
          <div className="col">
            {sites.map((s: any) => (
              <div key={s.name} className="row" style={{ justifyContent: "space-between" }}>
                <span>{s.description}</span>
                <Pill status={s.wan.freshness.state !== "LIVE" ? "UNKNOWN" : s.wan.up ? "UP" : "DOWN"}>{s.wan.freshness.state !== "LIVE" ? "stale" : s.wan.up ? "Up" : "Down"}</Pill>
                <span className="small mono">{fmtNum(s.wan.now?.latency_ms, 0, " ms")}</span>
                <span className="small mono">↓{fmtBps(s.wan.now?.live_rx_bps ?? s.wan.now?.rx_bps)} ↑{fmtBps(s.wan.now?.live_tx_bps ?? s.wan.now?.tx_bps)}</span>
              </div>
            ))}
          </div>
        </Card>
        <Card title="Devices">
          <div className="col">
            {sites.map((s: any) => (
              <div key={s.name} className="row" style={{ justifyContent: "space-between" }}>
                <span>{s.description}</span>
                {(["gateway", "switch", "ap"] as const).map((k) => {
                  const c = s.devices[k] || { online: 0, offline: 0 };
                  return <span key={k} className="small" title={k}><span className={`dot ${c.offline ? "crit" : "ok"}`} /> {c.online}/{c.online + c.offline} {k === "ap" ? "AP" : k === "gateway" ? "GW" : "SW"}</span>;
                })}
              </div>
            ))}
          </div>
        </Card>
        <Card title="Clients" right={<a onClick={() => nav("/clients")} style={{ cursor: "pointer" }}>details</a>}>
          <div className="col">
            {sites.map((s: any) => (
              <div key={s.name} className="row" style={{ justifyContent: "space-between" }}>
                <span>{s.description}</span>
                <b>{s.clients.online ?? 0}</b>
                <span className="small muted">{s.clients.wifi ?? 0} Wi-Fi · {s.clients.wired ?? 0} wired</span>
              </div>
            ))}
          </div>
        </Card>
        <Card title="Alerts" right={<a onClick={() => nav("/alerts")} style={{ cursor: "pointer" }}>all</a>} className={firing.some((a: any) => a.severity === "critical") ? "alert-crit" : firing.length ? "alert-warn" : ""}>
          <div className="col">
            <div className="row">
              <span className="bigstat" style={{ color: firing.length ? `var(--${firing.some((a: any) => a.severity === "critical") ? "crit" : "warn"})` : "var(--ok)" }}>{firing.length}</span>
              <span className="muted">active · {anomalies.length} anomal{anomalies.length === 1 ? "y" : "ies"}</span>
            </div>
            {firing.slice(0, 3).map((a: any) => <div key={a.id} className="small"><Sev s={a.severity} /> {a.message}</div>)}
          </div>
        </Card>
      </div>

      <div className="grid g2">
        {tunnels.filter((link: Tunnel) => link.members.length === 2).map((link: Tunnel) => <Card key={link.id} title={`${link.label} traffic (last 15 min)`}>
            <TimeSeries range="15m" refreshMs={5000} height={210} events={false} emptyText="No RX/TX samples in this window. Connection status does not include traffic counters."
              series={[{ entity: `tunnel:${link.id}`, metric: "rx_bps", label: `${siteShort(link, link.remote_site)} → ${siteShort(link, link.local_site)} (RX)`, area: true },
                { entity: `tunnel:${link.id}`, metric: "tx_bps", label: `${siteShort(link, link.local_site)} → ${siteShort(link, link.remote_site)} (TX)`, area: true }]}
              axes={[{ fmt: (v) => fmtBps(v, 0) }]} />
        </Card>)}
        <Card title="WAN latency (6 h)">
          <TimeSeries range="6h" height={210} events={{ types: "WAN", min_severity: "medium" }}
            series={sites.filter((s: any) => s.wan.entity_id).map((s: any) => ({ entity: s.wan.entity_id, metric: "latency_ms", label: s.description }))}
            axes={[{ fmt: fmtMs }]} />
        </Card>
      </div>

      <div className="grid g2">
        <Card title="Health score (24 h)">
          <TimeSeries range="24h" height={200} events={{ min_severity: "high" }}
            series={[...(q.data.include_global_history ? [{ entity: "site:_all", metric: "health_score", label: "Network", color: "#e5ecf6" }] : []),
              ...sites.map((s: any) => ({ entity: `site:${s.name}`, metric: "health_score", label: s.description }))]}
            axes={[{ fmt: (v) => v.toFixed(0), min: 0, max: 100 }]} />
        </Card>
        <Card title="Recent incidents (24 h)" right={<a onClick={() => nav("/events")} style={{ cursor: "pointer" }}>event viewer</a>}>
          {incidents.length ? incidents.map((i: any) => (
            <div key={i.id} className="incident">
              <span className="when">{fmtTime(i.ts)}</span>
              <Sev s={i.severity} />
              <span style={{ flex: 1 }}>{i.message}</span>
              <span className="small muted">{i.site_name}</span>
            </div>
          )) : <div className="empty">No incidents in the last 24 hours</div>}
        </Card>
      </div>
    </>
  );
}

function siteShort(t: Tunnel, site: string) {
  const m = t.members.find((x) => x.site === site);
  return m ? m.site_desc : site;
}

function SiteCard({ s, onClick }: { s: any; onClick: () => void }) {
  const h = s.health;
  const gw = s.gateway;
  const stale = s.health_freshness.state !== "LIVE";
  const dev = s.devices;
  const offline = ["switch", "ap"].reduce((n, k) => n + (dev[k]?.offline || 0), 0);
  const alerts = (s.alerts.critical || 0) + (s.alerts.warning || 0);
  return (
    <div className="sitecard" role="button" tabIndex={0} onClick={onClick} onKeyDown={event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onClick(); } }}>
      <div className="title">
        <HealthRing score={stale ? null : h.health_score} size={64} label="" />
        <div className="col" style={{ gap: 4 }}>
          <h2>{s.description}</h2>
          {stale ? <span className="stale">DATA STALE</span> : <Pill status={h.status}>{h.status}</Pill>}
          {h.coverage && <span className="small muted" title={(h.unavailable_checks || []).join(', ')}>Health checks: {h.coverage.measured}/{h.coverage.total}</span>}
        </div>
      </div>
      <div className="lines">
        <span>WAN</span><span><Pill status={s.wan.freshness.state !== "LIVE" ? "UNKNOWN" : s.wan.up ? "UP" : "DOWN"}>{s.wan.freshness.state !== "LIVE" ? "Stale" : s.wan.up ? "Up" : "Down"}</Pill> <span className="small mono">{s.wan.ip}</span> <span className="small muted">{s.wan.isp}</span></span>
        <span>Gateway</span><span>{gw ? <><span className={`dot ${tone(gw.online)}`} /> {gw.name} <span className="small muted">CPU {fmtNum(gw.now?.cpu_pct, 0, "%")} · mem {fmtNum(gw.now?.mem_pct, 0, "%")} · up {fmtDuration(gw.uptime)}</span></> : "—"}</span>
        <span>Switches</span><span>{(dev.switch?.online || 0)}/{(dev.switch?.online || 0) + (dev.switch?.offline || 0)} online</span>
        <span>APs</span><span>{(dev.ap?.online || 0)}/{(dev.ap?.online || 0) + (dev.ap?.offline || 0)} online</span>
        <span>Clients</span><span>{s.clients.online ?? 0}</span>
        <span>Alerts</span><span>{alerts ? <span className="sev critical">{alerts} active</span> : <span className="muted">none</span>}{offline ? <span className="sev warning"> · {offline} device(s) offline</span> : null}</span>
      </div>
      {h.reasons?.length > 0 && !stale && (
        <ul className="reasons small">
          {h.reasons.slice(0, 3).map((r: any, i: number) => <li key={i}><span className={`dot ${r.severity === "critical" ? "crit" : "warn"}`} />{r.text}</li>)}
        </ul>
      )}
    </div>
  );
}

function LinkPanel({ t, onClick }: { t?: Tunnel; onClick: () => void }) {
  if (!t) return <div className="link"><span className="na">No SD-WAN / VPN between sites</span></div>;
  const live = t.live || {};
  const stale = t.freshness.state !== "LIVE";
  const status = stale ? "UNKNOWN" : live.status || (t.connected ? "CONNECTED" : "DOWN");
  const tn = tone(status);
  return (
    <div className="link" onClick={onClick} title="Open site-to-site monitoring">
      <span className="small muted" style={{ letterSpacing: "0.1em" }}>SD-WAN · {t.name}</span>
      <div className={`bar ${tn === "ok" ? "ok live" : tn === "warn" ? "warn live" : tn === "crit" ? "crit" : ""}`} />
      <span className={`statusword ${tn}`} style={{ fontSize: 18 }}>{status}</span>
      {stale ? <span className="stale">DATA STALE · {ago(t.freshness.last_update)}</span> : (
        <div className="stats">
          <span>Health</span><span>{live.health_score ?? "—"}</span>
          <span>Latency</span><span>{fmtNum(live.latency_ms, 1, " ms")}</span>
          <span>Loss (1 m)</span><span>{fmtNum(live.loss_pct_1m, 1, "%")}</span>
          <span>Jitter</span><span>{fmtNum(live.jitter_ms, 1, " ms")}</span>
          <span>RX</span><span>{fmtBps(live.rx_bps)}</span>
          <span>TX</span><span>{fmtBps(live.tx_bps)}</span>
        </div>
      )}
    </div>
  );
}

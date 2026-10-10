import { PageTitle } from "../components/ui";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, EventRow, qs } from "../api";
import { BAND, fmtBps, fmtDuration, fmtMs, fmtNum, fmtTime } from "../format";
import { Heatmap, TimeSeries } from "../components/charts";
import { Card, ErrorBox, FreshBadge, HealthRing, Kpi, Loading, NotAvailable, Pill, RangePicker, Sev, Table, tone, useRange } from "../components/ui";

export default function SitePage() {
  const { slug = "" } = useParams();
  const nav = useNavigate();
  const [range, setRange] = useRange("6h");
  const q = useQuery({ queryKey: ["site", slug], queryFn: () => api<any>(`/sites/${slug}`), refetchInterval: 15000 });
  const ev = useQuery({ queryKey: ["site-events", slug], queryFn: () => api<{ items: EventRow[] }>(`/events${qs({ site: slug, range: "24h", limit: 25, min_severity: "low" })}`), refetchInterval: 30000 });
  const devs = useQuery({ queryKey: ["devices", slug, "ap"], queryFn: () => api<any[]>(`/devices${qs({ site: slug, type: "ap" })}`), staleTime: 60000 });
  if (q.isError) return <ErrorBox error={q.error} />;
  if (!q.data) return <Loading />;
  const s = q.data;
  const stale = s.health_freshness.state !== "LIVE";
  const wan = s.wan;
  const gw = s.gateway;
  const radioNames: Record<string, string> = {};
  (devs.data || []).forEach((d) => ["ng", "na", "6e"].forEach((r) => (radioNames[`radio:${d.mac}:${r}`] = `${d.name} ${BAND[r]}`)));
  return (
    <>
      <div className="page-head">
        <HealthRing score={stale ? null : s.health.health_score} size={86} />
        <div>
          <PageTitle>{s.description}</PageTitle>
          <div className="row" style={{ marginTop: 4 }}>
            {stale ? <span className="stale">DATA STALE</span> : <Pill status={s.health.status}>{s.health.status}</Pill>}
            <span className="sub" title={`Site ID: ${s.name}`}>{s.description}{s.timezone ? ` · ${s.timezone}` : ''}</span>
          </div>
        </div>
        <div className="right"><FreshBadge f={s.health_freshness} /><RangePicker value={range} onChange={setRange} /></div>
      </div>

      {s.health.coverage && <Card title="Health check coverage"><strong>{s.health.coverage.measured}/{s.health.coverage.total} checks measured</strong>{s.health.unavailable_checks?.length > 0 && <div className="small muted">Unavailable: {s.health.unavailable_checks.join(', ')}</div>}</Card>}

      {s.health.reasons?.length > 0 && (
        <Card title="Why the score is reduced">
          <ul className="reasons">
            {s.health.reasons.map((r: any, i: number) => (
              <li key={i}><span className={`dot ${r.severity === "critical" ? "crit" : r.severity === "info" ? "unk" : "warn"}`} />{r.text}{r.impact ? <span className="imp">−{r.impact}</span> : null}</li>
            ))}
          </ul>
        </Card>
      )}

      <div className="grid g4">
        <Card title="Internet / WAN" right={<FreshBadge f={wan.freshness} />}>
          <div className="kpis">
            <Kpi label="Status" value={<Pill status={wan.up ? "UP" : "DOWN"}>{wan.up ? "Up" : "Down"}</Pill>} hint={`${wan.isp || ""} ${wan.asn ? "AS" + wan.asn : ""}`} />
            <Kpi label="Latency" value={fmtNum(wan.now?.latency_ms, 0)} unit="ms" hint={`24 h avg ${fmtNum(wan.now?.latency_avg_24h, 0, " ms")}`} />
            <Kpi label="Download" value={fmtBps(wan.now?.live_rx_bps ?? wan.now?.rx_bps)} hint={wan.now?.live_rx_bps !== undefined ? "live 5 s" : "UniFi rate"} />
            <Kpi label="Upload" value={fmtBps(wan.now?.live_tx_bps ?? wan.now?.tx_bps)} />
            <Kpi label="Availability 24 h" value={fmtNum(wan.now?.availability_24h, 2)} unit="%" />
            <Kpi label="Public IP" value={<span className="mono small">{wan.ip || "—"}</span>} />
          </div>
        </Card>
        <Card title="Gateway" right={gw && <a onClick={() => nav(`/device/${gw.mac}`)} style={{ cursor: "pointer" }}>details</a>}>
          {gw ? (
            <div className="kpis">
              <Kpi label={gw.name} value={<Pill status={gw.online}>{gw.online ? "Online" : "Offline"}</Pill>} hint={`${gw.model} · ${gw.version}`} />
              <Kpi label="CPU" value={fmtNum(gw.now?.cpu_pct, 0)} unit="%" t={(gw.now?.cpu_pct ?? 0) >= 85 ? "warn" : undefined} />
              <Kpi label="Memory" value={fmtNum(gw.now?.mem_pct, 0)} unit="%" />
              <Kpi label="Temperature" value={gw.now?.temp_c !== undefined ? fmtNum(gw.now.temp_c, 0) : <NotAvailable />} unit={gw.now?.temp_c !== undefined ? "°C" : undefined} />
              <Kpi label="Uptime" value={fmtDuration(gw.uptime)} />
            </div>
          ) : <div className="empty">No gateway reported</div>}
        </Card>
        <Card title="Clients" right={<a onClick={() => nav(`/clients?site=${slug}`)} style={{ cursor: "pointer" }}>list</a>}>
          <div className="kpis">
            <Kpi label="Connected" value={s.clients.online ?? 0} />
            <Kpi label="Wi-Fi" value={s.clients.wifi ?? 0} />
            <Kpi label="Wired" value={s.clients.wired ?? 0} />
          </div>
        </Card>
        <Card title="Alerts & incidents" right={<a onClick={() => nav(`/alerts?site=${slug}`)} style={{ cursor: "pointer" }}>alerts</a>}>
          <div className="kpis">
            <Kpi label="Critical" value={s.alerts.critical || 0} t={s.alerts.critical ? "crit" : "ok"} />
            <Kpi label="Warning" value={s.alerts.warning || 0} t={s.alerts.warning ? "warn" : undefined} />
            <Kpi label="Anomalies" value={s.alerts.info || 0} />
          </div>
          {s.tunnels.map((t: any) => (
            <div key={t.id} className="row small" style={{ marginTop: 10, cursor: "pointer" }} onClick={() => nav("/s2s")}>
              <span className="muted">Site-to-site</span>
              <Pill status={t.freshness.state !== "LIVE" ? "UNKNOWN" : t.live?.status}>{t.freshness.state !== "LIVE" ? "stale" : t.live?.status}</Pill>
              <span className="mono">{fmtNum(t.live?.latency_ms, 0, " ms")}</span>
            </div>
          ))}
        </Card>
      </div>

      <Card title="Devices">
        <Table rows={s.device_list} rowKey={(r: any) => r.mac} onRow={(r: any) => nav(`/device/${r.mac}`)}
          cols={[
            { key: "name", label: "Name", render: (r: any) => <><span className={`dot ${tone(r.online)}`} /> {r.name}</> },
            { key: "device_type", label: "Type" },
            { key: "model", label: "Model" },
            { key: "state", label: "State", render: (r: any) => <Pill status={r.online}>{r.online ? "Online" : "Offline"}</Pill> },
            { key: "ip", label: "IP", render: (r: any) => <span className="mono">{r.ip}</span> },
            { key: "version", label: "Firmware" },
            { key: "cpu", label: "CPU", num: true, sort: (r: any) => r.now?.cpu_pct, render: (r: any) => fmtNum(r.now?.cpu_pct, 0, "%") },
            { key: "mem", label: "Memory", num: true, sort: (r: any) => r.now?.mem_pct, render: (r: any) => fmtNum(r.now?.mem_pct, 0, "%") },
            { key: "sta", label: "Clients", num: true, sort: (r: any) => r.now?.num_sta, render: (r: any) => fmtNum(r.now?.num_sta) },
            { key: "uptime", label: "Uptime", num: true, render: (r: any) => (r.online ? fmtDuration(r.uptime) : "—") },
            { key: "last_seen", label: "Last seen", render: (r: any) => fmtTime(r.last_seen) },
          ]} />
      </Card>

      {wan.entity_id && (
        <div className="grid g2">
          <Card title="WAN throughput">
            <TimeSeries range={range} events={{ site: slug, types: "WAN", min_severity: "low" }}
              series={[{ entity: wan.entity_id, metric: "rx_bps", label: "Download", area: true }, { entity: wan.entity_id, metric: "tx_bps", label: "Upload", area: true }]}
              axes={[{ fmt: (v) => fmtBps(v, 0) }]} />
          </Card>
          <Card title="WAN latency & packet loss">
            <TimeSeries range={range} events={{ site: slug, types: "WAN", min_severity: "low" }}
              series={[{ entity: wan.entity_id, metric: "latency_ms", label: "Latency (gateway)" }, { entity: wan.entity_id, metric: "loss_pct", label: "Packet loss (UniFi 5-min)", axis: 1, type: "bar", color: "#ef4444" }]}
              axes={[{ fmt: fmtMs }, { fmt: (v) => `${v.toFixed(1)}%`, min: 0 }]} />
          </Card>
        </div>
      )}
      <div className="grid g2">
        <Card title="Clients">
          <TimeSeries range={range} events={false}
            series={[{ entity: `site:${s.name}`, metric: "clients_total", label: "Total" }, { entity: `site:${s.name}`, metric: "clients_wifi", label: "Wi-Fi" },
              { entity: `site:${s.name}`, metric: "clients_wired", label: "Wired" }, { entity: `site:${s.name}`, metric: "clients_poor_signal", label: "Poor signal", color: "#f59e0b" }]}
            axes={[{ fmt: (v) => v.toFixed(0), min: 0 }]} />
        </Card>
        <Card title="Client connection churn (events per 15 min)">
          <TimeSeries range={range} events={false}
            series={[{ entity: `site:${s.name}`, metric: "disconnects_15m", label: "Disconnects", color: "#ef4444" }, { entity: `site:${s.name}`, metric: "roams_15m", label: "Roams" },
              { entity: `site:${s.name}`, metric: "connects_15m", label: "Connects", color: "#34d399" }]}
            axes={[{ fmt: (v) => v.toFixed(0), min: 0 }]} />
        </Card>
      </div>
      <div className="grid g2">
        <Card title="Wi-Fi channel utilization (hourly)">
          <Heatmap entityType="radio" metric="cu_total" site={slug} range={range === "15m" || range === "1h" || range === "6h" ? "24h" : range} names={radioNames} fmt={(v) => `${v.toFixed(0)}%`} />
        </Card>
        <Card title="Health score">
          <TimeSeries range={range} events={{ site: slug, min_severity: "high" }} series={[{ entity: `site:${s.name}`, metric: "health_score", label: "Health" }]} axes={[{ fmt: (v) => v.toFixed(0), min: 0, max: 100 }]} />
        </Card>
      </div>

      <Card title="Recent events (24 h)" right={<a onClick={() => nav(`/events?site=${slug}`)} style={{ cursor: "pointer" }}>all events</a>}>
        <Table rows={ev.data?.items || []} rowKey={(r) => r.id}
          cols={[
            { key: "ts", label: "Time", render: (r) => fmtTime(r.ts) },
            { key: "severity", label: "Severity", render: (r) => <Sev s={r.severity} /> },
            { key: "event_type", label: "Type", render: (r) => <span className="tag">{r.event_type}</span> },
            { key: "message", label: "Message" },
          ]} />
      </Card>
    </>
  );
}

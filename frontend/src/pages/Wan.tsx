import { PageTitle } from "../components/ui";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { api, qs } from "../api";
import { fmtBps, fmtMs, fmtNum, fmtTime } from "../format";
import { TimeSeries } from "../components/charts";
import { Card, ErrorBox, FreshBadge, Kpi, Loading, Pill, RangePicker, Sev, Table, useRange } from "../components/ui";

export default function Wan() {
  const [range, setRange] = useRange("24h");
  const [params] = useSearchParams();
  const site = params.get('site') || 'all';
  const q = useQuery({ queryKey: ["wan", range, site], queryFn: () => api<any[]>(`/wan${qs({ range, site: site === 'all' ? undefined : site })}`), refetchInterval: 15000 });
  if (q.isError) return <ErrorBox error={q.error} />;
  if (!q.data) return <Loading />;
  const live = range === "15m" || range === "1h" || range === "6h";
  return (
    <>
      <div className="page-head">
        <div><PageTitle>WAN / Internet</PageTitle><div className="sub">Gateway uplink status, latency, packet loss, throughput and outages</div></div>
        <div className="right"><RangePicker value={range} onChange={setRange} /></div>
      </div>
      {q.data.map((w) => (
        <div key={w.site} className="col" style={{ gap: 16 }}>
          <Card title={<h2>{w.site_name} — {w.name || "WAN"}</h2>} right={<FreshBadge f={w.freshness} />} className={w.up === false ? "alert-crit" : ""}>
            <div className="kpis">
              <Kpi label="Status" value={w.freshness.state !== "LIVE" ? <span className="stale">STALE</span> : <Pill status={w.up ? "UP" : "DOWN"}>{w.up ? "Up" : "Down"}</Pill>} hint={w.health_status ? `UniFi WAN: ${w.health_status}` : undefined} />
              <Kpi label="Public IP" value={<span className="mono" style={{ fontSize: 16 }}>{w.ip || "—"}</span>} hint={`${w.ifname || ""} · ${w.physical_speed ? w.physical_speed + " Mbps link" : ""}`} />
              <Kpi label="ISP" value={<span style={{ fontSize: 16 }}>{w.isp || "—"}</span>} hint={`${w.asn ? "AS" + w.asn : ""} ${w.city || ""}`} />
              <Kpi label="Latency" value={fmtNum(w.now?.latency_ms, 0)} unit="ms" hint={`24 h avg ${fmtNum(w.now?.latency_avg_24h, 0, " ms")}`} />
              <Kpi label="Packet loss" value={fmtNum(w.now?.loss_pct, 2)} unit="%" hint="UniFi 5-minute average" />
              <Kpi label="Download" value={fmtBps(w.now?.live_rx_bps ?? w.now?.rx_bps)} hint={w.now?.live_rx_bps !== undefined ? "live (5 s)" : undefined} />
              <Kpi label="Upload" value={fmtBps(w.now?.live_tx_bps ?? w.now?.tx_bps)} />
              <Kpi label="Utilization" value={fmtNum(w.now?.util_pct, 1)} unit="%" hint={w.max_speed ? `of ${w.max_speed} Mbps` : undefined} />
              <Kpi label="Availability 24 h" value={fmtNum(w.now?.availability_24h, 2)} unit="%" hint="gateway monitors" />
              <Kpi label={`Availability (${range})`} value={w.availability ? fmtNum(w.availability.availability_pct, 2) : "—"} unit={w.availability ? "%" : undefined}
                hint={w.availability ? `${w.availability.down_buckets} of ${w.availability.buckets} 5-min intervals down` : "no UniFi health history yet"} />
              <Kpi label="Interface errors" value={fmtNum(w.now?.err_per_min, 1)} unit="/min" hint={`drops ${fmtNum(w.now?.drop_per_min, 1)}/min`} />
            </div>
          </Card>
          {w.entity_id && (
            <>
              <div className="grid g2">
                <Card title="Throughput">
                  <TimeSeries range={range} events={{ site: w.slug, types: "WAN", min_severity: "low" }}
                    series={live ? [{ entity: w.entity_id, metric: "live_rx_bps", label: "Download (5 s)", area: true }, { entity: w.entity_id, metric: "live_tx_bps", label: "Upload (5 s)", area: true }]
                      : [{ entity: w.entity_id, metric: "rx_bps", label: "Download", area: true }, { entity: w.entity_id, metric: "tx_bps", label: "Upload", area: true }]}
                    axes={[{ fmt: (v) => fmtBps(v, 0) }]} />
                </Card>
                <Card title="Latency">
                  <TimeSeries range={range} events={{ site: w.slug, types: "WAN", min_severity: "low" }}
                    series={[{ entity: w.entity_id, metric: "latency_ms", label: "Gateway latency" }, { entity: w.entity_id, metric: "ua_latency_ms", label: "UniFi 5-min avg" },
                      { entity: w.entity_id, metric: "ua_latency_max_ms", label: "UniFi 5-min max", color: "#f59e0b" }]}
                    axes={[{ fmt: fmtMs }]} />
                </Card>
              </div>
              <div className="grid g3">
                <Card title="Packet loss">
                  <TimeSeries range={range} height={200} events={false} series={[{ entity: w.entity_id, metric: "loss_pct", label: "Loss (UniFi 5-min)", type: "bar", color: "#ef4444" }]}
                    axes={[{ fmt: (v) => `${v.toFixed(1)}%`, min: 0 }]} emptyText="No UniFi WAN loss samples in this window" />
                </Card>
                <Card title="Availability (1 = outage)">
                  <TimeSeries range={range} height={200} events={false} series={[{ entity: w.entity_id, metric: "downtime", label: "WAN downtime", type: "bar", color: "#ef4444" },
                    { entity: w.entity_id, metric: "up", label: "WAN up (gateway)", step: true, color: "#22c55e" }]}
                    axes={[{ fmt: (v) => v.toFixed(0), min: 0, max: 1 }]} />
                </Card>
                <Card title="Interface errors / drops">
                  <TimeSeries range={range} height={200} events={false} series={[{ entity: w.entity_id, metric: "err_per_min", label: "Errors/min", color: "#ef4444" }, { entity: w.entity_id, metric: "drop_per_min", label: "Drops/min", color: "#f59e0b" }]}
                    axes={[{ fmt: (v) => v.toFixed(1), min: 0 }]} />
                </Card>
              </div>
              <div className="grid g2">
                <Card title="Gateway WAN monitors (24 h averages reported by UniFi)">
                  <Table rows={w.monitors || []} rowKey={(r: any) => r.id || r.target}
                    cols={[{ key: "target", label: "Target" }, { key: "type", label: "Type" }, { key: "latency_average", label: "Latency", num: true, render: (r: any) => fmtNum(r.latency_average, 0, " ms") },
                      { key: "availability", label: "Availability", num: true, render: (r: any) => fmtNum(r.availability, 2, "%") }]} />
                  <div className="small muted" style={{ marginTop: 6 }}>Window: {w.time_period ? `${w.time_period / 3600} h` : "—"}. Speed test: {w.speedtest?.rundate ? `${fmtNum(w.speedtest.xput_download, 0)} / ${fmtNum(w.speedtest.xput_upload, 0)} Mbps` : "never run on this gateway (not available from UniFi)"}</div>
                </Card>
                <Card title="Outages and IP changes (30 days)">
                  <Table rows={w.outages || []} rowKey={(r: any) => r.ts + r.event_type} empty="No WAN outages recorded"
                    cols={[{ key: "ts", label: "Time", render: (r: any) => fmtTime(r.ts, true) }, { key: "event_type", label: "Event", render: (r: any) => <Sev s={r.event_type === "WAN_DOWN" ? "critical" : "info"} /> },
                      { key: "message", label: "Detail" }, { key: "dur", label: "Duration", render: (r: any) => (r.metadata?.outage_s ? `${r.metadata.outage_s} s` : "") }]} />
                </Card>
              </div>
            </>
          )}
        </div>
      ))}
    </>
  );
}

import { PageTitle } from "../components/ui";
import { useNavigate, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, qs } from "../api";
import { deviceMonitoringStatus, fmtBps, fmtDuration, fmtNum, fmtTime } from "../format";
import { TimeSeries } from "../components/charts";
import { Card, ErrorBox, Loading, NotAvailable, Pill, RangePicker, Seg, Sev, Table, tone, useRange } from "../components/ui";

const TITLES: Record<string, string> = { gateway: "Gateways", switch: "Switches", ap: "Access Points" };

export default function Devices() {
  const { type = "switch" } = useParams();
  const [sp, setSp] = useSearchParams();
  const site = sp.get("site") || "all";
  const [range, setRange] = useRange("6h");
  const nav = useNavigate();
  const sites = useQuery({ queryKey: ["sites"], queryFn: () => api<any[]>("/sites") });
  const q = useQuery({ queryKey: ["devices", site, type], queryFn: () => api<any[]>(`/devices${qs({ site: site === "all" ? undefined : site, type })}`), refetchInterval: 15000 });
  if (q.isError) return <ErrorBox error={q.error} />;
  const rows = (q.data || []).map((r) => ({ ...r, monitoring_status: deviceMonitoringStatus(r.online, r.freshness?.state || "NO_DATA", r.alert) }));
  const online = rows.filter((r) => r.online && r.freshness?.state === "LIVE").length;
  const stale = rows.filter((r) => r.monitoring_status === "UNKNOWN").length;
  return (
    <>
      <div className="page-head">
        <div><PageTitle>{TITLES[type] || type}</PageTitle><div className="sub">{online} of {rows.length} confirmed online{stale ? ` · ${stale} with stale or unavailable data` : ""}</div></div>
        <div className="right">
          <Seg value={site} onChange={(v) => { const x = new URLSearchParams(sp); x.set("site", v); setSp(x, { replace: true }); }}
            options={[{ v: "all", l: "All sites" }, ...(sites.data || []).map((s) => ({ v: s.slug, l: s.description }))]} />
          <RangePicker value={range} onChange={setRange} />
        </div>
      </div>
      <Card>
        {!q.data ? <Loading /> : (
          <Table rows={rows} rowKey={(r) => r.mac} onRow={(r) => nav(`/device/${r.mac}`)} initialSort={{ key: "site_name", dir: 1 }}
            cols={[
              { key: "name", label: "Name", render: (r) => <><span className={`dot ${tone(r.monitoring_status)}`} /> {r.name}</> },
              { key: "site_name", label: "Site" },
              { key: "monitoring_status", label: "Status", render: (r) => <Pill status={r.monitoring_status}>{r.monitoring_status === "UNKNOWN" ? "DATA STALE" : r.monitoring_status}</Pill> },
              { key: "alert", label: "Alert", render: (r) => (r.alert ? <Sev s={r.alert} /> : "") },
              { key: "model", label: "Model", render: (r) => `${r.model_name || ""} ${r.model ? `(${r.model})` : ""}` },
              { key: "ip", label: "IP", render: (r) => <span className="mono">{r.ip}</span> },
              { key: "version", label: "Firmware", render: (r) => <>{r.version}{r.upgradable ? <span className="tag warn" style={{ marginLeft: 6 }}>→ {r.upgrade_to || "update"}</span> : null}</> },
              { key: "cpu", label: "CPU", num: true, sort: (r) => r.now?.cpu_pct, render: (r) => (r.now?.cpu_pct !== undefined ? fmtNum(r.now.cpu_pct, 0, "%") : r.online ? <NotAvailable /> : "—") },
              { key: "mem", label: "Mem", num: true, sort: (r) => r.now?.mem_pct, render: (r) => fmtNum(r.now?.mem_pct, 0, "%") },
              { key: "temp", label: "Temp", num: true, sort: (r) => r.now?.temp_c, render: (r) => (r.now?.temp_c !== undefined ? fmtNum(r.now.temp_c, 0, " °C") : <span className="na">n/a</span>) },
              { key: "sta", label: "Clients", num: true, sort: (r) => r.now?.num_sta ?? r.num_sta, render: (r) => fmtNum(r.now?.num_sta ?? r.num_sta) },
              { key: "uptime", label: "Uptime", num: true, render: (r) => (r.online ? fmtDuration(r.uptime) : "—") },
              { key: "last_seen", label: "Last seen", render: (r) => fmtTime(r.last_seen) },
              { key: "last_poll", label: "Last poll", render: (r) => fmtTime(r.last_poll) },
            ]} />
        )}
      </Card>
      {rows.length > 0 && (
        <div className="grid g2">
          <Card title="CPU">
            <TimeSeries range={range} events={false} series={rows.map((r) => ({ entity: `dev:${r.mac}`, metric: "cpu_pct", label: r.name }))} axes={[{ fmt: (v) => `${v.toFixed(0)}%`, min: 0 }]} />
          </Card>
          <Card title="Memory">
            <TimeSeries range={range} events={false} series={rows.map((r) => ({ entity: `dev:${r.mac}`, metric: "mem_pct", label: r.name }))} axes={[{ fmt: (v) => `${v.toFixed(0)}%`, min: 0 }]} />
          </Card>
          {type !== "gateway" && (
            <Card title="Uplink traffic">
              <TimeSeries range={range} events={false} series={rows.flatMap((r) => [{ entity: `dev:${r.mac}`, metric: "uplink_rx_bps", label: `${r.name} RX` }])} axes={[{ fmt: (v) => fmtBps(v, 0) }]} />
            </Card>
          )}
          <Card title="Availability (1 = online)">
            <TimeSeries range={range} events={{ types: "DEVICE", min_severity: "low" }} series={rows.map((r) => ({ entity: `dev:${r.mac}`, metric: "online", label: r.name, step: true }))} axes={[{ fmt: (v) => v.toFixed(0), min: 0, max: 1 }]} />
          </Card>
        </div>
      )}
      {rows.length === 0 && q.data && <div className="empty">No {TITLES[type]?.toLowerCase()} on the monitored sites.</div>}
      <div className="small muted">Status dot: green healthy, amber warning alert, red critical alert, grey offline or unknown. Stale data is not counted as confirmed online.</div>
    </>
  );
}

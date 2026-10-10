import { PageTitle } from "../components/ui";
import { ReactNode, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { BAND, deviceMonitoringStatus, fmtBps, fmtBytes, fmtDuration, fmtMs, fmtNum, fmtTime, NA } from "../format";
import { TimeSeries } from "../components/charts";
import { PortPanel } from "../components/PortPanel";
import { Card, ErrorBox, FreshBadge, Kpi, Loading, NotAvailable, Pill, RangePicker, Sev, Table, tone, useRange } from "../components/ui";

export default function DeviceDetail() {
  const { mac = "" } = useParams();
  const nav = useNavigate();
  const [range, setRange] = useRange("6h");
  const [port, setPort] = useState<number | null>(null);
  const q = useQuery({ queryKey: ["device", mac], queryFn: () => api<any>(`/devices/${mac}`), refetchInterval: 15000 });
  if (q.isError) return <ErrorBox error={q.error} />;
  if (!q.data) return <Loading />;
  const d = q.data;
  const raw = d.raw || {};
  const now = d.now[`dev:${mac}`] || {};
  const caps = d.capabilities || {};
  const kind = d.device_type;
  const firing = d.alerts.filter((a: any) => a.state === "firing" && a.kind !== "anomaly");
  const severity = firing.some((a: any) => a.severity === "critical") ? "critical" : firing.some((a: any) => a.severity === "warning") ? "warning" : null;
  const status = deviceMonitoringStatus(d.online, d.freshness.state, severity);
  const ports: any[] = raw.port_table || [];
  const radios: any[] = raw.radio_table_stats || [];
  const radioCfg = Object.fromEntries((raw.radio_table || []).map((r: any) => [r.radio, r]));
  const dev = `dev:${mac}`;
  const evFilter = { device: mac, min_severity: "low" };
  const selPort = port ?? ports.find((p) => p.is_uplink)?.port_idx ?? ports[0]?.port_idx;
  return (
    <>
      <div className="page-head">
        <div>
          <PageTitle parent={{label: kind === 'gateway' ? 'Gateways' : kind === 'ap' ? 'Access Points' : 'Switches', to: `/devices/${kind === 'gateway' || kind === 'ap' ? kind : 'switch'}`}}>{d.name}</PageTitle>
          <div className="row" style={{ marginTop: 4 }}>
            <Pill status={status}>{status === "UNKNOWN" ? "DATA STALE" : status}</Pill>
            <span className="sub">{kind} · {d.model_name || ""} {d.model} · {d.site_name}</span>
            {d.alerts.some((a: any) => a.state === "firing") && <Sev s={d.alerts.find((a: any) => a.state === "firing").severity} />}
          </div>
        </div>
        <div className="right"><FreshBadge f={d.freshness} /><RangePicker value={range} onChange={setRange} /></div>
      </div>

      <div className="grid device-summary-grid">
        <Card title="Status">
          <div className="kpis">
            <Kpi label="Status" value={status === "UNKNOWN" ? "Unknown (data stale)" : status} t={tone(status)} hint={raw._state_label} />
            <Kpi label="Uptime" value={d.online ? fmtDuration(d.uptime) : "—"} />
            <Kpi label="CPU" value={caps.cpu ? fmtNum(now.cpu_pct, 0) : <NotAvailable />} unit={caps.cpu ? "%" : undefined} t={(now.cpu_pct ?? 0) >= 85 ? "warn" : undefined} />
            <Kpi label="Memory" value={caps.memory ? fmtNum(now.mem_pct, 0) : <NotAvailable />} unit={caps.memory ? "%" : undefined} />
            <Kpi label="Temperature" value={caps.temperature ? fmtNum(now.temp_c, 0) : <NotAvailable />} unit={caps.temperature ? "°C" : undefined} />
            <Kpi label="Load (1 m)" value={caps.load ? fmtNum(now.load1, 2) : <NotAvailable />} />
            <Kpi label="Clients" value={fmtNum(now.num_sta ?? raw.num_sta)} />
            {kind === "switch" && <Kpi label="PoE draw" value={caps.poe ? fmtNum(now.poe_w, 1) : <NotAvailable />} unit={caps.poe ? "W" : undefined}
              hint={raw.total_max_power ? `budget ${raw.total_max_power} W` : "PoE budget not reported"} />}
            {kind === "gateway" && <Kpi label="WAN" value={d.wan?.up ? "Up" : "Down"} t={d.wan?.up ? "ok" : "crit"} hint={d.wan?.ip} />}
          </div>
          <div className="sep" />
          <div className="kv">
            <span>IP</span><span className="mono">{d.ip}</span>
            <span>MAC</span><span className="mono">{d.mac}</span>
            <span>Firmware</span><span>{d.version}{d.upgradable ? <span className="tag warn" style={{ marginLeft: 6 }}>update available: {d.upgrade_to}</span> : null}</span>
            <span>Adoption</span><span>{d.adopted ? "adopted" : "not adopted"}</span>
            <span>Last seen</span><span>{fmtTime(d.last_seen, true)}</span>
            <span>Last successful poll</span><span>{fmtTime(d.last_poll, true)}</span>
            {raw.uplink?.uplink_mac && (<><span>Uplink</span><span><a onClick={() => nav(`/device/${raw.uplink.uplink_mac}`)} style={{ cursor: "pointer" }}>{raw.uplink.uplink_device_name || raw.uplink.uplink_mac}</a> port {raw.uplink.uplink_remote_port ?? "—"} · {raw.uplink.speed ? `${raw.uplink.speed} Mbps` : ""} {raw.uplink.full_duplex ? "full duplex" : ""}</span></>)}
          </div>
        </Card>
        <Card title="Metric availability (discovered)">
          <div className="kv small">
            {Object.entries(caps).map(([k, v]) => (
              <Frag key={k} a={k.replace(/_/g, " ")} b={v ? <span style={{ color: "var(--ok)" }}>available</span> : <span className="na">{NA}</span>} />
            ))}
          </div>
        </Card>
      </div>

      <div className="grid g2">
        <Card title="CPU & memory">
          <TimeSeries range={range} events={evFilter} series={[{ entity: dev, metric: "cpu_pct", label: "CPU" }, { entity: dev, metric: "mem_pct", label: "Memory" }]} axes={[{ fmt: (v) => `${v.toFixed(0)}%`, min: 0, max: 100 }]} />
        </Card>
        {kind === "gateway" && d.wan?.entity_id ? (
          <Card title="WAN traffic">
            <TimeSeries range={range} events={{ device: mac, types: "WAN", min_severity: "low" }}
              series={[{ entity: d.wan.entity_id, metric: "rx_bps", label: "Download", area: true }, { entity: d.wan.entity_id, metric: "tx_bps", label: "Upload", area: true }]} axes={[{ fmt: (v) => fmtBps(v, 0) }]} />
          </Card>
        ) : (
          <Card title="Uplink traffic">
            <TimeSeries range={range} events={evFilter} series={[{ entity: dev, metric: "uplink_rx_bps", label: "RX", area: true }, { entity: dev, metric: "uplink_tx_bps", label: "TX", area: true }]} axes={[{ fmt: (v) => fmtBps(v, 0) }]} />
          </Card>
        )}
      </div>
      <div className="grid g2">
        {caps.temperature ? (
          <Card title="Temperature"><TimeSeries range={range} events={false} series={[{ entity: dev, metric: "temp_c", label: "Temperature" }]} axes={[{ fmt: (v) => `${v.toFixed(1)} °C` }]} /></Card>
        ) : <Card title="Temperature"><div style={{ height: 120, display: "grid", placeItems: "center" }}><NotAvailable what="temperature" /></div></Card>}
        {kind === "gateway" && d.wan?.entity_id ? (
          <Card title="WAN latency"><TimeSeries range={range} events={{ device: mac, min_severity: "low" }} series={[{ entity: d.wan.entity_id, metric: "latency_ms", label: "Latency" }, { entity: d.wan.entity_id, metric: "loss_pct", label: "Loss %", axis: 1, type: "bar", color: "#ef4444" }]}
            axes={[{ fmt: fmtMs }, { fmt: (v) => `${v.toFixed(1)}%`, min: 0 }]} /></Card>
        ) : (
          <Card title="Connected clients"><TimeSeries range={range} events={false} series={[{ entity: dev, metric: "num_sta", label: "Clients" }]} axes={[{ fmt: (v) => v.toFixed(0), min: 0 }]} /></Card>
        )}
      </div>

      {ports.length > 0 && (
        <Card title={kind === "gateway" ? "Interfaces" : "Ports"} right={<span>click a port for its graphs</span>}>
          <PortPanel ports={ports} now={d.now} mac={mac} selected={selPort} onSelect={setPort} live={d.freshness.state === 'LIVE'} />
          <Table rows={ports} rowKey={(p) => String(p.port_idx)} onRow={(p) => setPort(p.port_idx)} selected={selPort !== undefined ? String(selPort) : null}
            cols={[
              { key: "port_idx", label: "#", num: true },
              { key: "name", label: "Name" },
              { key: "up", label: "State", render: (p) => <Pill status={p.up ? "UP" : "OFFLINE"}>{p.up ? "Up" : p.enable === false ? "Disabled" : "Down"}</Pill> },
              { key: "speed", label: "Speed", num: true, render: (p) => (p.up ? `${p.speed} Mbps` : "—") },
              { key: "full_duplex", label: "Duplex", render: (p) => (p.up ? (p.full_duplex ? "full" : "half") : "—") },
              { key: "rx", label: "RX", num: true, sort: (p) => p["rx_bytes-r"], render: (p) => fmtBps(p["rx_bytes-r"] == null ? null : p["rx_bytes-r"] * 8) },
              { key: "tx", label: "TX", num: true, sort: (p) => p["tx_bytes-r"], render: (p) => fmtBps(p["tx_bytes-r"] == null ? null : p["tx_bytes-r"] * 8) },
              { key: "util", label: "Util", num: true, sort: (p) => d.now[`port:${mac}:${p.port_idx}`]?.util_pct, render: (p) => fmtNum(d.now[`port:${mac}:${p.port_idx}`]?.util_pct, 1, "%") },
              { key: "pk", label: "Packets RX/TX", num: true, render: (p) => `${fmtNum(p.rx_packets)} / ${fmtNum(p.tx_packets)}` },
              { key: "bytes", label: "Bytes RX/TX", num: true, render: (p) => `${fmtBytes(p.rx_bytes)} / ${fmtBytes(p.tx_bytes)}` },
              { key: "err", label: "Errors", num: true, sort: (p) => (p.rx_errors || 0) + (p.tx_errors || 0), render: (p) => {
                const n = p.rx_errors == null || p.tx_errors == null ? null : p.rx_errors + p.tx_errors; const r = d.now[`port:${mac}:${p.port_idx}`]?.err_per_min;
                return <span style={{ color: r ? "var(--crit)" : undefined }}>{fmtNum(n)}{r ? ` (${r.toFixed(0)}/min)` : ""}</span>; } },
              { key: "drop", label: "Drops", num: true, render: (p) => fmtNum(p.rx_dropped == null || p.tx_dropped == null ? null : p.rx_dropped + p.tx_dropped) },
              { key: "poe", label: "PoE", num: true, render: (p) => (p.port_poe ? (p.poe_enable ? `${fmtNum(Number(p.poe_power), 1)} W ${p.poe_class ? `(${p.poe_class})` : ""}` : "off") : "—") },
              { key: "net", label: "VLAN / profile", render: (p) => p.network_name || p.forward || <span className="na">n/a</span> },
              { key: "conn", label: "Connected", render: (p) => (d.port_clients[p.port_idx] || []).map((c: any) => (
                <a key={c.mac} style={{ cursor: "pointer", marginRight: 6 }} onClick={(e) => { e.stopPropagation(); nav(c.device ? `/device/${c.mac}` : `/client/${d.slug}/${c.mac}`); }}>{c.name}</a>)) },
              { key: "is_uplink", label: "Uplink", render: (p) => (p.is_uplink ? <span className="tag">uplink</span> : "") },
              { key: "flap", label: "Flaps", num: true, render: (p) => <>{p.link_down_count ?? "—"}{d.now[`port:${mac}:${p.port_idx}`]?.flaps_15m ? <span className="sev warning"> +{d.now[`port:${mac}:${p.port_idx}`].flaps_15m} /15m</span> : null}</> },
              { key: "stp", label: "STP", render: (p) => p.stp_state || "—" },
            ]} />
          {selPort !== undefined && (
            <div className="grid g3" style={{ marginTop: 14 }}>
              <TimeSeries title={`Port ${selPort} traffic`} range={range} height={200} events={false}
                series={[{ entity: `port:${mac}:${selPort}`, metric: "rx_bps", label: "RX", area: true }, { entity: `port:${mac}:${selPort}`, metric: "tx_bps", label: "TX", area: true }]} axes={[{ fmt: (v) => fmtBps(v, 0) }]} />
              <TimeSeries title={`Port ${selPort} errors / drops`} range={range} height={200} events={false}
                series={[{ entity: `port:${mac}:${selPort}`, metric: "err_per_min", label: "Errors/min", color: "#ef4444" }, { entity: `port:${mac}:${selPort}`, metric: "drop_per_min", label: "Drops/min", color: "#f59e0b" },
                  { entity: `port:${mac}:${selPort}`, metric: "up", label: "Link up", step: true, axis: 1, color: "#22c55e" }]}
                axes={[{ fmt: (v) => v.toFixed(1), min: 0 }, { fmt: (v) => v.toFixed(0), min: 0, max: 1 }]} />
              <TimeSeries title={`Port ${selPort} utilization / PoE`} range={range} height={200} events={false}
                series={[{ entity: `port:${mac}:${selPort}`, metric: "util_pct", label: "Utilization %" }, { entity: `port:${mac}:${selPort}`, metric: "poe_w", label: "PoE W", axis: 1, color: "#fbbf24" }]}
                axes={[{ fmt: (v) => `${v.toFixed(1)}%`, min: 0 }, { fmt: (v) => `${v.toFixed(1)} W`, min: 0 }]} />
            </div>
          )}
        </Card>
      )}

      {radios.length > 0 && (
        <>
          <Card title="Radios">
            <Table rows={radios} rowKey={(r) => r.radio}
              cols={[
                { key: "radio", label: "Band", render: (r) => BAND[r.radio] || r.radio },
                { key: "channel", label: "Channel", num: true },
                { key: "ht", label: "Width", num: true, render: (r) => (radioCfg[r.radio]?.ht ? `${radioCfg[r.radio].ht} MHz` : "—") },
                { key: "cu_total", label: "Ch. utilization", num: true, render: (r) => fmtNum(r.cu_total, 0, "%") },
                { key: "cu_self", label: "Self RX / TX", num: true, render: (r) => `${fmtNum(r.cu_self_rx, 0)}% / ${fmtNum(r.cu_self_tx, 0)}%` },
                { key: "other", label: "Other airtime", num: true, render: (r) => fmtNum(r.cu_total == null || r.cu_self_rx == null || r.cu_self_tx == null ? null : Math.max(r.cu_total - r.cu_self_rx - r.cu_self_tx, 0), 0, "%") },
                { key: "tx_retries_pct", label: "Retries", num: true, render: (r) => fmtNum(r.tx_retries_pct, 1, "%") },
                { key: "num_sta", label: "Clients", num: true },
                { key: "tx_power", label: "TX power", num: true, render: (r) => fmtNum(r.tx_power, 0, " dBm") },
                { key: "satisfaction", label: "Satisfaction", num: true, render: (r) => (r.satisfaction >= 0 ? fmtNum(r.satisfaction, 0, "%") : "—") },
                { key: "noise", label: "Noise floor", render: () => <NotAvailable what="AP noise floor" /> },
              ]} />
            <div className="small muted" style={{ marginTop: 6 }}>Other airtime = total airtime − this AP's own RX/TX airtime; this is not a direct measurement of interference. Noise is reported per client on the Clients page.</div>
          </Card>
          <div className="grid g3">
            <Card title="Channel utilization"><TimeSeries range={range} height={200} events={{ device: mac, types: "CHANNEL", min_severity: "low" }}
              series={radios.map((r) => ({ entity: `radio:${mac}:${r.radio}`, metric: "cu_total", label: BAND[r.radio] || r.radio }))} axes={[{ fmt: (v) => `${v.toFixed(0)}%`, min: 0, max: 100 }]} /></Card>
            <Card title="TX retries"><TimeSeries range={range} height={200} events={false}
              series={radios.map((r) => ({ entity: `radio:${mac}:${r.radio}`, metric: "tx_retries_pct", label: BAND[r.radio] || r.radio }))} axes={[{ fmt: (v) => `${v.toFixed(0)}%`, min: 0 }]} /></Card>
            <Card title="Clients per radio"><TimeSeries range={range} height={200} events={false}
              series={radios.map((r) => ({ entity: `radio:${mac}:${r.radio}`, metric: "num_sta", label: BAND[r.radio] || r.radio }))} axes={[{ fmt: (v) => v.toFixed(0), min: 0 }]} /></Card>
          </div>
          {raw.vap_table?.length > 0 && (
            <Card title="SSIDs">
              <Table rows={raw.vap_table} rowKey={(v: any) => `${v.essid}-${v.radio}`}
                cols={[{ key: "essid", label: "SSID" }, { key: "radio", label: "Band", render: (v: any) => BAND[v.radio] || v.radio }, { key: "channel", label: "Ch", num: true },
                  { key: "num_sta", label: "Clients", num: true }, { key: "avg_client_signal", label: "Avg signal", num: true, render: (v: any) => fmtNum(v.avg_client_signal, 0, " dBm") },
                  { key: "satisfaction", label: "Satisfaction", num: true, render: (v: any) => (v.satisfaction >= 0 ? fmtNum(v.satisfaction, 0, "%") : "—") },
                  { key: "tx_retries", label: "TX retries", num: true }, { key: "rx_bytes", label: "RX", num: true, render: (v: any) => fmtBytes(v.rx_bytes) }, { key: "tx_bytes", label: "TX", num: true, render: (v: any) => fmtBytes(v.tx_bytes) }]} />
            </Card>
          )}
        </>
      )}

      {d.clients.length > 0 && (
        <Card title={`Connected clients (${d.clients.length})`}>
          <Table rows={d.clients} rowKey={(c: any) => c.mac} onRow={(c: any) => nav(`/client/${d.slug}/${c.mac}`)}
            cols={[{ key: "name", label: "Name" }, { key: "ip", label: "IP", render: (c: any) => <span className="mono">{c.ip}</span> },
              { key: "conn", label: "Connection", render: (c: any) => (c.is_wired ? `port ${c.sw_port ?? "?"}` : `${c.essid || ""} ${BAND[c.radio] || ""}`) },
              { key: "signal", label: "Signal", num: true, render: (c: any) => (c.is_wired ? "—" : fmtNum(c.signal, 0, " dBm")) }]} />
        </Card>
      )}

      <div className="grid g2">
        <Card title="Events">
          <Table rows={d.events} rowKey={(e: any) => e.id} maxHeight={320} empty="No events for this device"
            cols={[{ key: "ts", label: "Time", render: (e: any) => fmtTime(e.ts, true) }, { key: "severity", label: "Sev", render: (e: any) => <Sev s={e.severity} /> }, { key: "message", label: "Message" }]} />
        </Card>
        <Card title="Alerts">
          <Table rows={d.alerts} rowKey={(a: any) => String(a.id)} maxHeight={320} empty="No alerts for this device"
            cols={[{ key: "started_at", label: "Started", render: (a: any) => fmtTime(a.started_at, true) }, { key: "severity", label: "Sev", render: (a: any) => <Sev s={a.severity} /> },
              { key: "state", label: "State" }, { key: "message", label: "Message" }]} />
        </Card>
      </div>
    </>
  );
}

function Frag({ a, b }: { a: string; b: ReactNode }) {
  return (<><span>{a}</span><span>{b}</span></>);
}

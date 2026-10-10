import { PageTitle } from "../components/ui";
import { useNavigate, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { BAND, fmtBps, fmtDuration, fmtNum, fmtTime } from "../format";
import { TimeSeries } from "../components/charts";
import { Card, ErrorBox, Kpi, Loading, Pill, RangePicker, Sev, Table, useRange } from "../components/ui";

export default function ClientDetail() {
  const { site = "", mac = "" } = useParams();
  const nav = useNavigate();
  const [range, setRange] = useRange("6h");
  const q = useQuery({ queryKey: ["client", site, mac], queryFn: () => api<any>(`/clients/${site}/${mac}`), refetchInterval: 30000 });
  if (q.isError) return <ErrorBox error={q.error} />;
  if (!q.data) return <Loading />;
  const c = q.data;
  const raw = c.raw || {};
  const eid = c.entity_id;
  const ev = { client: mac, min_severity: "low" };
  return (
    <>
      <div className="page-head">
        <div>
          <PageTitle>{c.name}</PageTitle>
          <div className="row" style={{ marginTop: 4 }}>
            <Pill status={c.online ? "ONLINE" : "OFFLINE"}>{c.online ? "Connected" : "Offline"}</Pill>
            <span className="sub">{c.site_name} · {c.is_wired ? "wired" : "Wi-Fi"} · <span className="mono">{c.mac}</span></span>
          </div>
        </div>
        <div className="right"><RangePicker value={range} onChange={setRange} /></div>
      </div>
      <Card title="Connection">
        <div className="kpis">
          <Kpi label="IP" value={<span className="mono" style={{ fontSize: 15 }}>{c.ip || "—"}</span>} hint={c.network ? `network ${c.network}${c.vlan ? ` (VLAN ${c.vlan})` : ""}` : undefined} />
          <Kpi label={c.is_wired ? "Switch / port" : "Access point"} value={<a style={{ cursor: "pointer", fontSize: 15 }} onClick={() => nav(`/device/${c.is_wired ? c.sw_mac : c.ap_mac}`)}>{c.is_wired ? `${c.sw_name || "?"} #${c.sw_port ?? "?"}` : c.ap_name || "—"}</a>} />
          {!c.is_wired && <Kpi label="SSID / band" value={<span style={{ fontSize: 15 }}>{c.essid}</span>} hint={`${BAND[c.radio] || c.radio || ""} · channel ${c.channel ?? "?"} ${raw.radio_proto || ""}`} />}
          {!c.is_wired && <Kpi label="Signal" value={fmtNum(c.signal, 0)} unit="dBm" t={c.signal < -75 ? "warn" : "ok"} hint={`RSSI ${c.rssi ?? "—"} · noise ${fmtNum(c.noise, 0, " dBm")}`} />}
          {!c.is_wired && <Kpi label="Link rate TX/RX" value={`${fmtNum((raw.tx_rate || 0) / 1000)} / ${fmtNum((raw.rx_rate || 0) / 1000)}`} unit="Mbps" />}
          <Kpi label="Throughput now" value={`↓${fmtBps(c.now?.tx_bps)}`} hint={`↑${fmtBps(c.now?.rx_bps)}`} />
          {!c.is_wired && <Kpi label="Retries" value={fmtNum(raw.wifi_tx_retries_percentage, 1)} unit="%" />}
          <Kpi label="Connected for" value={c.online ? fmtDuration(c.uptime) : "—"} hint={c.assoc_time ? `since ${fmtTime(c.assoc_time, true)}` : undefined} />
          <Kpi label="Satisfaction" value={raw.satisfaction >= 0 ? fmtNum(raw.satisfaction, 0) : "—"} unit={raw.satisfaction >= 0 ? "%" : undefined} />
          <Kpi label="First / last seen" value={<span style={{ fontSize: 13 }}>{fmtTime(c.first_seen, true)}</span>} hint={fmtTime(c.last_seen, true)} />
          <Kpi label="Vendor / OS" value={<span style={{ fontSize: 14 }}>{c.oui || "—"}</span>} hint={raw.os_name ? `OS ${raw.os_name}` : undefined} />
        </div>
      </Card>
      <div className="grid g2">
        <Card title="Throughput"><TimeSeries range={range} events={ev} series={[{ entity: eid, metric: "tx_bps", label: "Download", area: true }, { entity: eid, metric: "rx_bps", label: "Upload", area: true }]} axes={[{ fmt: (v) => fmtBps(v, 0) }]} /></Card>
        {!c.is_wired ? (
          <Card title="Signal & noise"><TimeSeries range={range} events={ev} series={[{ entity: eid, metric: "signal_dbm", label: "Signal" }, { entity: eid, metric: "noise_dbm", label: "Noise", color: "#94a3b8" }]} axes={[{ fmt: (v) => `${v.toFixed(0)} dBm` }]} /></Card>
        ) : <Card title="Signal"><div className="empty">Wired client — no radio metrics</div></Card>}
      </div>
      {!c.is_wired && (
        <div className="grid g2">
          <Card title="Link rate"><TimeSeries range={range} events={false} series={[{ entity: eid, metric: "tx_rate_kbps", label: "TX rate", scale: 1000 }, { entity: eid, metric: "rx_rate_kbps", label: "RX rate", scale: 1000 }]} axes={[{ fmt: (v) => fmtBps(v, 0) }]} /></Card>
          <Card title="Retries & satisfaction"><TimeSeries range={range} events={false} series={[{ entity: eid, metric: "retries_pct", label: "Retries %", color: "#f59e0b" }, { entity: eid, metric: "satisfaction", label: "Satisfaction %", color: "#22c55e" }]} axes={[{ fmt: (v) => `${v.toFixed(0)}%`, min: 0 }]} /></Card>
        </div>
      )}
      <Card title="Connection history (connects, disconnects, roaming)">
        <Table rows={c.events} rowKey={(e: any) => e.id} maxHeight={420} empty="No events recorded for this client"
          cols={[{ key: "ts", label: "Time", render: (e: any) => fmtTime(e.ts, true) }, { key: "event_type", label: "Event", render: (e: any) => <span className="tag">{e.event_type}</span> },
            { key: "severity", label: "Sev", render: (e: any) => <Sev s={e.severity} /> }, { key: "device", label: "Device" }, { key: "message", label: "Detail" }]} />
      </Card>
    </>
  );
}

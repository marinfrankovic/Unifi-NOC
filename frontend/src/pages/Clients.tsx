import { PageTitle } from "../components/ui";
import { useMemo } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, qs } from "../api";
import { BAND, fmtBps, fmtDuration, fmtNum, fmtTime } from "../format";
import { Card, ErrorBox, Loading, Seg, Table } from "../components/ui";

const FLAGS: Record<string, string> = {
  poor_signal: "Poor signal", high_bandwidth: "High bandwidth", frequent_roaming: "Frequent roaming", frequent_disconnects: "Frequent disconnects", high_retries: "High retries",
};

export default function Clients() {
  const [sp, setSp] = useSearchParams();
  const nav = useNavigate();
  const get = (k: string, d = "") => sp.get(k) || d;
  const set = (k: string, v: string) => { const x = new URLSearchParams(sp); if (v) x.set(k, v); else x.delete(k); setSp(x, { replace: true }); };
  const site = get("site", "all"), status = get("status", "online"), conn = get("conn"), band = get("band"), ap = get("ap"), sw = get("switch"), network = get("network"), search = get("q"), flag = get("flag");
  const sites = useQuery({ queryKey: ["sites"], queryFn: () => api<any[]>("/sites") });
  const devs = useQuery({ queryKey: ["devices-all"], queryFn: () => api<any[]>("/devices"), staleTime: 60000 });
  const q = useQuery({
    queryKey: ["clients", site, status, conn, band, ap, sw, network, search],
    queryFn: () => api<any[]>(`/clients${qs({ site: site === "all" ? undefined : site, online: status === "all" ? undefined : status === "online", conn, band, ap, switch: sw, network, q: search })}`),
    refetchInterval: 30000,
  });
  const rows = useMemo(() => (q.data || []).filter((r) => !flag || r.flags.includes(flag)), [q.data, flag]);
  const counts = useMemo(() => {
    const c: Record<string, number> = {};
    (q.data || []).forEach((r) => r.flags.forEach((f: string) => (c[f] = (c[f] || 0) + 1)));
    return c;
  }, [q.data]);
  const networks = [...new Set((q.data || []).map((r) => r.network).filter(Boolean))];
  if (q.isError) return <ErrorBox error={q.error} />;
  return (
    <>
      <div className="page-head">
        <div><PageTitle>Clients</PageTitle><div className="sub">{rows.length} shown · data from the controller's client table (30 s)</div></div>
      </div>
      <Card>
        <div className="filters">
          <Seg value={site} onChange={(v) => set("site", v === "all" ? "" : v)} options={[{ v: "all", l: "All sites" }, ...(sites.data || []).map((s) => ({ v: s.slug, l: s.description }))]} />
          <Seg value={status} onChange={(v) => set("status", v === "online" ? "" : v)} options={[{ v: "online", l: "Connected" }, { v: "offline", l: "Offline" }, { v: "all", l: "All" }]} />
          <Seg value={conn || "any"} onChange={(v) => set("conn", v === "any" ? "" : v)} options={[{ v: "any", l: "Any" }, { v: "wifi", l: "Wi-Fi" }, { v: "wired", l: "Wired" }]} />
          <select value={band} onChange={(e) => set("band", e.target.value)}><option value="">Any band</option><option value="ng">2.4 GHz</option><option value="na">5 GHz</option><option value="6e">6 GHz</option></select>
          <select value={ap} onChange={(e) => set("ap", e.target.value)}><option value="">Any AP</option>{(devs.data || []).filter((d) => d.device_type === "ap").map((d) => <option key={d.mac} value={d.mac}>{d.name}</option>)}</select>
          <select value={sw} onChange={(e) => set("switch", e.target.value)}><option value="">Any switch</option>{(devs.data || []).filter((d) => d.device_type === "switch" || d.device_type === "gateway").map((d) => <option key={d.mac} value={d.mac}>{d.name}</option>)}</select>
          <select value={network} onChange={(e) => set("network", e.target.value)}><option value="">Any network/VLAN</option>{networks.map((n) => <option key={n}>{n}</option>)}</select>
          <input type="search" placeholder="Search hostname, name, IP, MAC, vendor" defaultValue={search} onKeyDown={(e) => { if (e.key === "Enter") set("q", (e.target as HTMLInputElement).value); }} />
        </div>
        <div className="filters" style={{ marginTop: 10 }}>
          {Object.entries(FLAGS).map(([k, l]) => (
            <span key={k} className={`chip ${flag === k ? "on" : ""}`} onClick={() => set("flag", flag === k ? "" : k)}>{l} <b>{counts[k] || 0}</b></span>
          ))}
        </div>
      </Card>
      <Card>
        {!q.data ? <Loading /> : (
          <Table rows={rows} rowKey={(r) => `${r.site}-${r.mac}`} onRow={(r) => nav(`/client/${r.slug}/${r.mac}`)} initialSort={{ key: "name", dir: 1 }}
            cols={[
              { key: "name", label: "Client", render: (r) => <><span className={`dot ${r.online ? (r.flags.length ? "warn" : "ok") : "off"}`} /> {r.name}{r.hostname && r.hostname !== r.name ? <span className="small muted"> ({r.hostname})</span> : null}</> },
              { key: "site_name", label: "Site" },
              { key: "ip", label: "IP", render: (r) => <span className="mono">{r.ip || "—"}</span> },
              { key: "mac", label: "MAC", render: (r) => <span className="mono small">{r.mac}</span> },
              { key: "oui", label: "Vendor" },
              { key: "network", label: "Network" },
              { key: "conn", label: "Connected via", sort: (r) => r.ap_name || r.sw_name, render: (r) => (r.is_wired ? `${r.sw_name || "?"} port ${r.sw_port ?? "?"}` : `${r.ap_name || "?"}`) },
              { key: "band", label: "Band / ch", render: (r) => (r.is_wired ? "wired" : `${BAND[r.radio] || r.radio || ""} ch${r.channel ?? "?"} ${r.proto || ""}`) },
              { key: "signal", label: "Signal", num: true, render: (r) => (r.is_wired ? "—" : <span style={{ color: r.signal < -75 ? "var(--warn)" : undefined }}>{fmtNum(r.signal, 0, " dBm")}</span>) },
              { key: "noise", label: "Noise", num: true, render: (r) => (r.is_wired ? "—" : fmtNum(r.noise, 0, " dBm")) },
              { key: "rate", label: "TX/RX rate", num: true, sort: (r) => r.tx_rate, render: (r) => (r.is_wired ? "—" : `${fmtNum((r.tx_rate || 0) / 1000)} / ${fmtNum((r.rx_rate || 0) / 1000)} Mbps`) },
              { key: "thr", label: "Throughput", num: true, sort: (r) => (r.now?.rx_bps || 0) + (r.now?.tx_bps || 0), render: (r) => (r.online ? `↓${fmtBps(r.now?.tx_bps)} ↑${fmtBps(r.now?.rx_bps)}` : "—") },
              { key: "retries_pct", label: "Retries", num: true, render: (r) => (r.is_wired ? "—" : fmtNum(r.retries_pct, 0, "%")) },
              { key: "uptime", label: "Connected for", num: true, render: (r) => (r.online ? fmtDuration(r.uptime) : "—") },
              { key: "roams_24h", label: "Roams 24h", num: true },
              { key: "disconnects_24h", label: "Disc. 24h", num: true },
              { key: "last_seen", label: "Last seen", render: (r) => fmtTime(r.last_seen) },
              { key: "flags", label: "Flags", sort: (r) => r.flags.length, render: (r) => r.flags.map((f: string) => <span key={f} className="tag warn">{FLAGS[f]}</span>) },
            ]} />
        )}
        <div className="small muted" style={{ marginTop: 8 }}>Throughput direction is from the client's perspective (↓ download, ↑ upload). Roams/disconnects are counted from controller events.</div>
      </Card>
    </>
  );
}

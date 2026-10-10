import { PageTitle } from "../components/ui";
import { Fragment } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { fmtBytes, fmtDuration, fmtNum, fmtTime, NA } from "../format";
import { Card, ErrorBox, Json, Kpi, Loading, Pill, Table, tone } from "../components/ui";

export default function Diagnostics({ isAdmin }: { isAdmin: boolean }) {
  const qc = useQueryClient();
  const st = useQuery({ queryKey: ["status"], queryFn: () => api<any>("/status"), refetchInterval: 10000 });
  const dg = useQuery({ queryKey: ["diagnostics"], queryFn: () => api<any>("/diagnostics"), refetchInterval: 60000 });
  const reset = useMutation({ mutationFn: (site: string) => api(`/diagnostics/hostkey/${site}/reset`, { method: "POST" }), onSuccess: () => qc.invalidateQueries({ queryKey: ["diagnostics"] }) });
  if (st.isError) return <ErrorBox error={st.error} />;
  if (!st.data || !dg.data) return <Loading />;
  const s = st.data, d = dg.data;
  const hb = s.collector.heartbeat || {};
  const caps = Object.entries(d.capabilities || {}) as [string, any][];
  const capKeys = [...new Set(caps.flatMap(([, v]) => Object.keys(v.capabilities || {})))];
  return (
    <>
      <div className="page-head"><div><PageTitle>Diagnostics</PageTitle><div className="sub">Self-monitoring, discovered capabilities and raw controller data</div></div></div>
      <div className="grid g4">
        <Card title="Collector"><Kpi label="State" value={<Pill status={s.collector.state}>{s.collector.state}</Pill>}
          hint={<>heartbeat {s.collector.freshness.age_s ?? "?"} s ago · up {fmtDuration(hb.started ? Date.now() / 1000 - hb.started : null)}<br />UniFi requests {hb.unifi_requests} · errors {hb.unifi_errors} · last {hb.unifi_last_latency_ms} ms</>} /></Card>
        <Card title="Backend API"><Kpi label="State" value={<Pill status={s.api.state}>{s.api.state}</Pill>} hint={<>up {fmtDuration(s.api.uptime_s)} · {s.api.requests} requests · {s.api.errors} errors{s.api.last_error ? <><br />last error {s.api.last_error.path}</> : null}</>} /></Card>
        <Card title="Frontend"><Kpi label="State" value={<Pill status={s.frontend.state === "OK" ? "OK" : "ERROR"}>{s.frontend.state}</Pill>} hint={s.frontend.error || "nginx health endpoint"} /></Card>
        <Card title="Storage"><Kpi label="Database" value={<Pill status={s.database.state}>{s.database.state}</Pill>}
          hint={<>size {fmtBytes(s.database.db_bytes)} · metrics {fmtBytes(s.database.metrics_bytes)}<br />~{fmtNum(s.database.metric_rows)} metric rows · {s.database.chunks} chunks · {fmtNum(s.database.events)} events</>} /></Card>
      </div>
      {s.missing_sites?.length > 0 && <div className="banner crit">Configured site(s) not found on the controller: {s.missing_sites.join(", ")}</div>}
      <Card title="Collection sources" right={`controller ${s.controller.version || "?"} · ${s.controller.url || ""}`}>
        <Table rows={s.sources} rowKey={(r: any) => r.name}
          cols={[
            { key: "name", label: "Source", render: (r: any) => <span className="mono">{r.name}</span> },
            { key: "freshness", label: "Freshness", render: (r: any) => <Pill status={r.freshness}>{r.freshness}</Pill> },
            { key: "interval", label: "Interval", num: true, render: (r: any) => `${r.interval} s` },
            { key: "last_ok", label: "Last success", render: (r: any) => (r.last_ok ? fmtTime(r.last_ok * 1000) : "never") },
            { key: "age_s", label: "Data age", num: true, render: (r: any) => (r.age_s !== null ? `${r.age_s} s` : "—") },
            { key: "duration_ms", label: "Duration", num: true, render: (r: any) => `${r.duration_ms} ms` },
            { key: "runs", label: "Runs", num: true },
            { key: "total_errors", label: "Errors", num: true },
            { key: "consecutive_errors", label: "In a row", num: true, render: (r: any) => <span style={{ color: r.consecutive_errors ? "var(--crit)" : undefined }}>{r.consecutive_errors}</span> },
            { key: "error", label: "Last error", render: (r: any) => <span className="small" style={{ color: "var(--crit)" }}>{r.error || ""}</span> },
          ]} />
      </Card>
      <div className="grid g2">
        <Card title="Gateway SSH (live counters)">
          <Table rows={Object.entries(d.per_site) as [string, any][]} rowKey={(r) => r[0]}
            cols={[
              { key: "site", label: "Site", render: (r) => d.sites[r[0]]?.description },
              { key: "host", label: "Gateway", render: (r) => <span className="mono">{r[1].gateway_hostkey?.host || "—"}</span> },
              { key: "fp", label: "Pinned host key", render: (r) => <span className="mono small">{r[1].gateway_hostkey ? `${r[1].gateway_hostkey.algorithm} ${r[1].gateway_hostkey.fingerprint}` : "not pinned yet"}</span> },
              { key: "since", label: "Pinned", render: (r) => (r[1].gateway_hostkey?.first_seen ? fmtTime(r[1].gateway_hostkey.first_seen * 1000, true) : "—") },
              { key: "live", label: "Live poll", render: (r) => { const g = s.sources.find((x: any) => x.name === "live")?.gateways?.[r[0]]; return g ? <Pill status={g.ok ? "OK" : "ERROR"}>{g.ok ? "ok" : g.error}</Pill> : "—"; } },
              { key: "act", label: "", render: (r) => isAdmin && r[1].gateway_hostkey ? <button onClick={() => { if (confirm("Clear the pinned host key? The next connection will trust whatever key the gateway presents. Only do this after verifying a legitimate key change.")) reset.mutate(d.sites[r[0]].slug); }}>Reset pin</button> : null },
            ]} />
          <div className="small muted" style={{ marginTop: 6 }}>Read-only commands only (<span className="mono">cat /proc/net/dev</span>, <span className="mono">wg show</span>). Credentials come from the controller's device SSH settings and are held in collector memory only.</div>
        </Card>
        <Card title="Maintenance jobs (TimescaleDB)">
          <Table rows={s.database.jobs || []} rowKey={(r: any) => String(r.job_id)}
            cols={[{ key: "job_id", label: "Job", num: true }, { key: "proc_name", label: "Policy" }, { key: "last_run_status", label: "Last run", render: (r: any) => <Pill status={r.last_run_status === "Success" ? "OK" : r.last_run_status ? "ERROR" : "UNKNOWN"}>{r.last_run_status || "pending"}</Pill> },
              { key: "last_successful_finish", label: "Last success", render: (r: any) => fmtTime(r.last_successful_finish, true) }, { key: "total_failures", label: "Failures", num: true }]} />
        </Card>
      </div>
      <Card title="Discovered metric availability per device">
        <div className="table-wrap">
          <table className="t">
            <thead><tr><th>Device</th><th>Type</th>{capKeys.map((k) => <th key={k}>{k.replace(/_/g, " ")}</th>)}</tr></thead>
            <tbody>
              {caps.map(([mac, v]) => (
                <tr key={mac}><td>{v.name}</td><td>{v.kind}</td>
                  {capKeys.map((k) => <td key={k} title={v.capabilities[k] ? "available" : NA}>{v.capabilities[k] === undefined ? "" : v.capabilities[k] ? <span className="dot ok" /> : <span className="dot off" />}</td>)}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div className="small muted" style={{ marginTop: 6 }}><span className="dot ok" /> exposed by the controller · <span className="dot off" /> {NA} (never displayed as a value)</div>
      </Card>
      <div className="grid g2">
        <Card title="Settings (non-secret)"><div className="kv small">{Object.entries(d.settings).map(([k, v]) => <Fragment key={k}><span>{k}</span><span className="mono">{JSON.stringify(v)}</span></Fragment>)}</div></Card>
        <Card title="Authentication events">
          <Table rows={d.auth_events} rowKey={(r: any) => r.ts + r.event} maxHeight={300}
            cols={[{ key: "ts", label: "Time", render: (r: any) => fmtTime(r.ts, true) }, { key: "event", label: "Event", render: (r: any) => <span className={`sev ${r.event === "login_ok" ? "info" : r.event === "logout" ? "low" : "critical"}`}>{r.event}</span> },
              { key: "username", label: "User" }, { key: "ip", label: "IP", render: (r: any) => <span className="mono">{r.ip}</span> }]} />
        </Card>
      </div>
      {Object.entries(d.per_site).map(([site, v]: [string, any]) => (
        <Card key={site} title={`Raw controller data — ${d.sites[site]?.description}`}>
          <details><summary>stat/health (sanitized)</summary><Json data={v.unifi_health} /></details>
          <details><summary>WAN state</summary><Json data={v.wan} /></details>
          <details><summary>Networks / VLANs</summary><Json data={v.networks} /></details>
          <details><summary>Dashboard (routability, ISP metrics)</summary><Json data={v.dashboard} /></details>
          <div className="small muted">UniFi health status for the VPN subsystem: <Pill status={tone(v.unifi_health?.vpn?.status) === "ok" ? "OK" : "UNKNOWN"}>{v.unifi_health?.vpn?.status || "n/a"}</Pill> — this legacy counter includes inactive legacy VPN definitions; the live SD-WAN state is on the Site-to-Site page.</div>
        </Card>
      ))}
      <Card title="SD-WAN configuration (sanitized)"><details><summary>magicsitetositevpn</summary><Json data={d.tunnels_raw} /></details></Card>
    </>
  );
}

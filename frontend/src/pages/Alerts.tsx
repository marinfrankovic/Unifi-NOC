import { PageTitle } from "../components/ui";
import { useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertRow, api, qs, User } from "../api";
import { entityDeviceMac, fmtDuration, fmtNum, fmtTime } from "../format";
import { Card, ErrorBox, Loading, Pill, Seg, Sev, Table } from "../components/ui";

function entityLink(a: AlertRow): string | null {
  const mac = entityDeviceMac(a.entity_id);
  if (mac) return `/device/${mac}`;
  const p = a.entity_id.split(":");
  if (p[0] === "tunnel") return "/s2s";
  if (p[0] === "wan") return "/wan";
  if (p[0] === "client" && a.slug) return `/client/${a.slug}/${p.slice(2).join(":")}`;
  if (p[0] === "site" && a.slug) return `/site/${a.slug}`;
  if (p[0] === "noc") return "/diagnostics";
  return null;
}

export default function Alerts({ isAdmin, isOwner, user }: { isAdmin: boolean; isOwner: boolean; user: User }) {
  const [sp, setSp] = useSearchParams();
  const tab = sp.get("tab") || "active";
  const site = sp.get("site") || "all";
  const nav = useNavigate();
  const qc = useQueryClient();
  const sites = useQuery({ queryKey: ["sites"], queryFn: () => api<any[]>("/sites") });
  const state = tab === "active" ? "firing" : "all";
  const q = useQuery({
    queryKey: ["alerts", tab, site], enabled: tab !== "rules",
    queryFn: () => api<AlertRow[]>(`/alerts${qs({ state, site: site === "all" ? undefined : site, kind: tab === "anomalies" ? "anomaly" : undefined, range: "30d", limit: 1000 })}`),
    refetchInterval: 15000,
  });
  const ack = useMutation({ mutationFn: (id: number) => api(`/alerts/${id}/ack`, { method: "POST" }), onSuccess: () => {
    qc.invalidateQueries({ queryKey: ["alerts"] });
    qc.invalidateQueries({ queryKey: ["overview"] });
  } });
  const setP = (k: string, v: string) => { const x = new URLSearchParams(sp); x.set(k, v); setSp(x, { replace: true }); };
  return (
    <>
      <div className="page-head">
        <div><PageTitle>Alerts</PageTitle><div className="sub">Threshold alerts with debounce and hysteresis; statistical anomalies are listed separately and are not confirmed failures</div></div>
        <div className="right"><Seg value={site} onChange={(v) => setP("site", v)} options={[{ v: "all", l: "All sites" }, ...(sites.data || []).map((s) => ({ v: s.slug, l: s.description }))]} /></div>
      </div>
      <div className="tabs">
        {[["active", "Active"], ["history", "History (30 d)"], ["anomalies", "Anomalies"], ["rules", "Rules"]].map(([k, l]) => (
          <button key={k} className={tab === k ? "on" : ""} onClick={() => setP("tab", k)}>{l}</button>
        ))}
      </div>
      {tab === "rules" ? <Rules site={site} isAdmin={isOwner || user.site_roles[(sites.data || []).find(item => item.slug === site)?.id] === 'admin'} /> : q.isError ? <ErrorBox error={q.error} /> : !q.data ? <Loading /> : (
        <Card>
          <Table rows={tab === "active" ? q.data.filter((a) => a.kind !== "anomaly") : q.data} rowKey={(a) => String(a.id)} onRow={(a) => { const l = entityLink(a); if (l) nav(l); }}
            empty={tab === "active" ? "No active alerts — every monitored condition is OK" : "Nothing recorded"}
            cols={[
              { key: "severity", label: "Severity", render: (a) => <Sev s={a.severity} /> },
              { key: "state", label: "State", render: (a) => <Pill status={a.state === "firing" ? (a.severity === "critical" ? "CRITICAL" : a.kind === "anomaly" ? "INFO" : "WARNING") : "RESOLVED"}>{a.state === "firing" ? (a.kind === "anomaly" ? "anomaly" : a.severity) : "resolved"}</Pill> },
              { key: "site_name", label: "Site", render: (a) => a.site_name || (a.site === "_all" ? "NOC" : a.site) },
              { key: "entity_name", label: "Device / entity", render: (a) => a.entity_name || a.entity_id },
              { key: "metric", label: "Metric" },
              { key: "condition", label: "Condition", render: (a) => <span className="mono small">{a.condition}</span> },
              { key: "value", label: "Current", num: true, render: (a) => fmtNum(a.value, 2) },
              { key: "threshold", label: "Threshold", num: true, render: (a) => fmtNum(a.threshold, 2) },
              { key: "peak_value", label: "Peak", num: true, render: (a) => fmtNum(a.peak_value, 2) },
              { key: "started_at", label: "Started", render: (a) => fmtTime(a.started_at, true) },
              { key: "duration_s", label: "Duration", num: true, render: (a) => fmtDuration(a.duration_s) },
              { key: "resolved_at", label: "Resolved", render: (a) => (a.resolved_at ? fmtTime(a.resolved_at, true) : "—") },
              { key: "message", label: "Message", render: (a) => <span className="small">{a.message}</span> },
              { key: "ack", label: "Ack", render: (a) => a.acked_by ? <span className="small muted">{a.acked_by}</span> : a.state === "firing" && isAdmin && a.can_ack ? <button onClick={(e) => { e.stopPropagation(); ack.mutate(a.id); }}>Ack</button> : "" },
            ]} />
        </Card>
      )}
    </>
  );
}

function Rules({ isAdmin, site }: { isAdmin: boolean; site: string }) {
  const qc = useQueryClient();
  const q = useQuery({ queryKey: ["rules",site], queryFn: () => api<any[]>(`/alert-rules${qs({site:site==='all'?undefined:site})}`) });
  const [edit, setEdit] = useState<Record<string, any>>({});
  const save = useMutation({
    mutationFn: ({ id, body }: { id: string; body: any }) => api(`/alert-rules/${id}${qs({site:site==='all'?undefined:site})}`, { method: "PUT", body: JSON.stringify(body) }),
    onSuccess: (_, v) => { setEdit((e) => { const x = { ...e }; delete x[v.id]; return x; }); qc.invalidateQueries({ queryKey: ["rules"] }); },
  });
  if (!q.data) return <Loading />;
  const num = (v: string) => (v === "" ? null : Number(v));
  const field = (r: any, k: string, w = 70) => isAdmin ? (
    <input style={{ width: w }} value={edit[r.id]?.[k] ?? (r[k] ?? "")} onChange={(e) => setEdit((x) => ({ ...x, [r.id]: { ...(x[r.id] || {}), [k]: e.target.value } }))} />
  ) : (r[k] ?? "—");
  return (
    <Card right={isAdmin ? "Changes apply within 30 s" : "Read-only (administrator role required to edit)"}>
      {save.isError && <ErrorBox error={save.error} />}
      <Table rows={q.data} rowKey={(r) => r.id}
        cols={[
          { key: "enabled", label: "On", render: (r) => <input type="checkbox" disabled={!isAdmin} checked={edit[r.id]?.enabled ?? r.enabled}
            onChange={(e) => save.mutate({ id: r.id, body: { enabled: e.target.checked } })} /> },
          { key: "name", label: "Rule", render: (r) => <><b>{r.name}</b><div className="small muted">{r.description}</div></> },
          { key: "entity_type", label: "Applies to" },
          { key: "metric", label: "Metric", render: (r) => <span className="mono small">{r.metric} {r.op}</span> },
          { key: "warn", label: "Warning", render: (r) => field(r, "warn") },
          { key: "crit", label: "Critical", render: (r) => field(r, "crit") },
          { key: "unit", label: "Unit" },
          { key: "for_s", label: "Debounce s", render: (r) => field(r, "for_s", 60) },
          { key: "clear_for_s", label: "Clear after s", render: (r) => field(r, "clear_for_s", 60) },
          { key: "hysteresis", label: "Hysteresis", render: (r) => field(r, "hysteresis", 60) },
          { key: "save", label: "", render: (r) => edit[r.id] && isAdmin ? (
            <button className="primary" onClick={() => {
              const e = edit[r.id]; const body: any = {};
              for (const k of ["warn", "crit", "hysteresis"]) if (k in e) body[k] = num(e[k]);
              for (const k of ["for_s", "clear_for_s"]) if (k in e) body[k] = Number(e[k]);
              save.mutate({ id: r.id, body });
            }}>Save</button>) : null },
        ]} />
    </Card>
  );
}

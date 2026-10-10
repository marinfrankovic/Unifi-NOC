import { PageTitle } from "../components/ui";
import { useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, EventRow, qs } from "../api";
import { fmtTime } from "../format";
import { Card, Drawer, ErrorBox, EventDetail, Loading, RangePicker, Seg, Sev, Table, useRange } from "../components/ui";

const PAGE = 100;

export default function Events() {
  const [sp, setSp] = useSearchParams();
  const [range, setRange] = useRange("24h");
  const get = (k: string) => sp.get(k) || "";
  const set = (k: string, v: string) => { const x = new URLSearchParams(sp); if (v) x.set(k, v); else x.delete(k); x.delete("page"); setSp(x, { replace: true }); };
  const page = Number(sp.get("page") || 0);
  const [open, setOpen] = useState<EventRow | null>(null);
  const site = get("site") || "all";
  const sites = useQuery({ queryKey: ["sites"], queryFn: () => api<any[]>("/sites") });
  const params = { site: site === "all" ? undefined : site, min_severity: get("sev") || undefined, type: get("type") || undefined, device: get("device") || undefined,
    client: get("client") || undefined, q: get("q") || undefined, source: get("source") || undefined, range, limit: PAGE, offset: page * PAGE };
  const q = useQuery({ queryKey: ["events", params], queryFn: () => api<{ total: number; items: EventRow[]; types: { event_type: string; n: number }[] }>(`/events${qs(params)}`), refetchInterval: 30000 });
  if (q.isError) return <ErrorBox error={q.error} />;
  return (
    <>
      <div className="page-head">
        <div><PageTitle>Events</PageTitle><div className="sub">Normalized controller events and NOC-generated events · {q.data?.total ?? "…"} matching</div></div>
        <div className="right"><RangePicker value={range} onChange={setRange} /></div>
      </div>
      <Card>
        <div className="filters">
          <Seg value={site} onChange={(v) => set("site", v === "all" ? "" : v)} options={[{ v: "all", l: "All sites" }, ...(sites.data || []).map((s) => ({ v: s.slug, l: s.description }))]} />
          <select value={get("sev")} onChange={(e) => set("sev", e.target.value)}>
            <option value="">Any severity</option><option value="critical">Critical</option><option value="high">High and above</option>
            <option value="medium">Medium and above</option><option value="low">Low and above</option>
          </select>
          <select value={get("type")} onChange={(e) => set("type", e.target.value)}>
            <option value="">Any type</option>
            {(q.data?.types || []).map((t) => <option key={t.event_type} value={t.event_type}>{t.event_type} ({t.n})</option>)}
          </select>
          <select value={get("source")} onChange={(e) => set("source", e.target.value)}><option value="">Any source</option><option value="unifi">UniFi controller</option><option value="noc">NOC (collector / alerts)</option></select>
          <input placeholder="Device (name or MAC)" defaultValue={get("device")} onKeyDown={(e) => e.key === "Enter" && set("device", (e.target as HTMLInputElement).value)} />
          <input placeholder="Client (name or MAC)" defaultValue={get("client")} onKeyDown={(e) => e.key === "Enter" && set("client", (e.target as HTMLInputElement).value)} />
          <input type="search" placeholder="Search message text" defaultValue={get("q")} onKeyDown={(e) => e.key === "Enter" && set("q", (e.target as HTMLInputElement).value)} />
        </div>
      </Card>
      <Card>
        {!q.data ? <Loading /> : (
          <>
            <Table rows={q.data.items} rowKey={(r) => r.id} onRow={setOpen}
              cols={[
                { key: "ts", label: "Time", render: (r) => fmtTime(r.ts, true) },
                { key: "site_name", label: "Site", render: (r) => r.site_name || r.site },
                { key: "severity", label: "Severity", render: (r) => <Sev s={r.severity} /> },
                { key: "event_type", label: "Type", render: (r) => <span className="tag">{r.event_type}</span> },
                { key: "device", label: "Device", render: (r) => r.device || "" },
                { key: "client", label: "Client", render: (r) => r.client || "" },
                { key: "message", label: "Message" },
                { key: "source", label: "Source", render: (r) => <span className="small muted">{r.source}</span> },
              ]} />
            <div className="row" style={{ marginTop: 10, justifyContent: "flex-end" }}>
              <button disabled={page === 0} onClick={() => { const x = new URLSearchParams(sp); x.set("page", String(page - 1)); setSp(x); }}>Previous</button>
              <span className="small muted">page {page + 1} of {Math.max(1, Math.ceil(q.data.total / PAGE))}</span>
              <button disabled={(page + 1) * PAGE >= q.data.total} onClick={() => { const x = new URLSearchParams(sp); x.set("page", String(page + 1)); setSp(x); }}>Next</button>
            </div>
          </>
        )}
      </Card>
      {open && <Drawer onClose={() => setOpen(null)}><EventDetail e={open} /></Drawer>}
    </>
  );
}

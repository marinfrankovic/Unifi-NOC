import { ReactNode, useMemo, useState } from "react";
import { Link, useLocation, useSearchParams } from "react-router-dom";
import { EventRow, Freshness } from "../api";
import { fmtTime, NA } from "../format";

export type Tone = "ok" | "warn" | "crit" | "off" | "unk" | "info";

export function Brand({ subtitle, heading = false }: { subtitle: string; heading?: boolean }) {
  const Name = heading ? 'h1' : 'div';
  return <div className="brand"><img className="brand-mark" src={`${import.meta.env.BASE_URL}favicon.svg`} width="40" height="40" alt="" />
    <div><Name className="brand-name">UniFi <strong>NOC</strong></Name><small>{subtitle}</small></div>
  </div>;
}

export function PageTitle({ children, parent, breadcrumbs = true }: { children: ReactNode; parent?: { label: string; to: string }; breadcrumbs?: boolean }) {
  const { pathname } = useLocation();
  const settingsPage = ['/account', '/users', '/connection', '/monitoring', '/notifications', '/diagnostics', '/maintenance'].includes(pathname);
  const ancestor = parent || (settingsPage ? { label: 'Settings', to: undefined } : pathname.startsWith('/client/') ? { label: 'Clients', to: '/clients' } : undefined);
  return <div className="page-title"><h1>{children}</h1>{breadcrumbs && <nav className="breadcrumbs" aria-label="Breadcrumb"><ol>
    {pathname !== '/' && <li><Link to="/">Network Overview</Link></li>}
    {ancestor && <li>{ancestor.to ? <Link to={ancestor.to}>{ancestor.label}</Link> : <span>{ancestor.label}</span>}</li>}
    <li aria-current="page">{children}</li>
  </ol></nav>}</div>;
}

export function tone(status?: string | boolean | null): Tone {
  if (status === true) return "ok";
  if (status === false) return "off";
  switch ((status || "").toString().toUpperCase()) {
    case "HEALTHY": case "CONNECTED": case "OK": case "ONLINE": case "LIVE": case "UP": case "RESOLVED": return "ok";
    case "WARNING": case "DEGRADED": case "STALE": case "MEDIUM": return "warn";
    case "CRITICAL": case "DOWN": case "ERROR": case "HIGH": case "FIRING": case "DATA COLLECTION ERROR": return "crit";
    case "OFFLINE": return "off";
    case "INFO": return "info";
    default: return "unk";
  }
}

export function Pill({ status, children, t }: { status?: string | boolean | null; children?: ReactNode; t?: Tone }) {
  const label = children ?? (typeof status === "boolean" ? (status ? "Online" : "Offline") : status || "Unknown");
  return <span className={`pill ${t || tone(status)}`}>{label}</span>;
}

export function Sev({ s }: { s: string }) {
  return <span className={`sev ${s}`}>{s}</span>;
}

export function healthTone(score: number | null | undefined): Tone {
  if (score === null || score === undefined) return "unk";
  return score >= 90 ? "ok" : score >= 70 ? "warn" : "crit";
}

export function HealthRing({ score, size = 120, label = "/ 100" }: { score: number | null | undefined; size?: number; label?: string }) {
  const r = size / 2 - 8;
  const c = 2 * Math.PI * r;
  const t = healthTone(score);
  const color = `var(--${t === "unk" ? "unk" : t})`;
  return (
    <div className="ring" style={{ width: size, height: size, fontSize: size / 6.5 }}>
      <svg width={size} height={size}>
        <circle cx={size / 2} cy={size / 2} r={r} stroke="var(--grid)" strokeWidth={8} fill="none" />
        {score !== null && score !== undefined && (
          <circle cx={size / 2} cy={size / 2} r={r} stroke={color} strokeWidth={8} fill="none" strokeLinecap="round"
            strokeDasharray={`${(c * Math.max(score, 0)) / 100} ${c}`} />
        )}
      </svg>
      <div className="num">
        <b style={{ color }}>{score ?? "?"}</b>
        <span>{score === null || score === undefined ? "NO DATA" : label}</span>
      </div>
    </div>
  );
}

export function Card({ title, right, children, className = "", pad = false }: { title?: ReactNode; right?: ReactNode; children: ReactNode; className?: string; pad?: boolean }) {
  return (
    <section className={`card ${className}`}>
      {(title || right) && (
        <div className="head">
          {typeof title === "string" ? <h3>{title}</h3> : title}
          {right && <div className="right">{right}</div>}
        </div>
      )}
      <div className={pad ? "" : "body"}>{children}</div>
    </section>
  );
}

export function Kpi({ label, value, unit, hint, t }: { label: string; value: ReactNode; unit?: string; hint?: ReactNode; t?: Tone }) {
  return (
    <div className="kpi">
      <span className="label">{label}</span>
      <span className="value" style={t ? { color: `var(--${t})` } : undefined}>
        {value ?? "—"}
        {unit && <small>{unit}</small>}
      </span>
      {hint && <span className="hint">{hint}</span>}
    </div>
  );
}

export function NotAvailable({ what }: { what?: string }) {
  return <span className="na" title={what ? `${what}: the controller does not expose this metric` : undefined}>{NA}</span>;
}

export function FreshBadge({ f, interval }: { f?: Freshness | null; interval?: number }) {
  if (!f) return null;
  if (f.state === "LIVE")
    return (
      <span className="row small muted" style={{ gap: 6 }}>
        <span className="dot ok pulse" /> Last update {fmtTime(f.last_update)}
        {interval ? <span className="muted"> · refresh {interval} s</span> : null}
      </span>
    );
  if (f.state === "NO_DATA") return <span className="stale">NO DATA</span>;
  return (
    <span className="stale">
      DATA STALE · last update {fmtTime(f.last_update, true)}
    </span>
  );
}

export function Empty({ children = "No data" }: { children?: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function Loading() {
  return <div className="empty">Loading…</div>;
}

export function ErrorBox({ error }: { error: unknown }) {
  return <div className="banner crit">Request failed: {(error as Error)?.message || String(error)}</div>;
}

// ---------------------------------------------------------------- range
export const RANGES = ["15m", "1h", "6h", "24h", "7d", "30d"] as const;
export type Range = (typeof RANGES)[number];

export function useRange(def: Range = "6h"): [Range, (r: Range) => void] {
  const [sp, setSp] = useSearchParams();
  const r = (sp.get("range") || sessionStorage.getItem('noc-scope-range') || def) as Range;
  return [RANGES.includes(r) ? r : def, (n: Range) => { const x = new URLSearchParams(sp); x.set("range", n); sessionStorage.setItem('noc-scope-range', n); setSp(x, { replace: true }); }];
}

export function RangePicker({ value, onChange }: { value: Range; onChange: (r: Range) => void }) {
  return (
    <div className="seg" role="group" aria-label="time range">
      {RANGES.map((r) => (
        <button key={r} className={r === value ? "on" : ""} onClick={() => onChange(r)}>{r}</button>
      ))}
    </div>
  );
}

export function Seg<T extends string>({ value, options, onChange }: { value: T; options: { v: T; l: string }[]; onChange: (v: T) => void }) {
  return (
    <div className="seg">
      {options.map((o) => (
        <button key={o.v} className={o.v === value ? "on" : ""} onClick={() => onChange(o.v)}>{o.l}</button>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------- table
export type Col<T> = { key: string; label: ReactNode; render?: (r: T) => ReactNode; sort?: (r: T) => number | string | null | undefined; num?: boolean; width?: number | string };

export function Table<T>({ rows, cols, onRow, rowKey, empty, initialSort, selected, maxHeight }: {
  rows: T[]; cols: Col<T>[]; onRow?: (r: T) => void; rowKey: (r: T) => string; empty?: ReactNode; initialSort?: { key: string; dir: 1 | -1 };
  selected?: string | null; maxHeight?: number;
}) {
  const [sort, setSort] = useState(initialSort || null);
  const sorted = useMemo(() => {
    if (!sort) return rows;
    const col = cols.find((c) => c.key === sort.key);
    if (!col) return rows;
    const get = col.sort || ((r: T) => (r as any)[col.key]);
    return [...rows].sort((a, b) => {
      const x = get(a), y = get(b);
      if (x === y) return 0;
      if (x === null || x === undefined) return 1;
      if (y === null || y === undefined) return -1;
      return (x > y ? 1 : -1) * sort.dir;
    });
  }, [rows, sort, cols]);
  if (!rows.length) return <Empty>{empty || "Nothing to show"}</Empty>;
  return (
    <div className="table-wrap" style={maxHeight ? { maxHeight } : undefined}>
      <table className="t">
        <thead>
          <tr>
            {cols.map((c) => (
              <th key={c.key} className={c.num ? "num" : ""} style={c.width ? { width: c.width } : undefined}
                onClick={() => setSort((s) => (s?.key === c.key ? { key: c.key, dir: (s.dir * -1) as 1 | -1 } : { key: c.key, dir: c.num ? -1 : 1 }))}>
                {c.label}{sort?.key === c.key ? (sort.dir === 1 ? " ▲" : " ▼") : ""}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {sorted.map((r) => (
            <tr key={rowKey(r)} className={`${onRow ? "click" : ""} ${selected === rowKey(r) ? "sel" : ""}`} onClick={onRow ? () => onRow(r) : undefined}>
              {cols.map((c) => (
                <td key={c.key} className={c.num ? "num" : ""}>{c.render ? c.render(r) : (r as any)[c.key] ?? "—"}</td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// ---------------------------------------------------------------- drawer
export function Drawer({ onClose, children }: { onClose: () => void; children: ReactNode }) {
  return (
    <>
      <div className="drawer-bg" onClick={onClose} />
      <aside className="drawer" role="dialog">
        <div className="row"><span className="spacer" style={{ flex: 1 }} /><button onClick={onClose}>Close</button></div>
        {children}
      </aside>
    </>
  );
}

export function EventDetail({ e }: { e: EventRow }) {
  const params = e.metadata?.parameters || {};
  return (
    <div className="col">
      <div className="row"><Sev s={e.severity} /><span className="tag">{e.source}</span><span className="tag">{e.event_type}</span></div>
      <h2>{e.title || e.event_type}</h2>
      <div>{e.message}</div>
      <div className="kv">
        <span>Time</span><span>{fmtTime(e.ts, true)}</span>
        <span>Site</span><span>{e.site_name || e.site}</span>
        {e.device && (<><span>Device</span><span>{e.device} {e.device_type ? `(${e.device_type})` : ""}</span></>)}
        {e.client && (<><span>Client</span><span>{e.client} <span className="mono muted">{e.client_mac}</span></span></>)}
        {e.category && (<><span>Category</span><span>{e.category}</span></>)}
      </div>
      {Object.keys(params).length > 0 && (
        <>
          <h3>Parameters</h3>
          <div className="kv">
            {Object.entries(params).map(([k, v]: [string, any]) => (
              <FragmentKV key={k} k={k} v={typeof v === "object" && v !== null ? v.name ?? v.id ?? JSON.stringify(v) : String(v)} />
            ))}
          </div>
        </>
      )}
      <details><summary>Raw metadata</summary><pre className="json">{JSON.stringify(e.metadata, null, 2)}</pre></details>
    </div>
  );
}

function FragmentKV({ k, v }: { k: string; v: string }) {
  return (<><span>{k}</span><span>{v}</span></>);
}

export function Json({ data }: { data: unknown }) {
  return <pre className="json">{JSON.stringify(data, null, 2)}</pre>;
}

import { ReactNode, useEffect, useRef, useState } from "react";
import { NavLink, useNavigate, useLocation, useSearchParams } from "react-router-dom";
import { Menu, X, Search, Sun, Moon, LogOut, Monitor } from "lucide-react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { api, User } from "../api";
import { fmtTime } from "../format";
import { useTheme } from "../theme";
import { Brand, Pill, tone } from "./ui";

export function Layout({ user, children }: { user: User; children: ReactNode }) {
  const { theme, toggle } = useTheme();
  const qc = useQueryClient();
  const nav = useNavigate();
  const location = useLocation();
  const [params, setParams] = useSearchParams();
  const [menuOpen, setMenuOpen] = useState(false);
  const previousPath = useRef('');
  const scoped = ['/wan', '/wireless', '/clients', '/events', '/alerts', '/operations', '/availability'].includes(location.pathname);
  useEffect(() => {
    setMenuOpen(false);
    if (scoped) {
      const next = new URLSearchParams(params);
      if (previousPath.current !== location.pathname) {
        for (const key of ['site', 'range']) if (!next.has(key)) {
          const saved = sessionStorage.getItem(`noc-scope-${key}`);
          if (saved) next.set(key, saved);
        }
      }
      sessionStorage.setItem('noc-scope-site', next.get('site') || 'all');
      if (next.has('range')) sessionStorage.setItem('noc-scope-range', next.get('range')!);
      if (next.toString() !== params.toString()) setParams(next, { replace: true });
    }
    previousPath.current = location.pathname;
  }, [location.pathname, location.search]);
  const sites = useQuery({ queryKey: ["sites"], queryFn: () => api<any[]>("/sites"), staleTime: 300000 });
  const status = useQuery({ queryKey: ["status"], queryFn: () => api<any>("/status"), refetchInterval: 15000 });
  const ov = useQuery({ queryKey: ["overview"], queryFn: () => api<any>("/overview"), refetchInterval: 10000 });
  const st = status.data;
  const unacknowledged = (ov.data?.alerts || []).filter((a: any) => a.kind !== "anomaly" && !a.acked_at && !a.acked_by);
  const firing = unacknowledged.length;
  const crit = unacknowledged.some((a: any) => a.severity === "critical");
  const collectorBad = st && st.collector.state !== "OK";
  const logout = async () => {
    await api("/auth/logout", { method: "POST" }).catch(() => {});
    qc.clear();
    sessionStorage.removeItem('noc-scope-site');
    sessionStorage.removeItem('noc-scope-range');
    nav("/");
    window.location.reload();
  };
  const overall = ov.data?.overall;
  const overallLive = overall?.freshness?.state === "LIVE";
  if (location.pathname === '/wallboard') return <>{children}</>;
  return (
    <div className="app">
      {menuOpen && <button className="nav-backdrop" aria-label="Close navigation" onClick={() => setMenuOpen(false)} />}
      <aside className={`sidebar ${menuOpen ? 'open' : ''}`} id="main-navigation">
        <button className="mobile-nav-close icon-button" title="Close navigation" aria-label="Close navigation" onClick={() => setMenuOpen(false)}><X size={20} /></button>
        <Brand subtitle="Network operations" />
        <nav className="nav" onClick={event => { if ((event.target as HTMLElement).closest('a')) setMenuOpen(false); }}>
          <div className="nav-group">Overview</div>
          <NavLink to="/" end>Network Overview</NavLink>
          <div className="nav-group">Monitored Sites</div>
          {(sites.data || []).map((s) => (
            <NavLink key={s.slug} to={`/site/${s.slug}`}>
              {s.description}
              <SiteDot slug={s.slug} ov={ov.data} />
            </NavLink>
          ))}
          <div className="nav-group">Connectivity</div>
          <NavLink to="/wan">WAN / Internet</NavLink>
          {(ov.data?.tunnels?.length > 0) && <NavLink to="/s2s">Site-to-Site / SD-WAN</NavLink>}
          <NavLink to="/topology">Topology</NavLink>
          <div className="nav-group">Infrastructure</div>
          <NavLink to="/devices/gateway">Gateways</NavLink>
          <NavLink to="/devices/switch">Switches</NavLink>
          <NavLink to="/devices/ap">Access Points</NavLink>
          <NavLink to="/clients">Clients</NavLink>
          <NavLink to="/wireless">Wi-Fi Health</NavLink>
          <div className="nav-group">Operations</div>
          <NavLink to="/operations">Needs Attention</NavLink>
          <NavLink to="/wallboard"><Monitor size={16} />Wallboard</NavLink>
          <NavLink to="/availability">Device Availability</NavLink>
          <NavLink to="/changes">Configuration Changes</NavLink>
          <NavLink to="/events">Events</NavLink>
          <NavLink to="/alerts">Alerts {firing > 0 && <span className={`tag ${crit ? "crit" : "warn"}`} title="Unacknowledged alerts" style={{ marginLeft: "auto" }}>{firing}</span>}</NavLink>
          <div className="nav-group">Settings</div>
          <NavLink to="/account">My Account</NavLink>
          <NavLink to="/maintenance">Maintenance</NavLink>
          {user.role === "admin" && <NavLink to="/users">Users</NavLink>}
          {user.is_owner && <><NavLink to="/connection">UniFi OS Connection</NavLink><NavLink to="/monitoring">Monitoring Sites</NavLink><NavLink to="/notifications">Notifications</NavLink><NavLink to="/diagnostics">Diagnostics</NavLink></>}
        </nav>
      </aside>
      <div className="main">
        <header className="topbar">
          <button className="mobile-nav-toggle icon-button" title="Open navigation" aria-label="Open navigation" aria-expanded={menuOpen} aria-controls="main-navigation" onClick={() => setMenuOpen(!menuOpen)}><Menu size={20} /></button>
          <span className="small muted">Network health</span>
          {overall ? <Pill status={overallLive ? overall.status : "UNKNOWN"}>{overallLive ? overall.health_score ?? "?" : "?"} {overallLive ? overall.status : "DATA STALE"}</Pill> : <Pill status="UNKNOWN" />}
          <span className="small muted">Collector</span>
          {st ? <Pill status={st.collector.state}>{st.collector.state}</Pill> : <Pill status="UNKNOWN" />}
          {st?.stale_sources?.length ? <span className="stale">{st.stale_sources.length} stale source(s)</span> : null}
          <span className="spacer" />
          {scoped && <select aria-label="Monitoring site" value={params.get('site') || 'all'} onChange={event => { const next = new URLSearchParams(params); next.set('site', event.target.value); setParams(next); }}><option value="all">All sites</option>{(sites.data || []).map(site => <option key={site.slug} value={site.slug}>{site.description}</option>)}</select>}
          <form className="global-search" onSubmit={event => { event.preventDefault(); const term = String(new FormData(event.currentTarget).get('q') || '').trim(); if (term) nav(`/search?q=${encodeURIComponent(term)}`); }}>
            <input type="search" name="q" aria-label="Search dashboard" placeholder="Search dashboard" minLength={1} maxLength={100} required />
            <button className="icon-button" title="Search" aria-label="Search"><Search size={18} /></button>
          </form>
          <span className="small muted">{user.username} ({user.role})</span>
          <button className="icon-button" onClick={toggle} title={theme === "dark" ? 'Light mode' : 'Dark mode'} aria-label={theme === "dark" ? 'Light mode' : 'Dark mode'}>{theme === 'dark' ? <Sun size={18} /> : <Moon size={18} />}</button>
          <button className="icon-button" onClick={logout} title="Sign out" aria-label="Sign out"><LogOut size={18} /></button>
        </header>
        <div className="content">
          {collectorBad && (
            <div className="banner crit">
              {st.collector.state === "DATA COLLECTION ERROR" ? "DATA COLLECTION ERROR" : "DATA STALE"}
              <span className="sub">
                Collector heartbeat {st.collector.freshness.state === "LIVE" ? "ok" : `last seen ${fmtTime(st.collector.freshness.last_update, true)}`}.
                {" "}Values shown may not be current.{" "}
                {st.sources.filter((s: any) => s.consecutive_errors >= 3).map((s: any) => `${s.name}: ${s.error}`).join(" · ")}
              </span>
            </div>
          )}
          {children}
        </div>
      </div>
    </div>
  );
}

function SiteDot({ slug, ov }: { slug: string; ov: any }) {
  const s = ov?.sites?.find((x: any) => x.slug === slug);
  if (!s) return null;
  return <span className={`dot ${s.health_freshness?.state !== 'LIVE' ? 'unk' : tone(s.health?.status || 'UNKNOWN')}`} title={s.health_freshness?.state !== 'LIVE' ? 'Data stale' : s.health?.status || 'UNKNOWN'} style={{ marginLeft: "auto" }} />;
}

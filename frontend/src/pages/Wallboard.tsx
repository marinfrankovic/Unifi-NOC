import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import { Maximize, Minimize, ArrowLeft } from 'lucide-react';
import { api, Tunnel } from '../api';
import { fmtBps, fmtNum, fmtTime } from '../format';
import { Brand, ErrorBox, Loading, Pill, Table } from '../components/ui';
import { Sparkline } from '../components/charts';
import { IncidentGroups } from './Operations';

export default function Wallboard() {
  const overview = useQuery({ queryKey: ['overview'], queryFn: () => api<any>('/overview'), refetchInterval: 10000 });
  const operations = useQuery({ queryKey: ['operations', 'all'], queryFn: () => api<any>('/operations'), refetchInterval: 15000 });
  const [clock, setClock] = useState(Date.now());
  const [fullscreen, setFullscreen] = useState(!!document.fullscreenElement);
  const [error, setError] = useState('');
  useEffect(() => {
    const timer = window.setInterval(() => setClock(Date.now()), 1000);
    const changed = () => setFullscreen(!!document.fullscreenElement);
    document.addEventListener('fullscreenchange', changed);
    return () => { window.clearInterval(timer); document.removeEventListener('fullscreenchange', changed); };
  }, []);
  if (overview.isError) return <ErrorBox error={overview.error} />;
  if (!overview.data) return <Loading />;
  const { sites, tunnels, overall } = overview.data;
  const current = overall.freshness?.state === 'LIVE';
  const toggleFullscreen = async () => {
    try { if (document.fullscreenElement) await document.exitFullscreen(); else await document.documentElement.requestFullscreen(); }
    catch { setError('Fullscreen is unavailable in this browser.'); }
  };
  return <main className="wallboard"><header className="wallboard-header"><Brand subtitle="Wallboard" /><Pill status={current ? overall.status : 'UNKNOWN'}>{current ? overall.status : 'DATA STALE'}</Pill><time>{fmtTime(clock, true)}</time><span className="small muted">Collected {fmtTime(overall.freshness?.last_update, true)}</span><div className="row"><Link className="icon-button" title="Exit wallboard" aria-label="Exit wallboard" to="/"><ArrowLeft size={20} /></Link><button className="icon-button" title={fullscreen ? 'Exit fullscreen' : 'Fullscreen'} aria-label={fullscreen ? 'Exit fullscreen' : 'Fullscreen'} onClick={toggleFullscreen}>{fullscreen ? <Minimize size={20} /> : <Maximize size={20} />}</button></div></header>
    {error && <div className="banner warn">{error}</div>}
    <section><h2>Sites</h2><Table rows={sites} rowKey={(site: any) => site.name} cols={[
      { key: 'description', label: 'Site', render: (site: any) => <Link to={`/site/${site.slug}`}>{site.description}</Link> },
      { key: 'health', label: 'Health', render: (site: any) => <Pill status={site.health_freshness?.state !== 'LIVE' ? 'UNKNOWN' : site.health?.status}>{site.health_freshness?.state !== 'LIVE' ? 'DATA STALE' : `${site.health?.health_score ?? '?'} ${site.health?.status || 'UNKNOWN'}`}</Pill> },
      { key: 'coverage', label: 'Checks', render: (site: any) => site.health?.coverage ? `${site.health.coverage.measured}/${site.health.coverage.total}` : 'Unknown' },
      { key: 'wan', label: 'WAN', render: (site: any) => <Pill status={site.wan?.freshness?.state !== 'LIVE' || site.wan?.up == null ? 'UNKNOWN' : site.wan.up ? 'UP' : 'DOWN'} /> },
      { key: 'traffic', label: 'WAN RX / TX', render: (site: any) => `${fmtBps(site.wan?.now?.live_rx_bps ?? site.wan?.now?.rx_bps)} / ${fmtBps(site.wan?.now?.live_tx_bps ?? site.wan?.now?.tx_bps)}` },
      { key: 'latency', label: 'Latency', render: (site: any) => fmtNum(site.wan?.now?.latency_ms, 0, ' ms') },
      { key: 'trend', label: 'RX · 1 h', render: (site: any) => site.wan?.entity_id ? <Sparkline entity={site.wan.entity_id} metric="rx_bps" /> : 'Unavailable' },
    ]} /></section>
    <section><h2>Inter-Site Links</h2><div className="link-matrix"><table><thead><tr><th>Site</th>{sites.map((site: any) => <th key={site.name}>{site.description}</th>)}</tr></thead><tbody>{sites.map((source: any) => <tr key={source.name}><th>{source.description}</th>{sites.map((destination: any) => {
      const links = (tunnels as Tunnel[]).filter(tunnel => tunnel.members.length === 2 && tunnel.members.some(member => member.site === source.name) && tunnel.members.some(member => member.site === destination.name));
      return <td key={destination.name}>{source.name === destination.name ? '—' : links.length ? links.map(link => <Link key={link.id} to={`/s2s?tunnel=${encodeURIComponent(link.id)}`}><Pill status={link.config_freshness.state !== 'LIVE' || link.connected == null ? 'UNKNOWN' : link.connected ? 'CONNECTED' : 'DOWN'} /></Link>) : <span className="muted">No reported pair</span>}</td>;
    })}</tr>)}</tbody></table></div>{(tunnels as Tunnel[]).filter(tunnel => tunnel.members.length !== 2).map(tunnel => <div key={tunnel.id} className="small muted">{tunnel.label}: group connection; pair-specific measurements unavailable</div>)}</section>
    <section><h2>Link Measurements</h2><Table rows={tunnels} rowKey={(tunnel: Tunnel) => tunnel.id} cols={[
      { key: 'label', label: 'Link', render: (tunnel: Tunnel) => <Link to={`/s2s?tunnel=${encodeURIComponent(tunnel.id)}`}>{tunnel.label}</Link> },
      { key: 'probe', label: 'Collector Probe', render: (tunnel: Tunnel) => <Pill status={tunnel.freshness.state === 'LIVE' ? tunnel.live?.probe_state || 'UNAVAILABLE' : 'UNKNOWN'} /> },
      { key: 'coverage', label: 'Measurements', render: (tunnel: Tunnel) => tunnel.live?.coverage ? `${tunnel.live.coverage.measured}/${tunnel.live.coverage.total}` : 'Unavailable' },
      { key: 'traffic', label: 'RX / TX', render: (tunnel: Tunnel) => tunnel.freshness.state === 'LIVE' ? `${fmtBps(tunnel.live?.rx_bps)} / ${fmtBps(tunnel.live?.tx_bps)}` : 'Stale' },
      { key: 'trend', label: 'RX · 1 h', render: (tunnel: Tunnel) => tunnel.members.length === 2 ? <Sparkline entity={`tunnel:${tunnel.id}`} metric="rx_bps" /> : 'Pair attribution unavailable' },
    ]} /></section>
    <section><h2>Active Incidents</h2>{operations.isError ? <ErrorBox error={operations.error} /> : operations.data ? <IncidentGroups incidents={operations.data.incidents || []} /> : <Loading />}</section>
  </main>;
}
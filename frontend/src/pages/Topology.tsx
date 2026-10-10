import { PageTitle } from "../components/ui";
import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { api, qs } from "../api";
import { cssVar, EChart, escapeHtml } from "../components/charts";
import { ErrorBox, Loading } from "../components/ui";
import { useTheme } from "../theme";
import { Maximize, RotateCcw, Minimize } from 'lucide-react';

type N = { id: string; type: string; site: string; label: string; status: string; detail?: string; slug?: string; mac?: string; model?: string };
type E = { source: string; target: string; kind: string; status?: string; label?: string; tunnel?: string };

const STATUS_VAR: Record<string, string> = { healthy: "--ok", warning: "--warn", critical: "--crit", offline: "--off", unknown: "--unk" };
const SHAPE: Record<string, { symbol: string; size: number | number[] }> = {
  internet: { symbol: "circle", size: 30 }, site: { symbol: "roundRect", size: [150, 30] }, gateway: { symbol: "roundRect", size: [44, 30] },
  switch: { symbol: "rect", size: [40, 22] }, ap: { symbol: "diamond", size: 28 }, client: { symbol: "circle", size: 9 }, other: { symbol: "triangle", size: 20 },
};

function TopologyViewport({ graph, onEvents }: { graph: { width: number; height: number; option: any }; onEvents: Record<string, (event: any) => void> }) {
  const ref = useRef<HTMLDivElement>(null);
  const [fit, setFit] = useState(false);
  const [reset, setReset] = useState(0);
  useEffect(() => {
    const element = ref.current;
    if (!element) return;
    const observer = new ResizeObserver(() => { element.scrollLeft = (element.scrollWidth - element.clientWidth) / 2; });
    observer.observe(element);
    return () => observer.disconnect();
  }, [graph.width]);
  return <><div className="row topology-controls"><button className="icon-button" title={fit ? 'Actual size' : 'Fit topology'} aria-label={fit ? 'Actual size' : 'Fit topology'} onClick={() => { setFit(!fit); setReset(reset + 1); }}>{fit ? <Minimize size={18} /> : <Maximize size={18} />}</button><button className="icon-button" title="Reset zoom" aria-label="Reset zoom" onClick={() => setReset(reset + 1)}><RotateCcw size={18} /></button></div><div className="topology-viewport" ref={ref}><div style={{ width: fit ? '100%' : graph.width, margin: '0 auto' }}><EChart key={reset} option={graph.option} height={fit ? Math.min(700, graph.height) : graph.height} onEvents={onEvents} /></div></div></>;
}

export default function Topology() {
  const [site, setSite] = useState("all");
  const [clients, setClients] = useState(false);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');
  const nav = useNavigate();
  const sites = useQuery({ queryKey: ["sites"], queryFn: () => api<any[]>("/sites") });
  const q = useQuery({ queryKey: ["topology", site, clients], queryFn: () => api<{ nodes: N[]; edges: E[] }>(`/topology${qs({ site: site === "all" ? undefined : site, clients })}`), refetchInterval: 15000 });
  const { theme } = useTheme();
  const slugBySite = useMemo(() => Object.fromEntries((sites.data || []).map((s) => [s.name, s.slug])), [sites.data]);

  const graphs = useMemo(() => {
    if (!q.data) return null;
    function graph(nodes: N[], edges: E[]) {
    const query = search.trim().toLowerCase();
    const matches = (node: N) => !!query && `${node.label} ${node.mac || ''} ${node.id}`.toLowerCase().includes(query);
    const children = new Map<string, N[]>();
    nodes.filter(node => node.type === 'client').forEach(node => { const owner = edges.find(edge => edge.source === node.id && edge.kind !== 'tunnel')?.target; if (owner) children.set(owner, [...(children.get(owner) || []), node]); });
    const hidden = new Set<string>();
    for (const [owner, clients] of children) {
      if (expanded.has(owner) || clients.some(matches)) continue;
      clients.forEach(client => hidden.add(client.id));
      const parent = nodes.find(node => node.id === owner)!;
      nodes = [...nodes, { id: `clients:${owner}`, site: parent.site, type: 'client-group', label: `${clients.length} clients`, status: 'unknown', detail: parent.label, mac: owner }];
      edges = [...edges, { source: `clients:${owner}`, target: owner, kind: 'inferred' }];
    }
    nodes = nodes.filter(node => !hidden.has(node.id));
    edges = edges.filter(edge => !hidden.has(edge.source) && !hidden.has(edge.target));
    const parent: Record<string, string> = {};
    edges.forEach((e) => { if (e.kind !== "tunnel" && e.source !== e.target && !parent[e.source]) parent[e.source] = e.target; });
    const depthCache: Record<string, number> = {};
    const depth = (id: string, guard = 0): number => {
      if (depthCache[id] !== undefined) return depthCache[id];
      const p = parent[id];
      const d = !p || guard > 20 ? 0 : depth(p, guard + 1) + 1;
      depthCache[id] = d;
      return d;
    };
    const pos: Record<string, [number, number]> = {};
    let width = 0, height = 320;
    const laneSites = [...new Set(nodes.map(node => node.site))];
    laneSites.forEach(name => {
      const byLevel: Record<number, N[]> = {};
      nodes.filter(node => node.site === name).forEach(node => (byLevel[depth(node.id)] ||= []).push(node));
      const columns = Math.min(5, Math.max(1, ...Object.values(byLevel).map(level => level.length)));
      const laneWidth = Math.max(laneSites.length > 1 ? 420 : 640, columns * 190 + 180);
      let row = 0;
      Object.entries(byLevel).sort(([first], [second]) => Number(first) - Number(second)).forEach(([, list]) => {
        const parentX = (node: N) => pos[parent[node.id]]?.[0] ?? width + laneWidth / 2;
        const order = (node: N) => `${node.type === "client" ? "z" : "a"}:${node.label || node.id}`;
        list.sort((first, second) => parentX(first) - parentX(second) || order(first).localeCompare(order(second)));
        list.forEach((n, i) => {
          const count = Math.min(columns, list.length - Math.floor(i / columns) * columns);
          const x = width + (laneWidth - (count - 1) * 190) / 2 + (i % columns) * 190;
          const y = 90 + (row + Math.floor(i / columns)) * 130;
          pos[n.id] = [x, y];
        });
        row += Math.ceil(list.length / columns);
      });
      height = Math.max(height, (row - 1) * 130 + 180);
      width += laneWidth;
    });
    const col = (st?: string) => cssVar(STATUS_VAR[st || "unknown"] || "--unk");
    const text = cssVar("--text"), muted = cssVar("--muted"), panel = cssVar("--panel2");
    return { width, height, option: {
      animation: false,
      tooltip: { formatter: (p: any) => p.dataType === "node" ? `<b>${escapeHtml(p.data.name)}</b><br/>${escapeHtml(p.data.ntype)}${p.data.model ? " · " + escapeHtml(p.data.model) : ""}<br/>status: ${escapeHtml(p.data.status)}${p.data.detail ? "<br/>" + escapeHtml(p.data.detail) : ""}`
        : p.data.linkLabel ? escapeHtml(p.data.linkLabel) : `${escapeHtml(p.data.kind)} link` },
      series: [{
        type: "graph", layout: "none", roam: true, draggable: false, edgeSymbol: ["none", "none"], scaleLimit: { min: 0.7, max: 4 },
        left: 0, right: 0, top: 0, bottom: 0,
        label: { show: true, position: "bottom", color: text, fontSize: 11, width: 150, overflow: "break", lineHeight: 15 },
        data: [...nodes.map((n) => {
          const sh = SHAPE[n.type] || SHAPE.other;
          const c = col(n.status);
          return {
            id: n.id, name: n.label || n.mac || n.id, x: pos[n.id][0], y: pos[n.id][1], symbol: sh.symbol, symbolSize: n.type === 'client' ? 14 : sh.size, ntype: n.type, status: n.status, detail: n.detail, model: n.model,
            slug: n.slug, mac: n.mac, site: n.site,
            emphasis: { itemStyle: { borderWidth: 3 } },
            itemStyle: n.type === "site" ? { color: panel, borderColor: c, borderWidth: 2, borderType: n.status === "unknown" ? "dashed" : "solid" }
              : { color: n.status === "unknown" ? panel : c, borderColor: matches(n) ? '#fbbf24' : c, borderWidth: matches(n) ? 4 : n.status === "unknown" ? 2 : 1, borderType: n.status === "unknown" ? "dashed" : "solid", opacity: query && !matches(n) ? .4 : 1 },
            label: n.type === "site" ? { position: "inside", fontWeight: 700, color: text, formatter: `${n.label}\n${n.detail || ""}`, lineHeight: 13, width: 140, overflow: "truncate" }
              : n.type === "client" ? { show: true, position: "bottom", fontSize: 10, width: 150, overflow: "truncate", lineHeight: 14 } : n.type === "internet" ? { position: "top", color: muted } : undefined,
          };
        }), ...[{ id: '_layout-start', x: 0, y: 0 }, { id: '_layout-end', x: width, y: height }].map(bound => ({ ...bound, symbolSize: 0, silent: true, label: { show: false }, tooltip: { show: false } }))],
        links: edges.filter((e) => pos[e.source] && pos[e.target]).map((e) => ({
          source: e.source, target: e.target, kind: e.kind, linkLabel: e.label, tunnel: e.tunnel,
          lineStyle: {
            color: e.kind === "tunnel" ? col(e.status) : e.status ? col(e.status) : cssVar("--border"),
            width: e.kind === "tunnel" ? 4 : e.kind === "wan" ? 2.5 : e.kind === "wifi" ? 1 : 1.6,
            type: e.kind === "wifi" || e.kind === "inferred" ? "dashed" : e.kind === "tunnel" ? [10, 6] : "solid",
            curveness: e.kind === "tunnel" ? -0.25 : 0, opacity: e.kind === "wifi" ? 0.5 : 0.9,
          },
          label: e.kind === "tunnel" || (e.label && e.kind === "wired") ? { show: true, formatter: e.label || "", color: e.kind === "tunnel" ? text : muted, fontSize: e.kind === "tunnel" ? 12 : 10, fontWeight: e.kind === "tunnel" ? 700 : 400 } : undefined,
        })),
      }],
    } };
    }
    const { nodes, edges } = q.data;
    const siteNames = [...new Set(nodes.map(node => node.site))];
    const nodeById = new Map(nodes.map(node => [node.id, node]));
    const adjacent = new Map(siteNames.map(name => [name, new Set<string>()]));
    edges.filter(edge => edge.kind === 'tunnel').forEach(edge => {
      const source = nodeById.get(edge.source)?.site, target = nodeById.get(edge.target)?.site;
      if (!source || !target || source === target) return;
      adjacent.get(source)?.add(target);
      adjacent.get(target)?.add(source);
    });
    const visited = new Set<string>();
    const groups: string[][] = [];
    siteNames.forEach(name => {
      if (visited.has(name)) return;
      const members = [name];
      visited.add(name);
      for (let index = 0; index < members.length; index++) adjacent.get(members[index])?.forEach(neighbour => {
        if (!visited.has(neighbour)) { visited.add(neighbour); members.push(neighbour); }
      });
      groups.push(members);
    });
    return groups.map(members => {
      const memberSites = new Set(members), localNodes = nodes.filter(node => memberSites.has(node.site));
      const ids = new Set(localNodes.map(node => node.id));
      const localEdges = edges.filter(edge => ids.has(edge.source) && ids.has(edge.target));
      return { name: members.join(':'), members, label: members.map(name => localNodes.find(node => node.site === name && node.type === 'site')?.label || name).join(' / '),
        devices: localNodes.filter(node => !['site', 'internet', 'client'].includes(node.type)).length,
        connections: localEdges.filter(edge => edge.kind === 'tunnel').length,
        ...graph(localNodes, localEdges) };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.data, theme, expanded, search]);

  const onEvents = useMemo(() => ({
    click: (p: any) => {
      const d = p.data || {};
      if (p.dataType === "edge") { if (d.kind === "tunnel") nav(`/s2s?tunnel=${encodeURIComponent(d.tunnel || '')}`); return; }
      if (d.ntype === 'client-group') { setExpanded(previous => new Set([...previous, d.mac])); return; }
      if (["gateway", "switch", "ap", "other"].includes(d.ntype)) nav(`/device/${d.id}`);
      else if (d.ntype === "client" && d.mac) nav(`/client/${encodeURIComponent(slugBySite[d.site] || d.site)}/${d.mac}`);
      else if (d.ntype === "site") nav(`/site/${d.slug}`);
      else if (d.ntype === "internet") nav("/wan");
    },
  }), [nav, slugBySite]);

  if (q.isError) return <ErrorBox error={q.error} />;
  return (
    <>
      <div className="page-head">
        <div><PageTitle>Topology</PageTitle></div>
        <div className="right">
          <input type="search" aria-label="Find topology node" placeholder="Find node" value={search} onChange={event => setSearch(event.target.value)} />
          <label className="row small">Site<select value={site} onChange={event => setSite(event.target.value)}><option value="all">All sites</option>{(sites.data || []).map(item => <option key={item.slug} value={item.slug}>{item.description}</option>)}</select></label>
          <label className="row small"><input type="checkbox" checked={clients} onChange={(e) => setClients(e.target.checked)} />Show clients</label>
          {clients && <><button onClick={() => setExpanded(new Set((q.data?.edges || []).filter(edge => q.data?.nodes.some(node => node.id === edge.source && node.type === 'client')).map(edge => edge.target)))}>Expand all</button><button onClick={() => setExpanded(new Set())}>Collapse clients</button></>}
          {clients && <select aria-label="Client groups" value="" onChange={event => { const owner = event.target.value; setExpanded(previous => { const next = new Set(previous); if (next.has(owner)) next.delete(owner); else next.add(owner); return next; }); }}><option value="">Client groups</option>{[...new Set((q.data?.edges || []).filter(edge => q.data?.nodes.some(node => node.id === edge.source && node.type === 'client')).map(edge => edge.target))].map(owner => <option key={owner} value={owner}>{expanded.has(owner) ? 'Collapse' : 'Expand'} {q.data?.nodes.find(node => node.id === owner)?.label || owner}</option>)}</select>}
        </div>
      </div>
      <div className="row small topology-legend">{Object.entries(STATUS_VAR).map(([key, value]) => <span key={key} className="row" style={{ gap: 4 }}><span className="dot" style={{ background: `var(${value})` }} />{key}</span>)}<span className="muted">Dashed: Wi-Fi / inferred uplink · thick dashed: SD-WAN</span></div>
      {!graphs ? <Loading /> : <>
        {graphs.map(group => <section className="topology-section topology-site" key={group.name} data-site={group.members.join(',')} aria-label={`Topology for ${group.label}`}><div className="topology-heading"><h2>{group.label}</h2><span className="small muted">{group.devices} {group.devices === 1 ? 'device' : 'devices'}{group.connections > 0 && ` · ${group.connections} SD-WAN ${group.connections === 1 ? 'link' : 'links'}`}</span></div>
          <TopologyViewport graph={group} onEvents={onEvents} />
        </section>)}
        {!graphs.length && <div className="empty">No monitored topology is available</div>}
      </>}
    </>
  );
}

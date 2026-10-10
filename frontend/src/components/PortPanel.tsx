import { fmtNum } from "../format";

export function PortPanel({ ports, now, mac, selected, onSelect, live }: {
  ports: any[]; now: Record<string, any>; mac: string; selected: number; onSelect: (port: number) => void; live: boolean;
}) {
  return <div className="port-panel">{ports.map(port => {
    const values = now[`port:${mac}:${port.port_idx}`] || {};
    const state = !live ? 'unk' : !port.up ? 'off' : (values.err_per_min || 0) > 0 || (values.flaps_15m || 0) >= 3 ? 'warn' : 'ok';
    return <button key={port.port_idx} className={`port-tile ${selected === port.port_idx ? 'selected' : ''}`}
      onClick={() => onSelect(port.port_idx)} aria-label={`Select port ${port.port_idx}`} aria-pressed={selected === port.port_idx}
      title={`${port.name || `Port ${port.port_idx}`} · ${live ? port.up ? `${port.speed} Mbps` : 'Down' : 'Data stale'}`}>
      <span className={`dot ${state}`} /><b>{port.port_idx}</b><span className="small">{live && port.up ? port.speed >= 1000 ? `${port.speed / 1000}G` : `${port.speed}M` : '—'}</span>
      {port.is_uplink && <span className="small">uplink</span>}{port.port_poe && port.poe_enable && <span className="small">{fmtNum(values.poe_w, 1)} W</span>}
    </button>;
  })}</div>;
}
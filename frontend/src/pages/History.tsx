import { PageTitle } from "../components/ui";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Link, useSearchParams } from "react-router-dom";
import { api, qs } from "../api";
import { fmtNum, fmtTime, fmtDuration } from "../format";
import { Card, ErrorBox, Loading, Table } from "../components/ui";

type ChangeRecord = { id: number; ts: string; site_name: string; device_name: string; device_mac: string; before: Record<string, unknown>; after: Record<string, unknown> };
type ChangeField = { group: string; label: string; value: unknown };
const fieldLabels: Record<string, string> = { firmware: 'Firmware version', version: 'Firmware version', uplink_mac: 'Connected device', uplink_remote_port: 'Remote port',
  speed: 'Link speed', wan_ip: 'WAN address', channel: 'Channel', width: 'Channel width', portconf_id: 'Port profile', forward: 'Forwarding mode' };
const bandLabels: Record<string, string> = { ng: '2.4 GHz radio', na: '5 GHz radio', '6e': '6 GHz radio' };

function snapshotFields(snapshot: Record<string, unknown>) {
  const fields = new Map<string, ChangeField>();
  function visit(value: unknown, key: string, group: string, label: string) {
    if (Array.isArray(value)) {
      value.forEach((item, index) => {
        const identity = item && typeof item === 'object' ? (item.band ?? item.port_idx ?? index) : index;
        const name = key === 'radios' ? bandLabels[String(identity)] || `${identity} radio` : key === 'ports' ? `Port ${identity}` : `${label} ${index + 1}`;
        const category = key === 'radios' ? 'Wi-Fi' : key === 'ports' ? 'Ports' : group;
        visit(item, `${key}.${identity}`, category, name);
      });
    } else if (value !== null && typeof value === 'object') {
      Object.entries(value).forEach(([property, item]) => {
        if ((key.startsWith('radios.') && property === 'band') || (key.startsWith('ports.') && property === 'port_idx')) return;
        const name = fieldLabels[property] || property.replaceAll('_', ' ');
        visit(item, `${key}.${property}`, group, `${label} / ${name}`);
      });
    } else {
      fields.set(key, { group, label, value });
    }
  }
  Object.entries(snapshot || {}).forEach(([key, value]) => {
    const group = key === 'firmware' || key === 'version' ? 'Firmware' : key === 'uplink' ? 'Uplink' : 'Other';
    visit(value, key, group, fieldLabels[key] || key.replaceAll('_', ' '));
  });
  return fields;
}

function changeFields(record: ChangeRecord) {
  const before = snapshotFields(record.before), after = snapshotFields(record.after);
  return [...new Set([...before.keys(), ...after.keys()])].filter(key => !Object.is(before.get(key)?.value, after.get(key)?.value))
    .map(key => ({ key, ...(after.get(key) || before.get(key))!, before: before.get(key)?.value, after: after.get(key)?.value }));
}

function changeValue(value: unknown, key: string) {
  if (value === undefined) return 'Not present';
  if (value === null || value === '') return 'Not reported';
  if (typeof value === 'boolean') return value ? 'Yes' : 'No';
  if (typeof value === 'number' && key.endsWith('.width')) return `${value} MHz`;
  if (typeof value === 'number' && key.endsWith('.speed')) return value >= 1000 ? `${value / 1000} Gbps` : `${value} Mbps`;
  return String(value);
}

function ConfigurationHistory({ records }: { records: ChangeRecord[] }) {
  const [term, setTerm] = useState(''), [site, setSite] = useState(''), [category, setCategory] = useState('');
  const [expanded, setExpanded] = useState<Record<number, boolean>>({});
  const entries = records.map(record => ({ record, fields: changeFields(record) }));
  const categories = [...new Set(entries.flatMap(entry => entry.fields.map(field => field.group)))].sort();
  const filtered = entries.filter(({ record, fields }) => (!site || record.site_name === site)
    && (!category || fields.some(field => field.group === category))
    && (!term.trim() || [record.device_name, record.device_mac, record.site_name, ...fields.flatMap(field => [field.label, changeValue(field.before, field.key), changeValue(field.after, field.key)])]
      .join(' ').toLowerCase().includes(term.trim().toLowerCase())));
  return <>
    <div className="change-toolbar">
      <label>Device or changed value<input type="search" placeholder="Filter changes" value={term} onChange={event => setTerm(event.target.value)} /></label>
      <label>Site<select value={site} onChange={event => setSite(event.target.value)}><option value="">All sites</option>{[...new Set(records.map(record => record.site_name))].sort().map(name => <option key={name}>{name}</option>)}</select></label>
      <label>Category<select value={category} onChange={event => setCategory(event.target.value)}><option value="">All categories</option>{categories.map(name => <option key={name}>{name}</option>)}</select></label>
    </div>
    <div className="change-count" role="status">{filtered.length} {filtered.length === 1 ? 'change record' : 'change records'}</div>
    {filtered.length === 0 && <div className="empty">{records.length ? 'No changes match these filters' : 'No configuration changes recorded'}</div>}
    <div className="change-feed">{filtered.map(({ record, fields }, index) => {
      const visibleFields = category ? fields.filter(field => field.group === category) : fields;
      return <details className="change-record" key={record.id} open={expanded[record.id] ?? index === 0} onToggle={event => {
        const open = event.currentTarget.open;
        setExpanded(previous => previous[record.id] === open ? previous : { ...previous, [record.id]: open });
      }}>
        <summary><div className="change-record-header"><div className="change-device"><strong>{record.device_name || record.device_mac}</strong><span className="small muted">{record.site_name}</span></div>
          <div className="change-record-kind"><span>{[...new Set(fields.map(field => field.group))].join(' / ') || 'Configuration'}</span><span className="small muted">{fields.length} {fields.length === 1 ? 'field changed' : 'fields changed'}</span></div>
          <time dateTime={record.ts}>{fmtTime(record.ts, true)}</time></div></summary>
        <div className="change-comparison"><div className="change-grid-head"><span>Changed field</span><span>Previous</span><span>Current</span></div>
          {visibleFields.map(field => <div className="change-field" key={field.key}><div className="change-field-name"><span className="small muted">{field.group}</span><strong>{field.label}</strong></div>
            <div className="change-before"><span className="change-value-label">Previous</span><span>{changeValue(field.before, field.key)}</span></div>
            <div className="change-after"><span className="change-value-label">Current</span><span>{changeValue(field.after, field.key)}</span></div></div>)}
          {!fields.length && <div className="empty">No field-level difference recorded</div>}
        </div><div className="change-record-footer"><Link to={`/device/${record.device_mac}`}>View device</Link><span className="small muted">{record.device_mac}</span></div>
      </details>;
    })}</div>
  </>;
}

export default function History({ view }: { view: 'availability' | 'changes' }) {
  const [params] = useSearchParams();
  const site = params.get('site') || 'all';
  const query=useQuery({queryKey:[view, site],queryFn:()=>api<any[]>(view === 'availability' ? `/reliability${qs({ site: site === 'all' ? undefined : site })}` : '/changes'),refetchInterval:30000});
  if(query.isError)return <ErrorBox error={query.error}/>;
  if(!query.data)return <Loading/>;
  return <><div className="page-head"><PageTitle>{view === 'availability' ? 'Device Availability' : 'Configuration Changes'}</PageTitle></div>
    {view === 'availability' ? <Card title="Last 24 hours"><Table rows={query.data} rowKey={(row:any)=>row.entity_id} cols={[{key:'site_name',label:'Site'}, {key:'name',label:'Device',render:(row:any)=><Link to={`/device/${row.entity_id.replace('dev:', '')}`}>{row.name}</Link>},
      {key:'observed_online_pct',label:'Online samples',render:(row:any)=>fmtNum(row.observed_online_pct,2,'%')}, {key:'coverage_pct',label:'24 h collection coverage',render:(row:any)=>fmtNum(row.coverage_pct,1,'%')},
      {key:'offline_events_30d',label:'Offline events (30 d)'}, {key:'first_sample',label:'First sample',render:(row:any)=>fmtTime(row.first_sample,true)}, {key:'last_sample',label:'Last sample',render:(row:any)=>fmtTime(row.last_sample,true)}]}/>
      <div className="small muted">Last 24 hours; the shared time range does not apply. Online percentage is among observed samples, not guaranteed end-to-end uptime. Collection gaps and time before installation are not counted as successful monitoring.</div></Card> :
    <ConfigurationHistory records={query.data} />}
    {view === 'availability' && query.data.map((device: any) => <details className="availability-device" key={device.entity_id}><summary><strong>{device.name}</strong><span>{device.site_name}</span><span className="small muted">{fmtDuration((device.intervals || []).filter((item: any) => item.state === 'OFFLINE').reduce((sum: number, item: any) => sum + item.duration_s, 0))} observed offline · {fmtDuration((device.intervals || []).filter((item: any) => item.state === 'UNKNOWN').reduce((sum: number, item: any) => sum + item.duration_s, 0))} unknown</span></summary>
      <div className="availability-timeline" aria-label={`24 hour availability for ${device.name}`}>{(device.intervals || []).map((item: any, index: number) => <span key={index} tabIndex={0} className={`availability-period ${item.state.toLowerCase()}`} style={{ flexBasis: `${item.duration_s / 864}%` }} title={`${item.state}: ${fmtTime(item.start, true)} to ${fmtTime(item.end, true)} (${fmtDuration(item.duration_s)})`} aria-label={`${item.state} for ${fmtDuration(item.duration_s)}`} />)}{(device.maintenance || []).map((item: any, index: number) => <span key={`maintenance:${index}`} className="maintenance-overlay" tabIndex={0} title={`Maintenance: ${item.title}`} aria-label={`Maintenance: ${item.title}`} style={{ left: `${(item.start - (device.intervals?.[0]?.start || item.start)) / 864000}%`, width: `${(item.end - item.start) / 864000}%` }} />)}</div>
      {!!device.maintenance?.length && <div className="small muted">Maintenance: {device.maintenance.map((item: any) => `${item.title} (${fmtTime(item.start, true)} - ${fmtTime(item.end, true)})`).join(' · ')}</div>}
      <div className="availability-legend small"><span className="dot ok" />Online <span className="dot crit" />Offline <span className="dot unk" />Unknown</div>
      <Table rows={(device.intervals || []).filter((item: any) => item.state !== 'ONLINE')} rowKey={(item: any) => String(item.start)} cols={[{ key: 'state', label: 'Observation' }, { key: 'start', label: 'From', render: (item: any) => fmtTime(item.start, true) }, { key: 'end', label: 'To', render: (item: any) => fmtTime(item.end, true) }, { key: 'duration_s', label: 'Duration', render: (item: any) => fmtDuration(item.duration_s) }, { key: 'maintenance', label: 'Maintenance overlap', render: (item: any) => (device.maintenance || []).filter((window: any) => window.start < item.end && window.end > item.start).map((window: any) => window.title).join(', ') || 'None' }]} />
    </details>)}
  </>;
}
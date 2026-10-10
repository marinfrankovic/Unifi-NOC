import { PageTitle } from "../components/ui";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, Link, useSearchParams } from "react-router-dom";
import { api, qs } from "../api";
import { fmtNum, fmtTime } from "../format";
import { Card, ErrorBox, Loading, Sev, Table } from "../components/ui";

export function IncidentGroups({ incidents }: { incidents: any[] }) {
  return <div className="incident-groups">{incidents.map(incident => <details key={incident.id} className="incident-group"><summary><Sev s={incident.primary.severity} /><strong>{incident.primary.site_name || incident.primary.site}: {incident.primary.message}</strong><span className="small muted">{incident.related.length} related</span></summary><div className="col"><span className="small muted">{incident.basis}</span><Link to={incident.path || '/alerts'}>Investigate primary</Link>{incident.related.map((alert: any) => <div key={alert.id} className="row"><Sev s={alert.severity} /><Link to="/alerts">{alert.message}</Link><span className="small muted">{fmtTime(alert.started_at, true)}</span></div>)}</div></details>)}{!incidents.length && <div className="empty">No current alert incidents</div>}</div>;
}

export default function Operations() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const site = params.get('site') || 'all';
  const query = useQuery({ queryKey: ['operations', site], queryFn: () => api<any>(`/operations${qs({ site: site === 'all' ? undefined : site })}`), refetchInterval: 15000 });
  if (query.isError) return <ErrorBox error={query.error} />;
  if (!query.data) return <Loading />;
  const attention = query.data.attention.filter((item: any) => !query.data.incidents || !item.id.startsWith('alert:'));
  return <><div className="page-head"><PageTitle>Needs Attention</PageTitle></div>
    {query.data.incidents && <Card title="Correlated incidents"><IncidentGroups incidents={query.data.incidents} /></Card>}
    <Card title="Needs attention">{attention.length ? attention.map((item: any) => <div key={item.id} className="incident">
      <Sev s={item.severity} /><div style={{ flex: 1 }}><b>{item.site}</b><div>{item.title}</div>{item.since && <span className="small muted">Since {fmtTime(item.since, true)}</span>}</div>
      <button onClick={() => navigate(item.path)}>Investigate</button></div>) : <div className="empty">No additional collection or Wi-Fi issues reported.</div>}</Card>
    <div className="grid g2"><Card title="Resource pressure"><Table rows={query.data.capacity} rowKey={(item: any) => item.mac} onRow={(item: any) => navigate(`/device/${item.mac}`)}
      cols={[{key:'name',label:'Device'}, {key:'site',label:'Site'}, {key:'cpu',label:'CPU',render:(item:any)=>fmtNum(item.cpu,0,'%')}, {key:'memory',label:'Memory',render:(item:any)=>fmtNum(item.memory,0,'%')}, {key:'freshness',label:'Data',render:(item:any)=>item.freshness.state}]} /></Card>
      <Card title="Firmware available"><Table rows={query.data.updates} rowKey={(item: any) => item.mac} onRow={(item: any)=>navigate(`/device/${item.mac}`)} empty="No reported updates"
        cols={[{key:'name',label:'Device'}, {key:'current',label:'Running'}, {key:'available',label:'Available'}]} /></Card></div>
  </>;
}
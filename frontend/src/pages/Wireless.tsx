import { PageTitle } from "../components/ui";
import { useQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, qs } from "../api";
import { BAND, fmtNum, fmtTime } from "../format";
import { Card, ErrorBox, Loading, Pill, Table } from "../components/ui";

export default function Wireless() {
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const site = params.get('site') || 'all';
  const query = useQuery({ queryKey: ['wireless-health', site], queryFn: () => api<any>(`/wireless${qs({ site: site === 'all' ? undefined : site })}`), refetchInterval: 15000 });
  if (query.isError) return <ErrorBox error={query.error} />;
  if (!query.data) return <Loading />;
  return <><div className="page-head"><PageTitle>Wi-Fi Health</PageTitle><span className="small muted">Current measurements; time range does not apply</span></div>
    <Card title="Radios"><Table rows={query.data.radios} rowKey={(item:any)=>`${item.mac}:${item.band}`} onRow={(item:any)=>navigate(`/device/${item.mac}`)}
      cols={[{key:'site_name',label:'Site'}, {key:'device',label:'AP'}, {key:'band',label:'Band',render:(item:any)=>BAND[item.band] || item.band}, {key:'channel',label:'Channel'},
        {key:'width',label:'Width',render:(item:any)=>fmtNum(item.width,0,' MHz')}, {key:'clients',label:'Clients'}, {key:'utilization',label:'Airtime',render:(item:any)=>fmtNum(item.utilization,0,'%')},
        {key:'retries',label:'Retries',render:(item:any)=>fmtNum(item.retries,1,'%')}, {key:'cochannel_radios',label:'Other APs on channel'},
        {key:'freshness',label:'Data',render:(item:any)=><Pill status={item.freshness}>{item.freshness==='LIVE'?'Current':'DATA STALE'}</Pill>}, {key:'last_poll',label:'Last update',render:(item:any)=>fmtTime(item.last_poll)}]} /></Card>
    <div className="small muted">Shared channels are a potential contention risk within the same site, not proof of interference. Sites in different locations are not compared.</div>
    <Card title="Current client signal"><Table rows={query.data.signal} rowKey={(item:any)=>item.site}
      cols={[{key:'site_name',label:'Site'}, {key:'poor',label:'Below −75 dBm'}, {key:'fair',label:'−75 to −60 dBm'}, {key:'good',label:'At least −60 dBm'}]} /></Card>
  </>;
}
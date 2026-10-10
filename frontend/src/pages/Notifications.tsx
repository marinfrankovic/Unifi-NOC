import { PageTitle } from "../components/ui";
import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";
import { fmtTime } from "../format";
import { Card, ErrorBox, Loading, Pill, Table } from "../components/ui";

export default function Notifications() {
  const client = useQueryClient();
  const query = useQuery({ queryKey: ['notification-settings'], queryFn: () => api<any>('/notifications') });
  const sites = useQuery({ queryKey: ['sites'], queryFn: () => api<any[]>('/sites') });
  const history = useQuery({ queryKey: ['notification-history'], queryFn: () => api<any[]>('/notifications/history'), refetchInterval: 5000 });
  const [form, setForm] = useState<any>({ enabled: false, provider: 'ntfy', url: 'https://ntfy.sh', topic: '', token: '', chat_id: '', severity: 'warning', sites: [] });
  const [saved, setSaved] = useState(false);
  useEffect(() => { if (query.data) setForm((previous:any) => ({...previous, ...query.data, token: ''})); }, [query.data]);
  const mutation = useMutation({ mutationFn: ({path, body}:{path:string;body?:any}) => api(path,{method:body?'PUT':'POST', body:body?JSON.stringify(body):undefined}),
    onSuccess:()=>{setSaved(true); client.invalidateQueries({queryKey:['notification-settings']});client.invalidateQueries({queryKey:['notification-history']});} });
  if (query.isError) return <ErrorBox error={query.error} />;
  if (!query.data) return <Loading />;
  const field=(key:string,value:any)=>{setForm((old:any)=>({...old,[key]:value}));setSaved(false);};
  return <><div className="page-head"><PageTitle>Notifications</PageTitle></div>
    {mutation.isError && <ErrorBox error={mutation.error}/>} {saved && <span role="status">Saved or queued</span>}
    <Card title="Delivery channel"><form className="col settings-form wide-form" onSubmit={event=>{event.preventDefault();mutation.mutate({path:'/notifications',body:form});}}>
      <label className="row"><input type="checkbox" checked={form.enabled} onChange={event=>field('enabled',event.target.checked)}/>Enable notifications</label>
      <div className="form-fields"><label>Provider <select value={form.provider} onChange={event=>field('provider',event.target.value)}><option value="ntfy">ntfy</option><option value="telegram">Telegram</option></select></label>
      <label>Severity <select value={form.severity} onChange={event=>field('severity',event.target.value)}><option value="warning">Warning and critical</option><option value="critical">Critical only</option></select></label></div>
      {form.provider==='ntfy'?<div className="form-fields"><label>HTTPS server <input type="url" value={form.url} onChange={event=>field('url',event.target.value)}/></label>
        <label>Topic <input value={form.topic} onChange={event=>field('topic',event.target.value)}/></label></div>:<label>Chat ID <input value={form.chat_id} onChange={event=>field('chat_id',event.target.value)}/></label>}
      <label>Access token <input type="password" autoComplete="off" value={form.token} placeholder={form.token_configured?'Configured; leave blank to keep':'Optional for ntfy; required for Telegram'} onChange={event=>field('token',event.target.value)}/></label>
      <fieldset><legend>Sites</legend>{(sites.data || []).map(site=><label key={site.id} className="row"><input type="checkbox" checked={form.sites?.includes(site.id)} onChange={event=>field('sites',event.target.checked?[...(form.sites||[]),site.id]:(form.sites||[]).filter((id:string)=>id!==site.id))}/>{site.description}</label>)}</fieldset>
      <div className="small muted">No site selections means all monitored sites. Maintenance holds delivery; collection continues.</div>
      <div className="row"><button className="primary" disabled={mutation.isPending}>Save channel</button><button type="button" disabled={mutation.isPending} onClick={()=>mutation.mutate({path:'/notifications/test'})}>Send test</button></div>
      <details><summary>Delivery and self-monitoring</summary><p>Secrets remain server-side. Failed delivery is retried with increasing delays and recorded below. A stopped collector or Docker host cannot send its own outage notification; use an independent monitor for host availability.</p></details>
    </form></Card>
    <Card title="Delivery history"><Table rows={history.data || []} rowKey={(row:any)=>String(row.id)} cols={[{key:'created_at',label:'Queued',render:(row:any)=>fmtTime(row.created_at,true)},
      {key:'site',label:'Site'}, {key:'status',label:'Status',render:(row:any)=><Pill status={row.status==='sent'?'OK':row.status==='failed'?'ERROR':'INFO'}>{row.status}</Pill>}, {key:'attempts',label:'Attempts'}, {key:'last_error',label:'Last result'}]}/></Card>
  </>;
}
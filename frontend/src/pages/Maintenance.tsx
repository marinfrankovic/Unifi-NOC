import { PageTitle } from "../components/ui";
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, User } from "../api";
import { fmtTime } from "../format";
import { Card, ErrorBox, Pill, Table } from "../components/ui";

export default function Maintenance({user}:{user:User}) {
  const client=useQueryClient();
  const sites=useQuery({queryKey:['sites'],queryFn:()=>api<any[]>('/sites')});
  const windows=useQuery({queryKey:['maintenance'],queryFn:()=>api<any[]>('/maintenance'),refetchInterval:10000});
  const [site,setSite]=useState(''),[title,setTitle]=useState(''),[start,setStart]=useState(''),[end,setEnd]=useState(''),[repeat,setRepeat]=useState('none');
  const manageable=(sites.data||[]).filter(item=>user.is_owner||user.site_roles[item.id]==='admin');
  const mutate=useMutation({mutationFn:({path,body}:{path:string;body?:any})=>api(path,{method:body?'POST':'DELETE',body:body?JSON.stringify(body):undefined}),onSuccess:()=>client.invalidateQueries({queryKey:['maintenance']})});
  return <><div className="page-head"><PageTitle>Maintenance</PageTitle></div>{(mutate.isError||windows.isError)&&<ErrorBox error={mutate.error||windows.error}/>}
    {manageable.length>0&&<Card title="Schedule maintenance"><form className="col settings-form wide-form" onSubmit={event=>{event.preventDefault();mutate.mutate({path:'/maintenance',body:{site_id:site,title,starts_at:new Date(start).toISOString(),ends_at:new Date(end).toISOString(),repeat,timezone:Intl.DateTimeFormat().resolvedOptions().timeZone}});}}>
      <div className="form-fields"><label>Site <select required value={site} onChange={event=>setSite(event.target.value)}><option value="">Select site</option>{manageable.map(item=><option key={item.id} value={item.id}>{item.description}</option>)}</select></label>
      <label>Reason <input required value={title} onChange={event=>setTitle(event.target.value)}/></label></div>
      <div className="form-fields"><label>Start <input type="datetime-local" required value={start} onChange={event=>setStart(event.target.value)}/></label><label>End <input type="datetime-local" required value={end} onChange={event=>setEnd(event.target.value)}/></label>
      <label>Repeat <select value={repeat} onChange={event=>setRepeat(event.target.value)}><option value="none">Once</option><option value="daily">Daily</option><option value="weekly">Weekly</option></select></label></div>
      <button className="primary" disabled={mutate.isPending}>Schedule</button>
    </form></Card>}
    <Card title="Windows"><Table rows={windows.data||[]} rowKey={(row:any)=>String(row.id)} cols={[{key:'site_name',label:'Site'}, {key:'title',label:'Reason'},
      {key:'starts_at',label:'Start',render:(row:any)=>fmtTime(row.starts_at,true)}, {key:'ends_at',label:'End',render:(row:any)=>fmtTime(row.ends_at,true)}, {key:'repeat',label:'Repeat'},
      {key:'active',label:'State',render:(row:any)=><Pill status={row.active?'WARNING':'INFO'}>{row.active?'Active':'Scheduled'}</Pill>},
      {key:'remove',label:'',render:(row:any)=>(user.is_owner||user.site_roles[row.site_id]==='admin')?<button disabled={mutate.isPending} onClick={()=>mutate.mutate({path:`/maintenance/${row.id}`})}>Delete</button>:null}]}/></Card>
  </>;
}
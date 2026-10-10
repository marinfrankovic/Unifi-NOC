import { PageTitle } from "../components/ui";
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { api } from "../api";
import { fmtTime } from "../format";
import { Card, ErrorBox, Table } from "../components/ui";

export default function Account() {
  const sessions=useQuery({queryKey:['my-sessions'],queryFn:()=>api<any[]>('/account/sessions')});
  const [current,setCurrent]=useState(''),[password,setPassword]=useState(''),[confirmation,setConfirmation]=useState('');
  const mutation=useMutation({mutationFn:({path,body}:{path:string;body?:any})=>api(path,{method:'POST',body:body?JSON.stringify(body):undefined}),onSuccess:()=>window.location.reload()});
  return <><div className="page-head"><PageTitle>My Account</PageTitle></div>{(mutation.isError||sessions.isError)&&<ErrorBox error={mutation.error||sessions.error}/>}
    <Card title="Change password"><form className="col settings-form" onSubmit={event=>{event.preventDefault();if(password===confirmation)mutation.mutate({path:'/account/password',body:{current_password:current,new_password:password}});}}>
      <label>Current password <input type="password" autoComplete="current-password" required value={current} onChange={event=>setCurrent(event.target.value)}/></label>
      <label>New password <input type="password" autoComplete="new-password" required minLength={12} value={password} onChange={event=>setPassword(event.target.value)}/></label>
      <label>Confirm password <input type="password" autoComplete="new-password" required value={confirmation} onChange={event=>setConfirmation(event.target.value)}/></label>
      <button className="primary" disabled={mutation.isPending||!password||password!==confirmation}>Change password and sign out</button></form></Card>
    <Card title="Active sessions"><Table rows={sessions.data||[]} rowKey={(row:any)=>row.created_at+row.ip} cols={[{key:'created_at',label:'Signed in',render:(row:any)=>fmtTime(row.created_at,true)},
      {key:'expires_at',label:'Expires',render:(row:any)=>fmtTime(row.expires_at,true)}, {key:'ip',label:'Address'}, {key:'user_agent',label:'Browser'}]}/>
      <button disabled={mutation.isPending} onClick={()=>{if(window.confirm('Sign out all your sessions, including this browser?'))mutation.mutate({path:'/account/sessions/revoke'});}}>Sign out all sessions</button></Card>
  </>;
}
import { PageTitle } from "../components/ui";
import { FormEvent, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api, User } from "../api";
import { Card, ErrorBox, Loading, Pill } from "../components/ui";

type Account = { username: string; is_owner: boolean; disabled: boolean; sites: Record<string, string> };
type Site = { id: string; name: string; description: string };

export default function Users({ user }: { user: User }) {
  const client = useQueryClient();
  const accounts = useQuery({ queryKey: ["users"], queryFn: () => api<Account[]>("/users") });
  const sites = useQuery({ queryKey: ["assignable-sites"], queryFn: () => api<Site[]>("/users/sites") });
  const [target, setTarget] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [grants, setGrants] = useState<Record<string, string>>({});
  const [message, setMessage] = useState("");
  const [resetTarget, setResetTarget] = useState("");
  const manageable = (sites.data || []).filter(site => user.is_owner || user.site_roles[site.id] === "admin");
  const change = useMutation({
    mutationFn: ({ path, method, body }: { path: string; method: string; body?: unknown }) => api(path, { method, body: body ? JSON.stringify(body) : undefined }),
    onSuccess: () => { client.invalidateQueries({ queryKey: ["users"] }); setPassword(""); setMessage("Saved"); },
  });
  const submit = (event: FormEvent) => {
    event.preventDefault();
    setMessage("");
    change.mutate(target ? { path: `/users/${encodeURIComponent(target)}/sites`, method: "PUT", body: { sites: grants, replace: true } }
      : { path: "/users", method: "POST", body: { username, password, sites: grants } });
  };
  if (accounts.isError || sites.isError) return <ErrorBox error={accounts.error || sites.error} />;
  if (!accounts.data || !sites.data) return <Loading />;
  return <>
    <div className="page-head"><PageTitle>Users</PageTitle></div>
    {change.isError && <ErrorBox error={change.error} />}
    {message && <div role="status">{message}</div>}
    {resetTarget && user.is_owner && <Card title={`Reset password: ${resetTarget}`}><form className="row" onSubmit={event => {
      event.preventDefault(); change.mutate({ path: `/users/${encodeURIComponent(resetTarget)}/password`, method: "POST", body: { password } }); setResetTarget("");
    }}><input aria-label="New password" type="password" autoComplete="new-password" required minLength={12} value={password} onChange={event => setPassword(event.target.value)} />
      <button className="primary" disabled={change.isPending}>Set password</button><button type="button" onClick={() => { setResetTarget(""); setPassword(""); }}>Cancel</button></form></Card>}
    <Card title={target ? `Site access: ${target}` : "Create user"}>
      <form onSubmit={submit} className="col">
        {!target && <div className="form-fields">
          <label>Username <input name="new-username" autoComplete="off" required value={username} onChange={event => setUsername(event.target.value)} /></label>
          <label>Initial password <input name="new-password" type="password" autoComplete="new-password" required minLength={12} value={password} onChange={event => setPassword(event.target.value)} /></label>
        </div>}
        <div className="table-wrap"><table className="t"><thead><tr><th>Site</th><th>Role</th></tr></thead><tbody>
          {manageable.map(site => <tr key={site.id}><td>{site.description}</td><td>
            <select aria-label={`Role for ${site.description}`} value={grants[site.id] || ""} onChange={event => setGrants(current => {
              const next = { ...current }; if (event.target.value) next[site.id] = event.target.value; else delete next[site.id]; return next;
            })}><option value="">Not assigned</option><option value="reader">Reader</option><option value="admin">Site admin</option></select>
          </td></tr>)}
        </tbody></table></div>
        <div className="row"><button className="primary" disabled={change.isPending || !Object.keys(grants).length}>{target ? "Save assignments" : "Create user"}</button>
          {target && <button type="button" onClick={() => { setTarget(""); setGrants({}); }}>Cancel</button>}
        </div>
      </form>
    </Card>
    <Card title="Accounts"><div className="table-wrap"><table className="t"><thead><tr><th>Username</th><th>Status</th><th>Site access</th><th>Actions</th></tr></thead><tbody>
      {accounts.data.map(account => <tr key={account.username}><td>{account.username} {account.is_owner && <Pill status="INFO">Dashboard owner</Pill>}</td>
        <td><Pill status={account.disabled ? "OFFLINE" : "OK"}>{account.disabled ? "Disabled" : "Active"}</Pill></td>
        <td>{Object.entries(account.sites).map(([id, role]) => <div key={id}>{sites.data!.find(site => site.id === id)?.description || "Unmonitored site"}: {role}
          <button disabled={change.isPending} title="Revoke this site assignment" onClick={() => change.mutate({ path: `/users/${encodeURIComponent(account.username)}/sites/${id}`, method: "DELETE" })}>Revoke</button></div>)}</td>
        <td><div className="row">
          {!account.is_owner && <button onClick={() => { setTarget(account.username); setGrants(Object.fromEntries(Object.entries(account.sites).filter(([id]) => manageable.some(site => site.id === id)))); setMessage(""); }}>Assign sites</button>}
          {user.is_owner && !account.is_owner && <button disabled={change.isPending} onClick={() => change.mutate({ path: `/users/${encodeURIComponent(account.username)}`, method: "PATCH", body: { disabled: !account.disabled } })}>{account.disabled ? "Enable" : "Disable"}</button>}
          {user.is_owner && <button onClick={() => { setResetTarget(account.username); setPassword(""); }}>Reset password</button>}
        </div></td>
      </tr>)}
    </tbody></table></div></Card>
  </>;
}
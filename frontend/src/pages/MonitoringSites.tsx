import { PageTitle } from "../components/ui";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { api } from "../api";
import { Card, ErrorBox, Loading, Pill } from "../components/ui";

type Site = { id: string; description: string; selected: boolean; connection_name: string };

export default function MonitoringSites() {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Record<string, boolean>>({});
  const sites = useQuery({ queryKey: ["monitoring-sites"], queryFn: () => api<Site[]>("/monitoring/sites"), refetchInterval: 2000 });
  const refreshState = useQuery({ queryKey: ['site-refresh'], queryFn: () => api<any>('/monitoring/refresh'), refetchInterval: 2000 });
  const refresh = useMutation({ mutationFn: () => api('/monitoring/refresh', {method:'POST'}),
    onSuccess: () => client.invalidateQueries({queryKey:['site-refresh']}) });
  useEffect(() => {
    if (refreshState.data?.state === 'complete') {
      client.invalidateQueries({queryKey:['monitoring-sites']});
      client.invalidateQueries({queryKey:['sites']});
    }
  }, [refreshState.data?.completed_at, client]);
  const refreshing = refresh.isPending || ['queued','checking'].includes(refreshState.data?.state);
  const selection = useMutation({
    mutationFn: (selections: Record<string, boolean>) => api('/monitoring/sites', { method: "PUT", body: JSON.stringify({ selections }) }),
    onSuccess: async () => { await client.invalidateQueries(); setDraft({}); },
  });
  const changes = Object.fromEntries((sites.data || []).filter(site => draft[site.id] !== undefined && draft[site.id] !== site.selected).map(site => [site.id, draft[site.id]]));
  const changeCount = Object.keys(changes).length;
  if (sites.isError) return <ErrorBox error={sites.error} />;
  if (!sites.data) return <Loading />;
  return <div className="monitoring-page">
    <div className="page-head"><PageTitle>Monitoring Sites</PageTitle><div className="right"><button disabled={refreshing} onClick={() => refresh.mutate()}>{refreshing ? 'Refreshing…' : 'Refresh sites'}</button></div></div>
    {refresh.isError && <ErrorBox error={refresh.error} />}
    {refreshState.data?.state === 'error' && <div role="alert" className="err">{refreshState.data.error}</div>}
    {refreshState.data?.state === 'complete' && <div role="status">Site list refreshed. Existing monitoring selections were preserved.</div>}
    <div className="small muted">All sites are enabled on first setup. Newly discovered sites appear here for you to enable; existing selections are never reset.</div>
    {selection.isError && <ErrorBox error={selection.error} />}
    <Card><div className="table-wrap"><table className="t"><thead><tr><th>Site</th><th>UniFi OS</th><th>State</th><th>Monitor</th></tr></thead><tbody>
      {sites.data.map(site => <tr key={site.id}><td>{site.description}</td><td>{site.connection_name}</td>
        <td><Pill status={site.selected ? "OK" : "UNKNOWN"}>{site.selected ? "Monitored" : "Not monitored"}</Pill></td>
        <td><input type="checkbox" aria-label={`Monitor ${site.description}`} checked={draft[site.id] ?? site.selected} disabled={selection.isPending}
          onChange={event => { const selected = event.target.checked; setDraft(previous => ({...previous, [site.id]: selected})); }} /></td>
      </tr>)}
    </tbody></table></div></Card>
    <div className="selection-actions" role="region" aria-label="Monitoring selection actions">
      <div><strong>{changeCount ? `${changeCount} unsaved site change${changeCount === 1 ? '' : 's'}` : 'All changes saved'}</strong><div className="small muted">Stopping monitoring retains historical data.</div></div>
      <div className="row"><button disabled={!changeCount || selection.isPending} onClick={() => setDraft({})}>Discard changes</button>
        <button className="primary" disabled={!changeCount || selection.isPending} onClick={() => selection.mutate(changes)}>{selection.isPending ? 'Applying…' : 'Apply selection'}</button></div>
    </div>
  </div>;
}
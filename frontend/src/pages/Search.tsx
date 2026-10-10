import { PageTitle } from "../components/ui";
import { useEffect, useState } from "react";
import { useInfiniteQuery } from "@tanstack/react-query";
import { useNavigate, useSearchParams } from "react-router-dom";
import { api, qs } from "../api";
import { ErrorBox, Loading, Table } from "../components/ui";

export default function Search() {
  const [params, setParams] = useSearchParams();
  const search = (params.get('q') || '').trim();
  const [value, setValue] = useState(search);
  useEffect(() => { setValue(search); }, [search]);
  const navigate = useNavigate();
  const query = useInfiniteQuery({ queryKey: ['search', search], queryFn: ({ pageParam }) => api<any[]>(`/search${qs({q:search,offset:pageParam})}`),
    initialPageParam: 0, enabled: search.length >= 1, getNextPageParam: (page, pages) => {
      const counts: Record<string, number> = {};
      for (const item of page) counts[item.type] = (counts[item.type] || 0) + 1;
      return Object.values(counts).some(count => count >= 100) ? pages.length * 100 : undefined;
    } });
  const results = query.data?.pages.flat() || [];
  const groups = [['site', 'Sites'], ['gateway', 'Gateways'], ['switch', 'Switches'], ['ap', 'Access Points'], ['device', 'Other Devices'],
    ['client', 'Clients'], ['event', 'Logs'], ['alert', 'Alerts'], ['change', 'Configuration Changes'], ['maintenance', 'Maintenance'], ['tunnel', 'Site-to-Site Links']];
  return <><div className="page-head"><PageTitle>Search</PageTitle></div><form className="row search-form" onSubmit={event=>{event.preventDefault();setParams({q:value.trim()});}}>
    <input type="search" aria-label="Search all monitored items" value={value} onChange={event=>setValue(event.target.value)} minLength={1} maxLength={100} required /><button className="primary">Search</button></form>
    {query.isError && <ErrorBox error={query.error} />}
    {search && query.isFetching && !query.data && <Loading />}
    {search && !query.isFetching && query.data && results.length === 0 && <div className="empty" role="status">No matches for &quot;{search}&quot;</div>}
    {groups.map(([type, title]) => {
      const rows = results.filter(item => item.type === type);
      return rows.length > 0 && <section className="search-results" key={type} aria-label={title}><h2>{title} <span className="small muted">({rows.length})</span></h2>
        <Table rows={rows} rowKey={(item:any)=>item.id || `${item.type}:${item.path}:${item.label}`} onRow={(item:any)=>navigate(item.path)}
          cols={[{key:'label',label:'Result'}, {key:'site',label:'Site'}, {key:'detail',label:'Details'}]} /></section>;
    })}
    {query.hasNextPage && <button disabled={query.isFetchingNextPage} onClick={() => query.fetchNextPage()}>{query.isFetchingNextPage ? 'Loading results...' : 'Load more results'}</button>}</>;
}
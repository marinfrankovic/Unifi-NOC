import { FormEvent, useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate, useBlocker, useBeforeUnload } from "react-router-dom";
import { api } from "../api";
import { Brand, Card, ErrorBox, PageTitle } from "../components/ui";
import { SetupShell } from "../components/SetupShell";

export function CreateOwner({ onCreated }: { onCreated: () => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [confirmation, setConfirmation] = useState("");
  const create = useMutation({ mutationFn: () => api('/setup/owner', { method: 'POST', body: JSON.stringify({ username, password }) }), onSuccess: onCreated });
  const submit = (event: FormEvent) => { event.preventDefault(); if (password === confirmation) create.mutate(); };
  return <SetupShell compact>
    <Brand heading subtitle="Create your dashboard administrator" />
    <Card><form className="col" onSubmit={submit}>
      <label>Username<input name="username" autoComplete="username" required value={username} onChange={event => setUsername(event.target.value)} /></label>
      <label>Password<input name="password" type="password" autoComplete="new-password" minLength={12} required value={password} onChange={event => setPassword(event.target.value)} /></label>
      <label>Confirm password<input name="confirmation" type="password" autoComplete="new-password" required value={confirmation} onChange={event => setConfirmation(event.target.value)} /></label>
      {password !== confirmation && confirmation && <div className="err">Passwords do not match</div>}
      {create.isError && <ErrorBox error={create.error} />}
      <button className="primary" disabled={create.isPending || !password || password !== confirmation}>{create.isPending ? 'Creating…' : 'Create administrator'}</button>
    </form></Card>
    <details><summary>About your administrator account</summary><p>This account manages the NOC and delegates access to selected sites. It is separate from your UniFi OS account. Complete first-time setup only over a trusted network.</p></details>
  </SetupShell>;
}

export function ConnectionSetup({ onConnected }: { onConnected?: () => void }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const [url, setUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [caPem, setCaPem] = useState("");
  const [serverName, setServerName] = useState("");
  const [trust, setTrust] = useState(false);
  const [inspectRequested, setInspectRequested] = useState(false);
  const [gatewaySsh, setGatewaySsh] = useState(false);
  const [probes, setProbes] = useState(false);
  const [probeSite, setProbeSite] = useState("");
  const [initialized, setInitialized] = useState(false);
  const status = useQuery({ queryKey: ['setup-connection'], queryFn: () => api<any>('/setup/connection'), refetchInterval: 2000 });
  const configuration = status.data?.configuration;
  const sameController = !!configuration && url.replace(/\/+$/, '') === configuration.url.replace(/\/+$/, '');
  const reuseSavedKey = sameController && configuration.has_api_key;
  useEffect(() => {
    if (initialized || !status.isSuccess) return;
    if (configuration) {
      setUrl(configuration.url);
      setServerName(configuration.tls_server_name);
      setGatewaySsh(configuration.gateway_ssh);
      setProbes(configuration.probes);
      setProbeSite(configuration.probe_site_id || configuration.probe_site);
    }
    setInitialized(true);
  }, [initialized, status.isSuccess, configuration]);
  const discovered = status.data?.connected || status.data?.state === 'connected';
  const probeSites = useQuery({ queryKey: ['connection-probe-sites'], queryFn: async () => {
    const [sites, tunnels] = await Promise.all([api<{ name: string; description: string }[]>('/sites'), api<{ members: { site: string }[] }[]>('/s2s')]);
    const members = new Set(tunnels.flatMap(tunnel => tunnel.members.map(member => member.site)));
    return sites.filter(site => members.has(site.name)).sort((first, second) => first.description.localeCompare(second.description));
  }, enabled: !!discovered, refetchInterval: 15000 });
  const probeMatches = (probeSites.data || []).filter(site => site.name === probeSite || site.description === probeSite);
  const selectedProbeSite = probeMatches.length === 1 ? probeMatches[0].name : '';
  const certificate = useQuery({ queryKey: ['setup-certificate'], queryFn: () => api<any>('/setup/certificate'), enabled: inspectRequested, refetchInterval: 2000 });
  const certificateError = status.data?.state === 'error' && /CERTIFICATE_VERIFY_FAILED|self-signed certificate|certificate verify failed/i.test(status.data.error || '');
  useEffect(() => { if (status.data?.state === 'connected') setApiKey(''); }, [status.data?.state]);
  const connect = useMutation({ mutationFn: () => api('/setup/connection', { method: 'POST', body: JSON.stringify({ url, api_key: apiKey, ca_pem: caPem || (sameController ? undefined : ''),
    tls_server_name: serverName, gateway_ssh: gatewaySsh, probes, probe_site: probes ? probeMatches[0]?.description || probeSite : '', probe_site_id: probes ? selectedProbeSite : '' }) }), onSuccess: () => { client.invalidateQueries({ queryKey: ['setup-connection'] }); } });
  const inspect = useMutation({ mutationFn: () => api('/setup/certificate', { method: 'POST', body: JSON.stringify({ url, tls_server_name: serverName }) }),
    onSuccess: () => { setInspectRequested(true); setTrust(false); client.invalidateQueries({ queryKey: ['setup-certificate'] }); } });
  useEffect(() => {
    if (status.data?.state !== 'connected' || !status.data.complete) return;
    if (onConnected || (connect.data?.job_id && status.data.job_id === connect.data.job_id)) {
      client.invalidateQueries({ queryKey: ['sites'] });
      client.invalidateQueries({ queryKey: ['overview'] });
      if (onConnected) onConnected(); else navigate('/');
    }
  }, [status.data?.state, status.data?.complete, status.data?.job_id, connect.data?.job_id, onConnected, client, navigate]);
  const busy = connect.isPending || inspect.isPending || status.data?.state === 'checking' || certificate.data?.state === 'checking';
  const dirty = !!configuration && initialized && (!!apiKey || !!caPem || url !== configuration.url || serverName !== configuration.tls_server_name || gatewaySsh !== configuration.gateway_ssh || probes !== configuration.probes || probeSite !== (configuration.probe_site_id || configuration.probe_site));
  const saved = !!connect.data?.job_id && status.data?.state === 'connected' && status.data?.job_id === connect.data.job_id;
  const blocker = useBlocker(dirty && !saved);
  useEffect(() => { if (saved && blocker.state === 'blocked') blocker.proceed(); }, [saved, blocker]);
  useBeforeUnload(event => { if (dirty && !saved) { event.preventDefault(); event.returnValue = ''; } });
  const discard = () => { setInitialized(false); setApiKey(''); setCaPem(''); setTrust(false); };
  return <div className="col">
    <PageTitle breadcrumbs={!onConnected}>Connect UniFi OS</PageTitle>
    {blocker.state === 'blocked' && <div className="banner warn" role="alert">Unsaved connection changes <button onClick={() => blocker.reset()}>Keep editing</button><button onClick={() => blocker.proceed()}>Discard and leave</button></div>}
    <Card><form className="col settings-form connection-form" onSubmit={event => { event.preventDefault(); connect.mutate(); }}>
      <label>UniFi OS address<input type="url" placeholder="https://192.168.1.1" required value={url} onChange={event => setUrl(event.target.value)} /></label>
      <label>Network API key<input type="password" autoComplete="off" required={!reuseSavedKey} placeholder={reuseSavedKey ? 'Saved API key (unchanged)' : ''} value={apiKey} onChange={event => setApiKey(event.target.value)} /></label>
      <details><summary>Where do I get the API key?</summary><p>Sign in to your UniFi OS as an administrator, open Network, then Settings → Control Plane → Integrations. Create a supported API key for monitoring. Some Network versions place integrations under System. Do not enter your UniFi account password here.</p></details>
      <details open={certificateError || inspectRequested || undefined}><summary>Certificate and connection help</summary>
        <p>Publicly trusted certificates work automatically. For a private or self-signed certificate, choose either option below. You do not need to do both.</p>
        <div className="certificate-options">
          <div className="certificate-method"><strong>Upload a certificate</strong><span className="small muted">Already have a trusted PEM certificate? Choose your file.</span>
            <label>Trusted certificate<input type="file" accept=".pem,.crt,.cer" onChange={async event => { const file = event.target.files?.[0]; if (file) setCaPem(await file.text()); }} /></label>
          </div>
          <span className="certificate-or">OR</span>
          <div className="certificate-method"><strong>Inspect the UniFi OS certificate</strong><span className="small muted">No file needed. Retrieve it, verify the fingerprint, then trust it.</span>
            <button type="button" disabled={busy || !url} onClick={() => inspect.mutate()}>Inspect certificate</button>
          </div>
        </div>
        <label>TLS certificate hostname<input placeholder="Optional, when connecting by IP" value={serverName} onChange={event => setServerName(event.target.value)} /></label>
        {certificate.data?.state === 'ready' && <div className="col"><span>{certificate.data.subject}</span><span>Expires: {certificate.data.expires}</span>
          <code className="fingerprint">SHA-256: {certificate.data.fingerprint}</code><span>Certificate names: {certificate.data.dns_names.join(', ') || 'No DNS names reported'}</span>
          <label className="row"><input type="checkbox" checked={trust} onChange={event => setTrust(event.target.checked)} />I verified this certificate fingerprint</label>
          <button type="button" disabled={!trust} onClick={() => { setCaPem(certificate.data.pem); const name = certificate.data.dns_names.find((value: string) => !value.includes('*')); if (!serverName && name) setServerName(name); }}>Trust this certificate</button>
        </div>}
        {(caPem || (sameController && configuration.ca_configured)) && <span className="small">A private trust certificate is configured.</span>}
        {certificate.data?.state === 'error' && <div className="err">{certificate.data.error}</div>}
      </details>
      <details><summary>Optional live tunnel measurements</summary>
        <p><strong>Start with both options off.</strong> The UniFi API already provides site, device, client and reported tunnel status. These extra measurements are useful when troubleshooting slow or unreliable connections; they are not required to connect or use the dashboard.</p>
        <label className="row"><input type="checkbox" aria-describedby="gateway-ssh-help" checked={gatewaySsh} onChange={event => setGatewaySsh(event.target.checked)} />Read gateway counters over SSH</label>
        <p id="gateway-ssh-help" className="small muted">Reads live traffic, interface errors and drops directly from gateways, adding more frequent WAN measurements and tunnel traffic where it can be matched reliably. Requires reachable gateways with SSH enabled and usable SSH credentials. Leave off if you only need API monitoring or do not want SSH access. The NOC runs read-only commands; it does not enable SSH or change gateway configuration.</p>
        <details><summary>Gateway SSH requirements</summary><p>Configure device SSH authentication in UniFi Network for each gateway site. The collector retrieves that site's existing SSH username and password through the controller; do not enter your UniFi account password into the NOC. Your API key must be allowed to read those settings.</p>
          <p>Allow this collector to reach each gateway's LAN address on TCP port 22. Enabling this checkbox only starts read-only collection; it does not enable SSH on the gateways. Console SSH credentials may differ from Network device SSH credentials.</p>
          <p>The first gateway SSH host key is pinned automatically. A changed key stops collection until verified. Check Diagnostics for missing credentials, unreachable gateways or SSH errors.</p></details>
        <label className="row"><input type="checkbox" aria-describedby="tunnel-probe-help" checked={probes} disabled={!probes && (!discovered || !probeSites.data?.length)} onChange={event => setProbes(event.target.checked)} />Measure ICMP latency/loss through the tunnel</label>
        <p id="tunnel-probe-help" className="small muted">Sends small periodic pings from this collector to remote gateway addresses to measure response time, packet loss and jitter. Useful for spotting delays or instability that a connected tunnel status alone can miss. Requires ICMP replies and a route through the tunnel from the site named below. Blocked pings can look like packet loss even when the tunnel works. A cloud or remote collector measures its own network path, not automatically the path between your sites; leave off if that route is not known.</p>
        {probes && <label>Site containing this collector<select value={selectedProbeSite} onChange={event => setProbeSite(event.target.value)} required disabled={!probeSites.data?.length}>
          <option value="">Select the site containing this collector</option>{(probeSites.data || []).map(site => <option key={site.name} value={site.name}>{site.description}</option>)}
        </select></label>}
        {!discovered ? <p className="small muted">Connect and discover sites first, then enable probes in Connection settings.</p> : probeSites.isError ? <ErrorBox error={probeSites.error} /> : !probeSites.data?.length && <p className="small muted">{probeSites.isPending ? 'Loading SD-WAN sites...' : 'No monitored sites participate in a reported SD-WAN link. Tunnel probes are unavailable.'}</p>}
        <p className="small muted">SSH and ping measurements are independent and add a small amount of network activity. You can enable either later in connection settings. Unavailable measurements are not replaced with guessed values.</p>
      </details>
      {(connect.isError || inspect.isError) && <ErrorBox error={connect.error || inspect.error} />}
      {certificateError ? <div className="err" role="alert"><strong>{caPem ? 'Certificate verification failed' : 'Certificate approval required'}</strong>
        <p>Upload a trusted PEM certificate, or click Inspect certificate above, verify its SHA-256 fingerprint, and choose Trust this certificate. When connecting by IP, use a hostname listed on the certificate. Then retry Connect and discover sites. Certificate checks remain enabled.</p>
        <details><summary>Technical details</summary>{status.data.error}</details>
      </div> : status.data?.state === 'error' && <div className="err">{status.data.error}</div>}
      <div className="row"><button className="primary" disabled={busy || (!apiKey && !reuseSavedKey) || (probes && !selectedProbeSite)}>{busy ? 'Checking connection…' : configuration ? 'Save connection' : 'Connect and discover sites'}</button>{dirty && <><span className="pill warn">Unsaved changes</span><button type="button" disabled={busy} onClick={discard}>Discard</button></>}</div>
      {status.data?.state === 'connected' && <div role="status">Connected to UniFi Network {status.data.version}. {status.data.discovered_sites} sites discovered.</div>}
    </form></Card>
  </div>;
}

export function SetupWizard({ onComplete }: { onComplete: () => void }) {
  return <SetupShell>
    <div className="row"><b>UniFi NOC</b><span className="muted">Connect your UniFi OS instance</span></div>
    <ConnectionSetup onConnected={onComplete} />
    <p className="small muted">All discovered sites are monitored after first connection. You can turn individual sites off later in Monitoring Sites.</p>
  </SetupShell>;
}
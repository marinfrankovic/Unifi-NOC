import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const namespace = process.env.DOCKERHUB_USERNAME;
const secret = process.env.DOCKERHUB_TOKEN;
if (!namespace || !secret) throw new Error('Docker Hub publishing credentials are required');
const source = 'https://github.com/marinfrankovic/Unifi-NOC';
const readme = readFileSync(resolve(root, 'README.md'), 'utf8')
  .replaceAll('](LICENSE)', `](${source}/blob/main/LICENSE)`)
  .replaceAll('](NOTICE)', `](${source}/blob/main/NOTICE)`)
  .replace(/\]\((site\/src\/assets\/[^)]+)\)/g, (_match, path) => `](https://raw.githubusercontent.com/marinfrankovic/Unifi-NOC/main/${path})`);

async function request(path, options = {}) {
  const response = await fetch(`https://hub.docker.com${path}`, options);
  if (!response.ok) throw new Error(`Docker Hub operation failed (HTTP ${response.status}; credentials/response body suppressed)`);
  return response.json();
}
const auth = await request('/v2/auth/token', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ identifier: namespace, secret }) });
const headers = { Authorization: `Bearer ${auth.access_token}`, 'Content-Type': 'application/json' };
const roles = {
  backend: 'UniFi NOC API + collector: WAN, SD-WAN, telemetry, alerts. Noncommercial use only.',
  web: 'UniFi NOC dashboard: topology, health, clients, alerts, wallboard. Noncommercial use.',
};
for (const [role, description] of Object.entries(roles)) {
  const name = `unifi-noc-${role}`;
  const full = `# Unifi-NOC ${role === 'backend' ? 'Backend' : 'Web'} Image\n\nUse this image through the complete [Docker Compose deployment](${source}/blob/main/docs/deployment.md), not as a standalone application.\n\n${readme}`;
  const url = `/v2/namespaces/${namespace}/repositories/${name}`;
  const found = await fetch(`https://hub.docker.com${url}`, { headers });
  if (found.status === 404) {
    await request(`/v2/namespaces/${namespace}/repositories`, { method: 'POST', headers, body: JSON.stringify({ name, namespace, registry: 'docker.io', is_private: false, description, full_description: full }) });
  } else {
    if (!found.ok) throw new Error(`Docker Hub repository check failed (HTTP ${found.status})`);
    const existing = await found.json();
    if (existing.is_private) throw new Error('Refusing to overwrite an existing private image repository');
    await request(`/v2/repositories/${namespace}/${name}/`, { method: 'PATCH', headers, body: JSON.stringify({ description, full_description: full }) });
  }
  const verified = await request(url, { headers });
  if (verified.is_private || verified.description !== description || !verified.full_description?.includes('PolyForm')) throw new Error('Docker Hub public metadata verification failed');
  console.log(`Docker Hub public metadata verified: ${namespace}/${name}`);
}
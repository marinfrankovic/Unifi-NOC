import { appendFileSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const role = process.argv[2];
if (!['backend', 'web'].includes(role)) throw new Error('Expected backend or web image role');
const stable = readFileSync(resolve(root, 'VERSION'), 'utf8').trim();
if (!/^\d+\.\d+\.\d+$/.test(stable)) throw new Error('Invalid stable VERSION');
const ref = process.env.GITHUB_REF || 'refs/heads/main';
const namespace = process.env.DOCKERHUB_USERNAME || 'mfrankovic';
const revision = process.env.GITHUB_SHA || 'local';
const run = process.env.GITHUB_RUN_NUMBER || '0';
if (!/^[a-z0-9_-]+$/.test(namespace) || !/^\d+$/.test(run)) throw new Error('Invalid registry/build metadata');
let version;
let suffixes;
if (ref.startsWith('refs/tags/')) {
  if (ref !== `refs/tags/v${stable}`) throw new Error('Release tag must match VERSION');
  version = stable;
  const [major, minor] = stable.split('.');
  suffixes = [stable, `${major}.${minor}`, major, 'latest', `sha-${revision.slice(0, 12)}`];
} else {
  if (ref !== 'refs/heads/main') throw new Error('Only main or stable version tags may publish');
  version = `${stable}-main.${run}`;
  suffixes = [version, 'edge', `sha-${revision.slice(0, 12)}`];
}
const tags = suffixes.map(suffix => `${namespace}/unifi-noc-${role}:${suffix}`);
if (process.env.GITHUB_OUTPUT) appendFileSync(process.env.GITHUB_OUTPUT, `version=${version}\ntags<<NOC_TAGS\n${tags.join('\n')}\nNOC_TAGS\n`);
console.log(JSON.stringify({ version, tags, revision }));
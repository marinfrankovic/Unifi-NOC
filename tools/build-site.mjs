import { cpSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const version = readFileSync(resolve(root, 'VERSION'), 'utf8').trim();
if (!/^\d+\.\d+\.\d+$/.test(version)) throw new Error('VERSION must be a stable semantic version');
const build = process.env.GITHUB_RUN_NUMBER || 'preview';
const commit = (process.env.GITHUB_SHA || 'local-preview').slice(0, 12);
const destination = resolve(root, 'site/dist');
mkdirSync(destination, { recursive: true });
cpSync(resolve(root, 'site/src'), destination, { recursive: true });
cpSync(resolve(root, 'frontend/public/favicon.svg'), resolve(destination, 'assets/mark.svg'));
const page = readFileSync(resolve(destination, 'index.html'), 'utf8')
  .replaceAll('%%VERSION%%', version)
  .replaceAll('%%BUILD%%', build)
  .replaceAll('%%COMMIT%%', commit);
writeFileSync(resolve(destination, 'index.html'), page);
writeFileSync(resolve(destination, 'version.json'), JSON.stringify({ version, build, commit, builtAt: new Date().toISOString(), repository: 'https://github.com/marinfrankovic/Unifi-NOC' }, null, 2) + '\n');
writeFileSync(resolve(destination, '.nojekyll'), '');
console.log(`Pages build PASS: version ${version}, build ${build}, commit ${commit}`);
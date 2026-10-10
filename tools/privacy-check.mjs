import { execFileSync } from 'node:child_process';
import { existsSync, lstatSync, readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const patterns = [
  ['GitHub token', /(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{30,})/],
  ['Docker access token', /dckr_pat_[A-Za-z0-9_-]{20,}/],
  ['AWS access key', /AKIA[A-Z0-9]{16}/],
  ['private key', /-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----/],
  ['private Windows user path', /[A-Z]:[\\/]Users[\\/][^\s"']+/],
  ['private deployment location', /\/mnt\/docker-storage\/compose\//],
  ['private instance identity', new RegExp(['Frank','ovic'].join('') + '\\s+(?:AM|ZG)')],
  ['private host address', new RegExp(['192','168','100','160'].join('\\.'))],
  ['private site identity', new RegExp(['a82','tiss1'].join('') + '|' + ['938','fkixg'].join(''))],
];

if (process.argv.includes('--self-test')) {
  const fixtures = ['ghp_' + 'x'.repeat(30), 'dckr_pat_' + 'x'.repeat(30), '-----BEGIN ' + 'PRIVATE KEY-----'];
  if (fixtures.some(text => !patterns.some(([, pattern]) => pattern.test(text)))) throw new Error('Privacy scanner self-test failed');
  console.log('Privacy scanner self-test PASS');
}

const files = [...new Set(execFileSync('git', ['-C', root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z'], { encoding: 'utf8' }).split('\0').filter(Boolean))];
const failures = [];
for (const name of files) {
  if (/^(?:certs|backups|data|\.venv|frontend\/(?:dist|node_modules)|tools\/_shots|site\/dist)\//.test(name) || /(?:^|\/)\.env(?:\..*)?$/.test(name) && name !== '.env.example' || /\.(?:pem|key|p12|pfx|dump|tar\.gz)$/.test(name) || /^docker-compose\.override\.ya?ml$/.test(name) || name === 'DEPLOYMENT_REPORT.md') {
    failures.push(`${name}: runtime/private artifact`);
    continue;
  }
  const path = resolve(root, name);
  if (!existsSync(path)) continue;
  if (lstatSync(path).isSymbolicLink()) { failures.push(`${name}: symlink must be reviewed`); continue; }
  const bytes = readFileSync(path);
  if (bytes.includes(0)) continue;
  for (const [reason, pattern] of patterns) if (pattern.test(bytes.toString('utf8'))) failures.push(`${name}: ${reason}`);
}
if (failures.length) {
  console.error(failures.join('\n'));
  process.exit(1);
}
console.log(`Publication privacy check PASS (${files.length} files; no matched secret values printed)`);
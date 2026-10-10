# Contributing

Read [LICENSE](LICENSE) before using or contributing. This is source-available under PolyForm Noncommercial 1.0.0, not unrestricted commercial-use software. Contributions should be offered under the project's existing license; third-party code/assets must retain their own required licenses/notices.

Use synthetic fixtures and never connect tests to a real controller. Keep changes scoped, document user-visible behavior and preserve account/site/history/encryption-key compatibility. Do not include private hostnames, real device/client addresses, source deployments, secrets or screenshots of production.

```bash
npm --prefix frontend ci
npm --prefix frontend run build
python -m pip install -r backend/requirements.txt playwright
python -m playwright install chromium
NOC_BROWSER_CHANNEL=chromium python tools/security_test.py
node tools/privacy-check.mjs --self-test
```

Windows fixtures default to installed Edge; set `NOC_BROWSER_CHANNEL=chromium` after installing Playwright Chromium if preferred. Database migration tests require a disposable `noc_test` database and are never run against production. See the test helper's usage before running it.

Open a pull request with the behavior, verification and any migration considerations. Publishing secrets are unavailable to pull-request checks. Do not bypass privacy failures by force-adding ignored files.
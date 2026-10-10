# Troubleshooting

[Project](https://github.com/marinfrankovic/Unifi-NOC) | [Deploy](https://github.com/marinfrankovic/Unifi-NOC/blob/main/docs/deployment.md) | [Issues](https://github.com/marinfrankovic/Unifi-NOC/issues)

| Symptom | Check |
| --- | --- |
| Browser certificate warning | Initial web certificate is self-signed; deploy trusted TLS before sharing access |
| Redirect loop or wrong port | Proxy HTTPS upstream, `/unifi/` preservation, external port and TLS settings |
| Setup cannot reach controller | DNS and routes from collector, correct console URL, integration key and API support |
| Controller TLS error | CA trust, chain validity and hostname; verification must remain enabled |
| No sites/data | Owner connection, initial discovery, selected sites, grants and collector diagnostics |
| Healthy-looking inventory but stale status | Check source age and collector errors; stale data is not current health |
| Connected tunnel without RTT or traffic | Controller state is separate; optional probes/SSH, capabilities and routes may be missing |
| Probe shows no reply | ICMP filtering, container/host permissions and routing; not proof of tunnel disconnection |
| Missing temperature/PoE/radio metric | Device/API may not support it; absence must not be treated as zero |
| Alerts remain after acknowledgement | Acknowledgement records review; the firing condition must recover separately |
| Reader sees no site | Site-scoped grants default to denied; ask the owner or authorized site administrator |
| Saved profile cannot decrypt | Restore the matching `noc-state` encryption key with the database; do not regenerate it |
| Database login fails after secret change | Recover matching `db-secrets` and database credentials; do not reset volumes |
| Docker networking shadows LAN/VPN | Check overlap and choose unused `NOC_*_SUBNET` values before deployment |
| Image not found | Use a published stable version or wait for its tagged Actions build; backend/web tags must match |

Start with `docker compose ps`, logs and owner Diagnostics. Before reporting an issue, remove API keys, passwords, tokens, private CA/key data, IP/MAC addresses, device/client names, site identifiers and deployment paths. Never submit dumps or backups. Use [private security reporting](https://github.com/marinfrankovic/Unifi-NOC/security/advisories/new) for vulnerabilities.
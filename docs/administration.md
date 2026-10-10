# Administration And Recovery

[Project](https://github.com/marinfrankovic/Unifi-NOC) | [Deployment](https://github.com/marinfrankovic/Unifi-NOC/blob/main/docs/deployment.md)

## Accounts And Scope

The first account becomes the instance owner. Owner-only functions include controller connection, collection-site selection, notifications and diagnostics. General administrator status is not equivalent to ownership. Site administrators can delegate only within sites they administer; readers have read access only to assigned sites. Unassigned sites default to denied.

Use distinct accounts and strong passwords. Normal sessions default to 12 hours; Remember Me can persist for 30 days. Use sign out on shared machines. Restrict network access; there is no claim of MFA, SSO or complete enterprise identity integration.

## Change Settings Safely

Public defaults belong in source control. Real `.env`, local network overrides, TLS private keys and UI-stored controller/notification profiles do not. Controller secrets are encrypted in the database using the persistent instance key; backup both together. Do not rotate/delete the key volume casually. A blank key field can reuse the stored key for the same controller, not a new controller.

Changing the controller or monitoring scope affects future collection. Review retention and permissions after adding sites. Test notification channels with non-sensitive messages before relying on delivery.

## Health And Logs

```bash
docker compose ps
docker compose logs --tail=100 api collector web
docker compose exec -T db pg_isready -U noc -d noc
```

Use owner Diagnostics to distinguish controller TLS/authentication errors, collector failures, stale sources and optional-measurement limitations. Logs can contain addresses and device names; redact them before opening public issues.

## Backup

```bash
bash backup.sh ./backups
```

The backup contains a PostgreSQL custom dump, available `.env`/Compose configuration, controller certificates, web certificates, instance key and database-secret volumes. It is created with restricted filesystem permissions, **not archive encryption**. Encrypt off-host copies and test them before relying on recovery. The default helper retains the newest 14 matching archives; set `KEEP` to suit your policy.

An archive contains credentials and personal telemetry. Never upload it to GitHub or attach it to an issue. Back up external reverse-proxy configuration and any mount paths outside this stack separately.

## Verify And Restore

```bash
bash restore.sh ./backups/your-backup.tar.gz --verify
```

The verification restores the dump into a scratch database on the running database service and prints row counts. It does not prove full end-to-end recovery, historical equality, profile decryption or notification delivery. It can consume considerable disk space. Also rehearse a complete recovery on an isolated host with restored secrets before trusting the backup.

```bash
bash restore.sh ./backups/your-backup.tar.gz --with-env --yes
```

**Full restore replaces live data.** Without `--yes`, the script requires explicit confirmation. It first creates a safety backup, stops application services, restores available state/certificates, recreates the database with TimescaleDB restore hooks and starts the stack. Review the manifest and choose compatible PostgreSQL/TimescaleDB/application versions first.

The helper is for an already-created stack with matching project/volumes. Review and restore the archived Compose files/overrides separately when moving hosts; `--with-env` restores environment/certificates, not all external deployment infrastructure. A restore reporting row counts is not proof that every dump object succeeded. Inspect errors and verify accounts, grants, profile decryption, representative history and collector operation.

## Upgrade

1. Review [release notes](https://github.com/marinfrankovic/Unifi-NOC/releases), compatibility and required migrations.
2. Back up the full stack and record current image versions and project name.
3. Set `NOC_VERSION` to the chosen stable version in your private `.env`.
4. Run `docker compose pull` followed by `docker compose up -d`.
5. Verify health, accounts, selected sites, saved settings, representative history and fresh collection.

Never run `docker compose down -v`, change `STACK_NAME` unintentionally, or recreate a database from scratch as an upgrade. Migrations are applied by the backend and tracked in the database. Do not change database major versions opportunistically.

## Rollback

Changing the image tag back is safe only when the database remains compatible. A newer migration can make an older image unusable. For incompatible migrations, restore the protected pre-upgrade database **and** its matching encryption key/configuration/secrets, accepting the loss of data collected after that backup. Rehearse rollback in isolation.

Automatic publication creates images and updates the website; it does not grant GitHub Actions access to a private Docker host or automatically run database migrations there. Production upgrades remain an explicit, backed-up operation.
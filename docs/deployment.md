# Deployment Guide

[Project](https://github.com/marinfrankovic/Unifi-NOC) | [README](https://github.com/marinfrankovic/Unifi-NOC#readme) | [Releases](https://github.com/marinfrankovic/Unifi-NOC/releases)

## Prerequisites

Linux x86-64, Docker with Compose 2.30+, 2 CPU cores, 4 GB RAM, persistent storage, and HTTPS connectivity from the collector to a UniFi OS console. Network 10.6 was used during development; there is no vendor compatibility certification. Consoles without the required integration API and endpoints are not guaranteed to work. Use the owner setup form to validate your controller before relying on telemetry.

The published images include an API/collector image and a web image. Do not run the backend alone or expose its port directly. PostgreSQL/TimescaleDB is private inside the stack. The web UI path in published images is fixed at `/unifi/`.

## Install

```bash
git clone https://github.com/marinfrankovic/Unifi-NOC.git
cd Unifi-NOC
cp .env.example .env
chmod 600 .env
docker compose pull
docker compose up -d
docker compose ps
```

Initial setup generates the database password automatically and stores it in `db-secrets`. Create the owner in the web UI; no default login password is shipped. Do not replace or reset the database secret on an existing installation.

## Remote Setup

Loopback binding prevents a publicly reachable unclaimed owner screen. From your workstation, forward the HTTPS port:

```bash
ssh -L 8096:127.0.0.1:8096 user@docker-host
```

Then open https://localhost:8096/unifi/. Complete owner setup before changing exposure. For trusted LAN access, set `BIND_ADDRESS` to the host's LAN address or `0.0.0.0`, restrict access using the host firewall, and run `docker compose up -d web`. A managed HTTPS reverse proxy is recommended for shared access. Do not expose the database or API service.

## Controller Connection

Create a UniFi Network integration API key in the console's supported integration settings. Apply the least permissions your console supports. The collector uses read requests; possession of a powerful API key can still be dangerous if compromised.

In **UniFi OS Connection**, provide the URL, API key and TLS details. Docker container DNS/routing must resolve and reach the URL. A private CA can be supplied through the setup form. The hostname used for verification must match the certificate. Disabling verification is deliberately rejected.

If container DNS cannot resolve your controller's internal name, add a local, uncommitted override using your actual address. The documentation-only address below must be replaced:

```yaml
services:
  collector:
    extra_hosts:
      - "controller.internal:192.0.2.10"
```

Save this as `docker-compose.override.yml` locally and use https://controller.internal in the UI. Do not publish the real override. API and collector share the encryption-key volume; losing it makes stored credentials undecryptable.

## Web TLS

The web image generates an initial self-signed certificate named by `TLS_CN`. That certificate is not automatically trusted by client devices. For trusted access, terminate HTTPS with your own reverse proxy or mount a directory containing `tls.crt` and `tls.key` into `/etc/nginx/certs`. Inspect the certificate helper and nginx template before replacing files. Keep private keys outside the Git checkout's published files and restrict their permissions.

When proxying the stack, preserve `/unifi/`, use its HTTPS upstream, and validate or explicitly trust the upstream certificate rather than globally disabling verification. The backend trusts forwarded headers only because it is network-isolated behind nginx; never publish it directly. A nonstandard external port or TLS name should be reflected in the web configuration.

## Networks And Probes

The stack reserves four `172.31.60-63.0/24` networks by default. Change the `NOC_*_SUBNET` variables before installation if they overlap with your LAN, VPN, routing table or other Docker networks. Do not casually renumber an existing stack during an upgrade.

Gateway SSH and ICMP collection are optional. SSH requires supported credentials and reachable gateways; no SSH credentials are bundled. ICMP availability depends on host permissions, device response and routes from the collector. A failed probe does not imply the controller reports a disconnected tunnel.

## Persistent Volumes

| Volume | Contains |
| --- | --- |
| `db-data` | Accounts, grants, settings, metrics, events and alerts |
| `noc-state` | Instance encryption key for controller/notification secrets |
| `db-secrets` | Generated database password |
| `web-certs` | Web TLS certificate and private key |

All are prefixed with the Compose project name. Keep `STACK_NAME` consistent. Never delete these volumes during upgrades.

## Build From Source

```bash
docker compose -f docker-compose.yml -f compose.build.yml build
docker compose -f docker-compose.yml -f compose.build.yml up -d
```

The build context is the repository root, where both Dockerfiles can include the license and notices. Custom frontend base paths require a coordinated source build and web configuration; public images use `/unifi/`.

Read [administration](https://github.com/marinfrankovic/Unifi-NOC/blob/main/docs/administration.md) before upgrading or restoring data. Commercial use is not granted by this project license.
# Monitoring Reference

[Project](https://github.com/marinfrankovic/Unifi-NOC) | [User Guide](https://github.com/marinfrankovic/Unifi-NOC/blob/main/docs/user-guide.md)

## Sources And Cadence

| Source | Default interval | Typical observations |
| --- | --- | --- |
| Main inventory/state | 15 seconds | Sites, infrastructure, health and WAN counters |
| Clients | 30 seconds | Associations, wired/wireless state and available client counters |
| Events | 60 seconds | Controller-reported events/incidents |
| Slower discovery | 300 seconds | Available topology, configuration and capabilities |
| Optional live measurements | 5 seconds | Available gateway SSH counters and ICMP probes |

Intervals are scheduling targets, not guaranteed delivery rates. Endpoint availability, backoff, routing and controller load can increase gaps. Avoid aggressive polling on undersized consoles.

## Coverage

Sites, gateways, switches, APs, WAN, discovered site-to-site/SD-WAN links and clients are supported surfaces. Actual metrics vary by model and API version. Temperature is required only for devices that report/support it or have previously reported it. Missing PoE/radio/counter fields are not manufactured.

For site-to-site links, controller connection state, probe state and traffic measurements are separate signals. A connected link without RTT, jitter, loss or counters displays incomplete measurement coverage. Remote counters can be normalized to the local direction and retain source provenance.

Switch interfaces can expose speed, link, RX/TX, errors/drops and PoE. Wireless data can expose channel, signal, retries, airtime and association information. “Other airtime” is computed only when its source measurements are present. Genuine zero counters remain valid; absent fields do not become zero traffic.

## Health And Availability

Health combines observed threshold checks with explicit critical/warning reasons and coverage. Global severity reflects the worst relevant site condition rather than averaging a critical site away. No observations produce unknown/no score. Stale inventory or partial required coverage remains visible.

Availability is based on sampled observations and expected cadence. Coverage gaps are unknown, not presumed online. Outage/event counts use defined windows; maintenance can explain or suppress configured alert workflows but does not rewrite underlying observations. These estimates do not provide compliance-grade or contractual SLA guarantees.

## Retention

Defaults are 48 hours raw metrics, 30 days five-minute rollups, 365 days hourly rollups and 90 days logs/events where applicable. Configure the corresponding retention variables before collecting more personal information than necessary. Storage use scales with device/client counts and poll intervals; no universal capacity guarantee is made.

## Limits

One connected UniFi OS source is configured through the UI. Collection is restricted to selected sites. Sites must be reachable/discoverable through that source. This is not a multi-controller federation service.

No UniFi configuration changes, device adoption, firmware installation or traffic blocking are intended. It is monitoring software, not the vendor's controller, a packet capture system, SIEM or IDS. Controller upgrades may change API behavior. SSH measurements require supported gateways and authorized access. ICMP may be blocked, and no reply is not proof of a disconnected tunnel.

The application does not send telemetry to a project-operated service. Configured controllers, probes and notification providers receive the requests necessary for their roles. Docker/operating-system/container-provider behavior remains governed by those third parties.
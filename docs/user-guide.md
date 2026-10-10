# User Guide

[Project](https://github.com/marinfrankovic/Unifi-NOC) | [Monitoring Reference](https://github.com/marinfrankovic/Unifi-NOC/blob/main/docs/monitoring.md)

## First Session

Create the initial owner, connect one UniFi OS console and review discovered sites. A fresh connection initially discovers/selects available sites; explicitly deselect anything outside your intended monitoring scope. Reconnecting an existing profile preserves prior selections. Readers see only granted sites; ownership is distinct from the general administrator role.

## Daily Monitoring

**Network Overview** summarizes site health, WAN, infrastructure, clients, alerts and recent history. Open a site for its local context. Freshness labels distinguish current observations from stale or unavailable data. A high numeric score does not override a critical site condition; incomplete coverage is not silently treated as healthy.

**WAN / Internet** shows observed uplinks and metrics. **Site-to-Site / SD-WAN** shows discovered connections, member sites, controller state, measurement coverage and available histories. Choose a connection explicitly where several exist. RX/TX direction is normalized to the displayed local side; the UI identifies measurement provenance.

**Gateways, Switches and Access Points** list infrastructure and link to device details. Ports, radios, events, alerts and clients appear only when supported/reported. **Clients** provides wired/wireless context and site-scoped client details. **Wi-Fi Health** summarizes reported wireless conditions.

**Topology** renders discovered relationships, not a fabricated mesh. Search, zoom, fit/reset and selectively include clients. Follow actual inter-site links into their detail view.

## Alerts And Operations

**Needs Attention** groups useful signals, capacity/update observations and correlated incidents. Correlation is based on observed timing/context, not proven causation.

**Alerts** supports filtering, details and acknowledgement. The sidebar badge counts unacknowledged threshold alerts from the current overview feed, excluding anomaly entries. Acknowledgement removes an item from that pending badge, but does not clear the underlying firing condition or delete history. A critical acknowledgement does not make the monitored device healthy. The overview feed is bounded; use the Alerts view for the complete filtered list.

**Events** offers filtering and device/site context. **Configuration Changes** shows observed snapshots/differences, not a tool for writing UniFi configuration. **Device Availability** separates observed online/offline periods from gaps and displays maintenance context. It is not contractual SLA accounting.

## Charts

Use range controls to inspect recent or rolled-up history. Linked charts coordinate cursor/zoom for comparisons. Missing samples remain gaps instead of becoming fabricated zero values. Min/max envelopes preserve spikes in aggregated data; coverage describes how much was actually observed.

## Wallboard

Open **Wallboard** for a fullscreen monitoring view with site health/WAN, inter-site matrix, live link traffic, probe status and incidents. Exit fullscreen to return to normal navigation. It uses the same account/site permissions and data availability rules.

## Settings

**My Account** handles account-specific actions. Owner-only **UniFi OS Connection** updates the controller profile; leaving the key blank reuses a saved key only for the same controller. Secret contents are never echoed back in the form.

**Monitoring Sites** controls collection scope. **Users** manages site-scoped permissions for authorized administrators. **Maintenance** schedules one-time/daily/weekly windows, respecting the selected site's timezone. **Notifications** configures supported ntfy or Telegram channels, severity and tests; tokens are sensitive. **Diagnostics** exposes collector health and measurement availability to the owner.

Keep the UI on trusted networks. Do not publish screenshots, diagnostics exports or event details containing real client names, addresses or tokens.
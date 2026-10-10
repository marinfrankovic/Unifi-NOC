export const NA = "NOT AVAILABLE FROM UNIFI";

export function entityDeviceMac(entityId: string): string | null {
  const match = /^(dev|port|radio):([0-9a-f]{2}(?::[0-9a-f]{2}){5})(?::([^:]+))?$/i.exec(entityId);
  if (!match || (match[1].toLowerCase() === "dev") !== (match[3] === undefined)) return null;
  return match[2].toLowerCase();
}

export function deviceMonitoringStatus(online: boolean, freshness: string, severity?: string | null): string {
  if (freshness !== "LIVE") return "UNKNOWN";
  if (!online) return "OFFLINE";
  if (severity === "critical") return "CRITICAL";
  if (severity === "warning") return "WARNING";
  return "HEALTHY";
}

export function fmtBps(v: number | null | undefined, digits = 1): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  const u = ["bps", "Kbps", "Mbps", "Gbps"];
  let i = 0;
  let x = Math.abs(v);
  while (x >= 1000 && i < u.length - 1) {
    x /= 1000;
    i++;
  }
  return `${(Math.sign(v) * x).toFixed(i === 0 ? 0 : digits)} ${u[i]}`;
}

export function fmtMs(v: number): string {
  return `${Math.abs(v) < 10 ? v.toFixed(1) : v.toFixed(0)} ms`;
}

export function fmtDbm(v: number): string {
  return `${v.toFixed(0)} dBm`;
}

export function fmtBytes(v: number | null | undefined): string {
  if (v === null || v === undefined) return "—";
  const u = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let x = v;
  while (x >= 1024 && i < u.length - 1) {
    x /= 1024;
    i++;
  }
  return `${x.toFixed(i ? 1 : 0)} ${u[i]}`;
}

export function fmtNum(v: number | null | undefined, digits = 0, unit = ""): string {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return `${v.toFixed(digits)}${unit}`;
}

export function fmtDuration(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return "—";
  sec = Math.max(0, Math.floor(sec));
  const d = Math.floor(sec / 86400);
  const h = Math.floor((sec % 86400) / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  if (d) return `${d}d ${String(h).padStart(2, "0")}h`;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

export function fmtTime(t: string | number | null | undefined, withDate = false): string {
  if (t === null || t === undefined || t === "") return "—";
  const d = typeof t === "number" ? new Date(t) : new Date(t);
  if (Number.isNaN(d.getTime())) return "—";
  const today = new Date().toDateString() === d.toDateString();
  return d.toLocaleString(undefined, {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    ...(withDate || !today ? { day: "2-digit", month: "short" } : {}),
  });
}

export function ago(t: string | number | null | undefined): string {
  if (t === null || t === undefined) return "—";
  const ms = typeof t === "number" ? t : new Date(t).getTime();
  return `${fmtDuration((Date.now() - ms) / 1000)} ago`;
}

export const BAND: Record<string, string> = { ng: "2.4 GHz", na: "5 GHz", "6e": "6 GHz" };

export const METRIC_UNITS: Record<string, { label: string; fmt: (v: number) => string }> = {
  rx_bps: { label: "RX", fmt: (v) => fmtBps(v) }, tx_bps: { label: "TX", fmt: (v) => fmtBps(v) },
  total_bps: { label: "Total", fmt: (v) => fmtBps(v) }, live_rx_bps: { label: "Download (live)", fmt: (v) => fmtBps(v) },
  live_tx_bps: { label: "Upload (live)", fmt: (v) => fmtBps(v) }, ua_rx_bps: { label: "Download (UniFi 5m avg)", fmt: (v) => fmtBps(v) },
  ua_tx_bps: { label: "Upload (UniFi 5m avg)", fmt: (v) => fmtBps(v) },
};

export function shortMac(mac?: string | null) {
  return mac ? mac.toUpperCase() : "—";
}

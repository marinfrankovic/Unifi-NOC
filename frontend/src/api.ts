export const BASE = import.meta.env.BASE_URL.replace(/\/$/, "");

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: () => void) {
  onUnauthorized = fn;
}

export async function api<T = any>(path: string, init: RequestInit = {}): Promise<T> {
  const res = await fetch(`${BASE}/api${path}`, {
    credentials: "same-origin",
    ...init,
    headers: {
      Accept: "application/json",
      "X-Requested-With": "noc-ui",
      ...(init.body ? { "Content-Type": "application/json" } : {}),
      ...(init.headers || {}),
    },
  });
  if (res.status === 401 && !path.startsWith("/auth/")) onUnauthorized?.();
  if (!res.ok) {
    let msg = res.statusText;
    try {
      const j = await res.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {
      /* not json */
    }
    throw new ApiError(res.status, msg);
  }
  return res.json();
}

export function qs(params: Record<string, string | number | boolean | undefined | null>) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== "") p.set(k, String(v));
  const s = p.toString();
  return s ? `?${s}` : "";
}

// ---------------------------------------------------------------- types (subset of API responses)
export type Freshness = { state: "LIVE" | "STALE" | "NO_DATA"; age_s: number | null; last_update?: number };
export type User = { username: string; role: string; is_owner: boolean; site_roles: Record<string, "admin" | "reader"> };
export type Reason = { text: string; impact: number; severity?: string; entity?: string };
export type Health = { health_score: number | null; status: string; reasons: Reason[]; ts?: number; stale?: boolean };
export type Point = [number, number | null, number | null, number | null];
export type Series = { entity_id: string; metric: string; points: Point[]; expected_cadence_ms?: number; coverage?: { observed: number; expected: number; percent: number } };
export type SeriesResp = { from: number; to: number; resolution: string; source: string; series: Series[] };
export type EventRow = {
  id: string; ts: string; site: string; slug?: string; site_name?: string; severity: string; device?: string; device_mac?: string;
  device_type?: string; event_type: string; category?: string; title?: string; message: string; client?: string; client_mac?: string;
  source: string; metadata: any;
};
export type AlertRow = {
  id: number; kind: string; rule_id?: string; site: string; slug?: string; site_name?: string; entity_type: string; entity_id: string;
  entity_name?: string; metric: string; condition: string; threshold?: number; value?: number; peak_value?: number; severity: string;
  state: string; message: string; started_at: string; updated_at: string; resolved_at?: string; duration_s?: number; acked_by?: string; metadata: any;
  can_ack?: boolean;
};
export type TunnelMember = { site: string; site_desc: string; tunnel_ip: string; wan_ip: string; port: number; router_ip?: string; subnets?: string[]; networks?: string[] };
export type Tunnel = {
  id: string; name: string; label: string; technology: string; tunnel_subnet: string; paused: boolean; connected: boolean | null;
  members: TunnelMember[]; status: Record<string, { connected: boolean; association_time: number; errors: any[]; warnings: any[] }>;
  local_site: string; remote_site: string; live: any; freshness: Freshness; config_freshness: Freshness;
  events?: EventRow[]; alerts?: AlertRow[]; entity_id?: string; capabilities?: Record<string, string>; gateway_hostkeys?: Record<string, any>;
};

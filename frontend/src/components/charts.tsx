import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useLocation } from "react-router-dom";
import * as echarts from "echarts/core";
import { BarChart, GraphChart, HeatmapChart, LineChart } from "echarts/charts";
import { DataZoomComponent, GridComponent, LegendComponent, MarkAreaComponent, MarkLineComponent, ToolboxComponent, TooltipComponent, VisualMapComponent } from "echarts/components";
import { CanvasRenderer } from "echarts/renderers";
import { api, EventRow, qs, SeriesResp } from "../api";
import { fmtTime } from "../format";
import { useTheme } from "../theme";
import { Drawer, Empty, EventDetail, Range } from "./ui";

echarts.use([LineChart, BarChart, HeatmapChart, GraphChart, GridComponent, TooltipComponent, DataZoomComponent, LegendComponent,
  MarkLineComponent, MarkAreaComponent, VisualMapComponent, ToolboxComponent, CanvasRenderer]);

export function cssVar(name: string) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

export const PALETTE = ["#38bdf8", "#a78bfa", "#34d399", "#fbbf24", "#f472b6", "#60a5fa", "#fb923c", "#2dd4bf", "#e879f9", "#a3e635"];

const chartGroups = new Map<string, number>();
export function EChart({ option, height = 260, onEvents, className = "chart", group }: {
  option: any; height?: number | string; onEvents?: Record<string, (p: any) => void>; className?: string; group?: string;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const inst = useRef<echarts.ECharts | null>(null);
  const { theme } = useTheme();
  useEffect(() => {
    if (!ref.current) return;
    inst.current = echarts.init(ref.current, undefined, { renderer: "canvas" });
    if (group) { inst.current.group = group; chartGroups.set(group, (chartGroups.get(group) || 0) + 1); echarts.connect(group); }
    const ro = new ResizeObserver(() => inst.current?.resize());
    ro.observe(ref.current);
    return () => { ro.disconnect(); inst.current?.dispose(); inst.current = null; if (group) { const count = (chartGroups.get(group) || 1) - 1; if (count) chartGroups.set(group, count); else { chartGroups.delete(group); echarts.disconnect(group); } } };
  }, [theme, group]);
  useEffect(() => {
    inst.current?.setOption({ ...option, ...(option?.tooltip ? { tooltip: { ...option.tooltip, confine: true, extraCssText: 'max-width: calc(100vw - 48px); white-space: normal; overflow-wrap: anywhere;' } } : {}) }, { replaceMerge: ["series", "xAxis", "yAxis", "visualMap"], lazyUpdate: true });
  }, [option, theme, group]);
  useEffect(() => {
    const c = inst.current;
    if (!c || !onEvents) return;
    Object.entries(onEvents).forEach(([k, fn]) => c.on(k, fn));
    return () => { Object.keys(onEvents).forEach((k) => c.off(k)); };
  }, [onEvents, theme, group]);
  return <div ref={ref} className={className} style={{ height }} data-chart-group={group} />;
}

export type SeriesSpec = {
  entity: string; metric: string; label: string; color?: string; area?: boolean; axis?: 0 | 1; type?: "line" | "bar"; step?: boolean; stack?: string; scale?: number;
};
export type AxisSpec = { fmt: (v: number) => string; name?: string; min?: number; max?: number; interval?: number; minInterval?: number };

const SEV_COLOR: Record<string, string> = { critical: "#ef4444", high: "#fb7185", medium: "#f59e0b", low: "#94a3b8", info: "#60a5fa" };

function withGaps(points: [number, number | null][], cadence?: number): [number, number | null][] {
  if (points.length < 2) return points;
  const d: number[] = [];
  for (let i = 1; i < points.length; i++) d.push(points[i][0] - points[i - 1][0]);
  const sorted = [...d].sort((a, b) => a - b);
  const med = sorted[Math.floor(sorted.length / 2)];
  const out: [number, number | null][] = [points[0]];
  for (let i = 1; i < points.length; i++) {
    if (points[i][0] - points[i - 1][0] > (cadence ? cadence * 2.5 : Math.max(med * 3.5, 20000))) out.push([points[i - 1][0] + 1, null]);
    out.push(points[i]);
  }
  return out;
}

/** Time-series chart backed by /api/metrics. Gaps in collection are shown as gaps, never interpolated. */
export function TimeSeries({ title, series, range, from, to, axes, height = 240, events, refreshMs, legend = true, emptyText, extraMarkAreas, group }: {
  title?: string; series: SeriesSpec[]; range?: Range; from?: number; to?: number; axes: AxisSpec[]; height?: number;
  events?: { site?: string; types?: string; min_severity?: string; device?: string; client?: string } | false; refreshMs?: number; legend?: boolean;
  emptyText?: string; extraMarkAreas?: [number, number][]; group?: string;
}) {
  const { pathname } = useLocation();
  const ents = [...new Set(series.map((s) => s.entity))].join(",");
  const mets = [...new Set(series.map((s) => s.metric))].join(",");
  const refetch = refreshMs ?? (range === "15m" || range === "1h" ? 15000 : range === "6h" ? 30000 : 60000);
  const q = useQuery({
    queryKey: ["metrics", ents, mets, range, from, to],
    queryFn: () => api<SeriesResp>(`/metrics${qs({ entity: ents, metric: mets, range: from ? undefined : range, from, to })}`),
    refetchInterval: refetch, enabled: !!ents,
  });
  const evq = useQuery({
    queryKey: ["events-overlay", events && JSON.stringify(events), q.data?.from, q.data?.to],
    queryFn: () => api<{ items: EventRow[] }>(`/events${qs({ ...(events || {}), type: events ? events.types : undefined, from: q.data!.from, to: q.data!.to, limit: 300, min_severity: events ? events.min_severity ?? "medium" : undefined })}`),
    enabled: !!events && !!q.data, refetchInterval: refetch,
  });
  const [openEvent, setOpenEvent] = useState<EventRow | null>(null);
  const { theme } = useTheme();
  const option = useMemo(() => {
    if (!q.data) return null;
    const text = cssVar("--muted"), grid = cssVar("--grid");
    const lookup = new Map(q.data.series.map((s) => [`${s.entity_id}|${s.metric}`, s]));
    const evs = evq.data?.items || [];
    const out = series.map((s, i) => {
      const data = lookup.get(`${s.entity}|${s.metric}`)?.points.map((p) => [p[0], p[1] === null ? null : p[1] * (s.scale ?? 1)] as [number, number | null]) || [];
      const color = s.color || PALETTE[i % PALETTE.length];
      return {
        name: s.label, type: s.type || "line", data: s.type === "bar" ? data : withGaps(data, lookup.get(`${s.entity}|${s.metric}`)?.expected_cadence_ms), yAxisIndex: s.axis || 0, showSymbol: false, step: s.step ? "end" : undefined,
        stack: s.stack, smooth: false, connectNulls: false, sampling: "lttb", lineStyle: { width: 1.6, color }, itemStyle: { color },
        areaStyle: s.area ? { opacity: 0.12, color } : undefined, emphasis: { focus: "series" },
        markLine: i === 0 && evs.length ? {
          symbol: ["none", "circle"], symbolSize: 7, silent: false, animation: false,
          label: { show: false },
          data: evs.map((e) => ({ xAxis: new Date(e.ts).getTime(), name: e.message, value: e.id, lineStyle: { color: SEV_COLOR[e.severity] || "#94a3b8", type: "dashed", width: 1 } })),
          tooltip: { formatter: (p: any) => { const e = evs.find((x) => x.id === p.value); return e ? `<b>${escapeHtml(e.event_type)}</b><br/>${fmtTime(e.ts, true)}<br/>${escapeHtml(e.message)}<br/><i>click for details</i>` : ""; } },
        } : undefined,
        markArea: i === 0 && extraMarkAreas?.length ? { silent: true, itemStyle: { color: "rgba(239,68,68,0.12)" }, data: extraMarkAreas.map(([a, b]) => [{ xAxis: a }, { xAxis: b }]) } : undefined,
      };
    });
    const bounds = q.data.resolution === 'raw' ? [] : series.flatMap((spec, index) => {
      if (spec.type === 'bar' || spec.step) return [];
      const source = lookup.get(`${spec.entity}|${spec.metric}`);
      return ([2, 3] as const).map(column => ({ name: `${spec.label} (${column === 2 ? 'min' : 'max'})`, type: 'line', yAxisIndex: spec.axis || 0,
        data: withGaps((source?.points || []).map(point => [point[0], point[column] == null ? null : point[column]! * (spec.scale ?? 1)]), source?.expected_cadence_ms),
        showSymbol: false, connectNulls: false, sampling: column === 2 ? 'min' : 'max', lineStyle: { width: 1, type: 'dashed', opacity: .5 }, color: spec.color || PALETTE[index % PALETTE.length] }));
    });
    const hasData = out.some((s) => s.data.some((p: any) => p[1] !== null));
    return {
      hasData,
      opt: {
        animation: false, color: PALETTE,
        grid: { left: 8, right: axes.length > 1 ? 8 : 16, top: legend ? 30 : 12, bottom: 36, containLabel: true },
        legend: legend ? { type: 'scroll', data: series.map(spec => spec.label), top: 0, left: 0, right: 0, textStyle: { color: text }, pageTextStyle: { color: text }, icon: "roundRect", itemWidth: 12, itemHeight: 4 } : undefined,
        tooltip: {
          trigger: "axis", confine: true, backgroundColor: cssVar("--panel"), borderColor: cssVar("--border"), textStyle: { color: cssVar("--text") },
          valueFormatter: undefined,
          formatter: (ps: any[]) => {
            if (!ps?.length) return "";
            const head = fmtTime(ps[0].axisValue, true);
            return head + "<br/>" + ps.filter((p) => p.componentType === "series").map((p) => {
              const ax = axes[[...out, ...bounds][p.seriesIndex]?.yAxisIndex || 0];
              const v = Array.isArray(p.value) ? p.value[1] : p.value;
              return `${p.marker}${escapeHtml(p.seriesName)}: <b>${v === null || v === undefined ? "no data" : escapeHtml(ax.fmt(v))}</b>`;
            }).join("<br/>");
          },
        },
        xAxis: { type: "time", min: q.data.from, max: q.data.to, axisLabel: { color: text, hideOverlap: true }, axisLine: { lineStyle: { color: grid } }, splitLine: { show: false } },
        axisPointer: { link: [{ xAxisIndex: 'all' }], label: { show: true } },
        yAxis: axes.map((a) => ({ type: "value", name: a.name, min: a.min, max: a.max, interval: a.interval ?? (a.min === 0 && a.max === 1 ? 1 : undefined),
          minInterval: a.minInterval ?? (a.fmt(0.5) === a.fmt(0) || a.fmt(0.5) === a.fmt(1) ? 1 : undefined),
          nameTextStyle: { color: text }, axisLabel: { color: text, formatter: (v: number) => a.fmt(v) }, splitLine: { lineStyle: { color: grid } }, scale: a.min === undefined })),
        dataZoom: [{ type: "inside", filterMode: "none" }, { type: "slider", height: 16, bottom: 4, borderColor: "transparent", textStyle: { color: text }, filterMode: "none", brushSelect: true }],
        series: [...out, ...bounds],
      },
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.data, evq.data, theme, JSON.stringify(series), axes.length, legend, extraMarkAreas]);
  const onEvents = useMemo(() => ({
    click: (p: any) => {
      if (p.componentType === "markLine" && p.data?.value) {
        const e = evq.data?.items.find((x) => x.id === p.data.value);
        if (e) setOpenEvent(e);
      }
    },
  }), [evq.data]);
  return (
    <div>
      {title && <div className="row" style={{ marginBottom: 4 }}><h3>{title}</h3>{q.data && <span className="small muted" style={{ marginLeft: "auto" }}>resolution: {q.data.resolution}</span>}</div>}
      {q.isError ? <Empty>Could not load metrics</Empty> : !option ? <div style={{ height }} className="empty">Loading…</div> :
        !option.hasData ? <div style={{ height, display: "grid", placeItems: "center" }}><span className="na">{emptyText || "No data collected in this window"}</span></div> :
            <EChart option={option.opt} height={height} onEvents={onEvents} group={group || `history:${pathname}:${range || `${from}:${to}`}`} />}
          {q.data && <div className="small muted chart-metadata">{q.data.resolution} · {q.data.resolution !== 'raw' ? 'average with min/max bounds · ' : ''}{series.map(spec => { const source = q.data.series.find(item => item.entity_id === spec.entity && item.metric === spec.metric); return `${spec.label}: ${source?.coverage ? source.coverage.percent + '% observed slots' : source ? 'coverage unavailable' : 'no samples'}`; }).join(' · ')}</div>}
      {openEvent && <Drawer onClose={() => setOpenEvent(null)}><EventDetail e={openEvent} /></Drawer>}
    </div>
  );
}

export function escapeHtml(s: string) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]!));
}

export function Sparkline({ entity, metric, range = "1h", color = "#38bdf8" }: { entity: string; metric: string; range?: Range; color?: string }) {
  const q = useQuery({ queryKey: ["spark", entity, metric, range], queryFn: () => api<SeriesResp>(`/metrics${qs({ entity, metric, range })}`), refetchInterval: 60000 });
  const pts = q.data?.series[0]?.points.map((p) => [p[0], p[1]]) || [];
  if (!pts.length) return <span className="na">no data</span>;
  return (
    <EChart className="spark" height={30} option={{
      animation: false, grid: { left: 0, right: 0, top: 2, bottom: 2 }, xAxis: { type: "time", show: false }, yAxis: { type: "value", show: false, scale: true },
      series: [{ type: "line", data: pts, showSymbol: false, lineStyle: { width: 1.4, color }, areaStyle: { opacity: 0.12, color } }],
    }} />
  );
}

export function Heatmap({ entityType, metric, site, range, names, height = 220, max = 100, fmt }: {
  entityType: string; metric: string; site?: string; range: Range; names: Record<string, string>; height?: number; max?: number; fmt?: (v: number) => string;
}) {
  const q = useQuery({
    queryKey: ["heatmap", entityType, metric, site, range],
    queryFn: () => api<{ bucket: string; series: Record<string, [number, number | null][]> }>(`/metrics/heatmap${qs({ entity_type: entityType, metric, site, range })}`),
    refetchInterval: 120000,
  });
  const { theme } = useTheme();
  const option = useMemo(() => {
    if (!q.data) return null;
    const ents = Object.keys(q.data.series).filter((e) => names[e]);
    const times = [...new Set(ents.flatMap((e) => q.data!.series[e].map((p) => p[0])))].sort((a, b) => a - b);
    const data: [number, number, number | null][] = [];
    ents.forEach((e, yi) => q.data!.series[e].forEach(([t, v]) => data.push([times.indexOf(t), yi, v])));
    const text = cssVar("--muted");
    return {
      empty: !ents.length,
      opt: {
        animation: false, grid: { left: 8, right: 16, top: 8, bottom: 50, containLabel: true },
        tooltip: { formatter: (p: any) => `${escapeHtml(names[ents[p.value[1]]])}<br/>${fmtTime(times[p.value[0]], true)}: <b>${p.value[2] === null ? "no data" : escapeHtml(String(fmt ? fmt(p.value[2]) : p.value[2]))}</b>` },
        xAxis: { type: "category", data: times.map((t) => fmtTime(t, true)), axisLabel: { color: text, hideOverlap: true } },
        yAxis: { type: "category", data: ents.map((e) => names[e]), axisLabel: { color: text } },
        visualMap: { min: 0, max, calculable: true, orient: "horizontal", left: "center", bottom: 0, itemHeight: 120, textStyle: { color: text },
          inRange: { color: ["#0f3d2e", "#22c55e", "#facc15", "#f97316", "#ef4444"] } },
        series: [{ type: "heatmap", data, emphasis: { itemStyle: { borderColor: "#fff", borderWidth: 1 } } }],
      },
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [q.data, theme, JSON.stringify(names)]);
  if (!option) return <div className="empty" style={{ height }}>Loading…</div>;
  if (option.empty) return <div style={{ height, display: "grid", placeItems: "center" }}><span className="na">No rollup data yet (5-minute aggregates appear after the first refresh)</span></div>;
  return <EChart option={option.opt} height={height} />;
}

"""Collector entrypoint: python -m noc.collector"""
import asyncio
import logging
import signal
import time

from ..config import settings
from ..db import connect, kv_get, kv_set, migrate
from ..unifi import UniFiClient
from .alerts import AlertEngine, detect_anomalies, seed_rules
from .health import overall_health, site_health
from .live import LiveCollector
from .poll import (Ctx, discover_capabilities, discover_sites, event_rates, housekeeping, poll_clients, poll_devices, poll_events,
                   poll_known_clients, poll_s2s, poll_topology, poll_wan_history)
from .poll import refresh_selection, process_site_refresh
from .store import MetricBuffer, State, record_source, utcnow
from .setup import apply_profile, load_profile, process_job
from ..notifications import process_notifications

log = logging.getLogger("noc.collector")
STARTED = time.time()


async def guarded(ctx: Ctx, name: str, interval: float, coro, extra=None):
    t0 = time.perf_counter()
    try:
        res = await coro
        await record_source(ctx.state, ctx.pool, name, interval, True, (time.perf_counter() - t0) * 1000,
                            extra=extra(res) if extra and res is not None else None)
        return res
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        log.warning("%s failed: %s", name, exc, exc_info=log.isEnabledFor(logging.DEBUG))
        await record_source(ctx.state, ctx.pool, name, interval, False, (time.perf_counter() - t0) * 1000, f"{type(exc).__name__}: {exc}")
        return None


async def periodic(name: str, interval: float, fn, stop: asyncio.Event, initial_delay: float = 0):
    if initial_delay:
        await asyncio.sleep(initial_delay)
    while not stop.is_set():
        t0 = time.monotonic()
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("loop %s crashed; continuing", name)
        elapsed = time.monotonic() - t0
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(interval - elapsed, 0.5))
        except asyncio.TimeoutError:
            pass


class Collector:
    def __init__(self, ctx: Ctx):
        self.ctx = ctx
        self.engine = AlertEngine(ctx)
        self.live = LiveCollector(ctx)
        self.slow_runs = 0

    async def fast(self):
        ctx, st = self.ctx, self.ctx.state
        await process_site_refresh(ctx)
        await refresh_selection(ctx)
        buf = MetricBuffer()
        await asyncio.gather(*(guarded(ctx, f"devices.{s}", settings.poll_interval, poll_devices(ctx, s, buf)) for s in st.sites))
        await guarded(ctx, "s2s", settings.poll_interval, poll_s2s(ctx, buf))
        for s in st.sites:
            await event_rates(ctx, s, buf)
        await buf.flush(ctx.pool)
        await self.health()
        self.stale_ratios()
        await guarded(ctx, "alerts", settings.poll_interval, self.engine.evaluate())

    async def health(self):
        ctx, st = self.ctx, self.ctx.state
        buf = MetricBuffer()
        now = utcnow()
        scores = {}
        for s in st.sites:
            wan = ctx.flags.get(f"wanif:{s}") or {}
            h = site_health(st, s, wan.get("entity_id"))
            h["ts"] = time.time()
            scores[s] = h
            st.health[s] = h
            await kv_set(ctx.pool, f"health.{s}", h)
            if h["health_score"] is not None:
                buf.add(now, s, "site", f"site:{s}", "health_score", h["health_score"])
                st.put_current(s, "site", f"site:{s}", st.sites[s].description, "health_score", h["health_score"])
        tunnels = {k: v for k, v in st.health.items() if k.startswith("tunnel:")}
        overall = overall_health(scores, tunnels, {n: x.description for n, x in st.sites.items()})
        overall["ts"] = time.time()
        await kv_set(ctx.pool, "health.overall", overall)
        if overall["health_score"] is not None:
            buf.add(now, "_all", "site", "site:_all", "health_score", overall["health_score"])
        await buf.flush(ctx.pool)

    def stale_ratios(self):
        st = self.ctx.state
        now = time.time()
        for name, s in st.sources.items():
            ref = s.get("last_ok") or STARTED
            ratio = (now - ref) / max(s.get("interval") or 1, 1)
            st.put_current("_all", "noc", f"noc:source:{name}", f"Collector source {name}", "stale_ratio", ratio)

    async def clients(self):
        ctx, st = self.ctx, self.ctx.state
        await refresh_selection(ctx)
        buf = MetricBuffer()
        await asyncio.gather(*(guarded(ctx, f"clients.{s}", settings.client_poll_interval, poll_clients(ctx, s, buf)) for s in st.sites))
        await buf.flush(ctx.pool)

    async def events(self):
        ctx, st = self.ctx, self.ctx.state
        await refresh_selection(ctx)
        for s in list(st.sites):
            await guarded(ctx, f"events.{s}", settings.event_poll_interval, poll_events(ctx, s), extra=lambda n: {"last_inserted": n})

    async def slow(self):
        ctx, st = self.ctx, self.ctx.state
        await refresh_selection(ctx)
        self.slow_runs += 1
        await guarded(ctx, "sites", settings.slow_poll_interval, discover_sites(ctx))
        buf = MetricBuffer()
        for s in list(st.sites):
            await guarded(ctx, f"topology.{s}", settings.slow_poll_interval, poll_topology(ctx, s))
            await guarded(ctx, f"wanhistory.{s}", settings.slow_poll_interval, poll_wan_history(ctx, s, buf))
            await guarded(ctx, f"knownclients.{s}", settings.slow_poll_interval, poll_known_clients(ctx, s))
        await buf.flush(ctx.pool)
        await guarded(ctx, "capabilities", settings.slow_poll_interval, discover_capabilities(ctx))
        await guarded(ctx, "anomaly", settings.slow_poll_interval, detect_anomalies(ctx))
        await guarded(ctx, "housekeeping", settings.slow_poll_interval, housekeeping(ctx))

    async def live_tick(self):
        ctx = self.ctx
        await refresh_selection(ctx)
        res = await guarded(ctx, "live", settings.live_poll_interval, asyncio.wait_for(self.live.run_once(), timeout=30),
                            extra=lambda r: {"gateways": r.get("gateways"), "probes": r.get("probes")})
        u = ctx.unifi
        await kv_set(ctx.pool, "collector.heartbeat", {
            "ts": time.time(), "started": STARTED, "sites": list(ctx.state.sites), "unifi_requests": u.request_count,
            "unifi_errors": u.error_count, "unifi_last_latency_ms": round(u.last_latency_ms, 1),
            "live_ok": res is not None, "intervals": {"poll": settings.poll_interval, "clients": settings.client_poll_interval,
                                                       "events": settings.event_poll_interval, "slow": settings.slow_poll_interval,
                                                       "live": settings.live_poll_interval}})


async def main():
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("asyncssh").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    pool = await connect(max_size=6)
    await migrate(pool)
    await seed_rules(pool)
    if settings.ui_setup:
        await ui_main(pool)
        return
    unifi = UniFiClient()
    ctx = Ctx(State(), pool, unifi)
    await guarded(ctx, "sites", settings.slow_poll_interval * 12, discover_sites(ctx))
    col = Collector(ctx)
    await col.engine.load()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = [
        asyncio.create_task(periodic("fast", settings.poll_interval, col.fast, stop)),
        asyncio.create_task(periodic("clients", settings.client_poll_interval, col.clients, stop, 3)),
        asyncio.create_task(periodic("events", settings.event_poll_interval, col.events, stop, 5)),
        asyncio.create_task(periodic("live", settings.live_poll_interval, col.live_tick, stop, 4)),
        asyncio.create_task(periodic("slow", settings.slow_poll_interval, col.slow, stop, 8)),
    ]
    log.info("collector running (poll %ss, clients %ss, events %ss, live %ss, slow %ss)", settings.poll_interval,
             settings.client_poll_interval, settings.event_poll_interval, settings.live_poll_interval, settings.slow_poll_interval)
    await stop.wait()
    log.info("shutting down")
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await unifi.close()
    await pool.close()


async def ui_main(pool):
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    try:
        while not stop.is_set():
            await process_job(pool)
            await process_notifications(pool)
            encrypted, profile = await load_profile(pool)
            if not profile:
                await kv_set(pool, 'collector.heartbeat', {"ts": time.time(), "state": "AWAITING SETUP", "sites": [], "intervals": {"live": 5}})
                try:
                    await asyncio.wait_for(stop.wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
                continue
            apply_profile(profile)
            unifi = UniFiClient(profile)
            ctx = Ctx(State(), pool, unifi)
            await guarded(ctx, 'sites', settings.slow_poll_interval, discover_sites(ctx))
            collector = Collector(ctx)
            await collector.engine.load()
            cycle_stop = asyncio.Event()

            async def connection_watch():
                await process_job(pool)
                await process_notifications(pool)
                if await kv_get(pool, 'connection.profile') != encrypted:
                    cycle_stop.set()

            tasks = [asyncio.create_task(periodic('fast', settings.poll_interval, collector.fast, cycle_stop)),
                     asyncio.create_task(periodic('clients', settings.client_poll_interval, collector.clients, cycle_stop, 3)),
                     asyncio.create_task(periodic('events', settings.event_poll_interval, collector.events, cycle_stop, 5)),
                     asyncio.create_task(periodic('live', settings.live_poll_interval, collector.live_tick, cycle_stop, 4)),
                     asyncio.create_task(periodic('slow', settings.slow_poll_interval, collector.slow, cycle_stop, 8)),
                     asyncio.create_task(periodic('setup', 2, connection_watch, cycle_stop))]
            waits = [asyncio.create_task(stop.wait()), asyncio.create_task(cycle_stop.wait())]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks + waits:
                    task.cancel()
                await asyncio.gather(*tasks, *waits, return_exceptions=True)
                await unifi.close()
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())

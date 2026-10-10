"""Render public screenshots using synthetic responses; all network requests are intercepted."""
import math
import os
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "frontend/dist"
OUTPUT = ROOT / "site/src/assets"
OUTPUT.mkdir(parents=True, exist_ok=True)
NOW = int(time.time() * 1000)
FRESH = {"state": "LIVE", "age_s": 0, "last_update": NOW}
SITES = []
for index, slug in enumerate(("east", "west")):
    SITES.append({
        "name": slug, "slug": slug, "description": f"Demo {slug.title()}",
        "health": {"health_score": 100 if index == 0 else 96, "status": "HEALTHY" if index == 0 else "WARNING",
                   "reasons": [] if index == 0 else [{"text": "AP retries above preferred threshold", "severity": "warning", "impact": 4}],
                   "coverage": {"measured": 7, "total": 7}},
        "health_freshness": FRESH,
        "wan": {"up": True, "ip": f"192.0.2.{10 + index}", "isp": "Example Fiber", "entity_id": f"wan:{slug}",
                "freshness": FRESH, "now": {"latency_ms": 8 + index * 3, "live_rx_bps": 42000000 + index * 7000000, "live_tx_bps": 12000000}},
        "devices": {"gateway": {"online": 1, "offline": 0}, "switch": {"online": 3 - index, "offline": 0}, "ap": {"online": 4 - index, "offline": 0}},
        "clients": {"online": 38 - index * 12, "wifi": 27 - index * 7, "wired": 11 - index * 5},
        "alerts": {"critical": 0, "warning": index}, "incidents": [],
        "gateway": {"name": f"Demo {slug.title()} Gateway", "online": True, "uptime": 864000,
                    "now": {"cpu_pct": 18 + index * 3, "mem_pct": 36 + index * 4}},
    })
ALERT = {"id": 1, "kind": "threshold", "site": "west", "slug": "west", "site_name": "Demo West",
         "entity_type": "device", "entity_id": "dev:02:00:00:00:20:01", "entity_name": "Demo West AP",
         "metric": "retry_pct", "severity": "warning", "state": "firing", "condition": "> 20", "value": 22,
         "threshold": 20, "message": "Demo West AP: elevated retry rate", "started_at": "2026-10-10T12:00:00Z"}
TUNNEL = {"id": "demo-link", "name": "Demo mesh", "label": "Demo East / Demo West", "technology": "WireGuard SD-WAN",
          "connected": True, "local_site": "east", "remote_site": "west", "freshness": FRESH, "config_freshness": FRESH,
          "members": [{"site": slug, "site_desc": f"Demo {slug.title()}"} for slug in ("east", "west")],
          "live": {"health_score": 100, "status": "HEALTHY", "latency_ms": 12.4, "jitter_ms": 0.7, "loss_pct_1m": 0,
                   "rx_bps": 18000000, "tx_bps": 8000000}}
RESPONSES = {
    "/auth/me": {"username": "demo-reader", "role": "viewer", "is_owner": False, "site_roles": {}},
    "/setup/status": {"needs_owner": False, "ui_setup": True},
    "/sites": [{"slug": site["slug"], "description": site["description"]} for site in SITES],
    "/status": {"collector": {"state": "OK", "freshness": FRESH}, "sources": [], "stale_sources": []},
    "/overview": {"overall": {"health_score": 98, "status": "WARNING", "freshness": FRESH,
                               "coverage": {"measured": 2, "total": 2}, "reasons": [{"text": "Demo West: wireless retry threshold", "severity": "warning", "impact": 2}]},
                  "sites": SITES, "tunnels": [TUNNEL], "alerts": [ALERT], "include_global_history": True},
    "/events": {"items": [], "types": [], "total": 0},
}


def metrics(query):
    start = int(query.get("from", [NOW - 3600000])[0])
    end = int(query.get("to", [NOW])[0])
    series = []
    for entity in query.get("entity", [""])[0].split(","):
        for metric in query.get("metric", [""])[0].split(","):
            points = []
            for index in range(91):
                phase = index / 8 + (1 if "west" in entity or metric == "tx_bps" else 0)
                if metric == "health_score":
                    value = 98 + 2 * math.sin(phase) if "west" not in entity else 95 + 1.5 * math.sin(phase)
                elif metric == "latency_ms":
                    value = 8 + (3 if "west" in entity else 0) + 1.3 * math.sin(phase)
                else:
                    value = (14000000 if metric == "rx_bps" else 6000000) + 3000000 * math.sin(phase) + index * 16000
                points.append([start + (end - start) * index // 90, value, value * 0.95, value * 1.05])
            series.append({"entity_id": entity, "metric": metric, "points": points, "expected_cadence_ms": max(1, (end - start) // 90)})
    return {"from": start, "to": end, "resolution": "raw", "source": "synthetic", "series": series}


def main():
    failures = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel=os.environ.get("NOC_BROWSER_CHANNEL", "msedge"), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1600, "height": 1000})
            page.on("pageerror", lambda error: failures.append(str(error)))

            def serve(route):
                url = urlsplit(route.request.url)
                if url.hostname != "noc-demo.invalid":
                    failures.append("Unexpected external request")
                    route.abort()
                    return
                if url.path.startswith("/unifi/api/"):
                    endpoint = url.path.removeprefix("/unifi/api")
                    if endpoint == "/metrics":
                        route.fulfill(json=metrics(parse_qs(url.query)))
                    elif endpoint in RESPONSES:
                        route.fulfill(json=RESPONSES[endpoint])
                    else:
                        failures.append(f"Unexpected endpoint: {endpoint}")
                        route.fulfill(status=404, json={"detail": "Unknown demo endpoint"})
                    return
                asset = (DIST / url.path.removeprefix("/unifi/")).resolve()
                if not asset.is_relative_to(DIST.resolve()):
                    route.abort()
                elif asset.is_file():
                    kind = {".js": "application/javascript", ".css": "text/css", ".svg": "image/svg+xml"}.get(asset.suffix, "text/html")
                    route.fulfill(path=str(asset), content_type=kind)
                else:
                    route.fulfill(path=str(DIST / "index.html"), content_type="text/html")

            page.route("**/*", serve)
            for filename, width, height in (("overview.png", 1600, 1000), ("overview-mobile.png", 390, 920)):
                page.set_viewport_size({"width": width, "height": height})
                page.goto("https://noc-demo.invalid/unifi/")
                expect(page.get_by_role("heading", name="Network Overview", exact=True)).to_be_visible()
                page.wait_for_function("document.querySelector('.brand-mark')?.naturalWidth > 0 && document.querySelectorAll('canvas').length >= 2")
                if page.evaluate("document.documentElement.scrollWidth > innerWidth"):
                    raise AssertionError(f"Demo layout overflows at {width}px")
                page.screenshot(path=str(OUTPUT / filename), animations="disabled")
            if failures:
                raise AssertionError(failures)
            print("Synthetic overview screenshots PASS (desktop/mobile; network isolated)")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
import asyncio
import logging
import ssl
import time
from urllib.parse import urlsplit

import httpx

from .config import settings

log = logging.getLogger("noc.unifi")

_SECRET_PARTS = ("password", "passphrase", "secret", "token", "psk", "private", "x_mgmt_key", "api_key", "authkey")


def sanitize(obj):
    """Recursively drop credential-like fields so controller secrets never reach storage or the browser."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl.startswith("x_") or any(p in kl for p in _SECRET_PARTS):
                continue
            out[k] = sanitize(v)
        return out
    if isinstance(obj, list):
        return [sanitize(v) for v in obj]
    return obj


class UniFiError(Exception):
    pass


def controller_tls_context() -> ssl.SSLContext:
    if not settings.verify_tls or urlsplit(settings.controller_url).scheme != "https":
        raise UniFiError("Controller HTTPS with certificate verification is required; configure UNIFI_CA_FILE for a private CA")
    context = ssl.create_default_context(cafile=settings.controller_ca_file)
    if settings.controller_ca_file:
        context.verify_flags &= ~ssl.VERIFY_X509_STRICT
    return context


class UniFiClient:
    """Read-only client for the UniFi Network application (API-key auth)."""

    def __init__(self, profile: dict | None = None) -> None:
        profile = profile or {}
        key = profile.get("api_key", settings.api_key)
        url = profile.get("url", settings.controller_url)
        if not key:
            raise UniFiError("UNIFI_API_KEY is not configured")
        context = ssl.create_default_context(cadata=profile["ca_pem"]) if profile.get("ca_pem") else controller_tls_context()
        if profile.get("ca_pem"):
            context.verify_flags &= ~ssl.VERIFY_X509_STRICT
        self.server_name = profile.get("tls_server_name")
        self._http = httpx.AsyncClient(
            base_url=url,
            verify=context,
            trust_env=False,
            timeout=httpx.Timeout(20.0, connect=10.0),
            headers={"X-API-KEY": key, "Accept": "application/json", **({"Host": self.server_name} if self.server_name else {})},
            limits=httpx.Limits(max_connections=6, max_keepalive_connections=4),
        )
        self.request_count = 0
        self.error_count = 0
        self.last_latency_ms = 0.0
        # Never more than this many concurrent requests against the controller.
        self._sem = asyncio.Semaphore(3)

    async def close(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, body=None):
        async with self._sem:
            t0 = time.perf_counter()
            self.request_count += 1
            try:
                r = await self._http.request(method, path, json=body, extensions={"sni_hostname": self.server_name} if self.server_name else None)
            except httpx.HTTPError as exc:
                self.error_count += 1
                raise UniFiError(f"{method} {path}: {type(exc).__name__}: {exc}") from exc
            finally:
                self.last_latency_ms = (time.perf_counter() - t0) * 1000
            if r.status_code != 200:
                self.error_count += 1
                raise UniFiError(f"{method} {path}: HTTP {r.status_code}")
            if r.content[:1] == b"<":
                self.error_count += 1
                raise UniFiError(f"{method} {path}: controller returned HTML instead of JSON")
            return r.json()

    async def legacy(self, site: str, path: str):
        data = await self._call("GET", f"/proxy/network/api/s/{site}/{path}")
        if isinstance(data, dict) and data.get("meta", {}).get("rc") not in (None, "ok"):
            raise UniFiError(f"{path}: rc={data['meta'].get('rc')}")
        return data.get("data", []) if isinstance(data, dict) else data

    async def v2(self, site: str, path: str, body=None):
        return await self._call("POST" if body is not None else "GET", f"/proxy/network/v2/api/site/{site}/{path}", body)

    async def sites(self):
        legacy = await self._call("GET", "/proxy/network/api/self/sites")
        integ = await self._call("GET", "/proxy/network/integration/v1/sites")
        return legacy.get("data", []), integ.get("data", [])

    async def info(self):
        return await self._call("GET", "/proxy/network/integration/v1/info")

    async def sysinfo(self, site: str):
        rows = await self.legacy(site, "stat/sysinfo")
        return rows[0] if rows else {}

    async def mgmt_ssh_credentials(self, site: str) -> tuple[str, str] | None:
        """Device SSH credentials already configured on the controller. Kept in memory only."""
        rows = await self.legacy(site, "get/setting/mgmt")
        if not rows:
            return None
        m = rows[0]
        if not m.get("x_ssh_enabled", True) or not m.get("x_ssh_username") or not m.get("x_ssh_password"):
            return None
        return m["x_ssh_username"], m["x_ssh_password"]

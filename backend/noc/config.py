import json
import os
import re
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def _duration(text: str) -> int:
    """'48h' / '30d' / '90m' / '3600' -> seconds."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhdw]?)\s*", text or "")
    if not m:
        raise ValueError(f"invalid duration: {text!r}")
    mult = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
    return int(m.group(1)) * mult


def _database_url() -> str:
    value = _env("DATABASE_URL", "postgresql://noc@db:5432/noc") or ""
    password_file = _env("POSTGRES_PASSWORD_FILE")
    if password_file:
        parsed = urlsplit(value)
        if not parsed.password:
            password = Path(password_file).read_text().strip()
            value = urlunsplit((parsed.scheme, f"{quote(parsed.username or 'noc')}:{quote(password, safe='')}@{parsed.hostname}:{parsed.port or 5432}", parsed.path, parsed.query, parsed.fragment))
    return value


@dataclass
class Settings:
    ui_setup: bool = field(default_factory=lambda: (_env("UI_SETUP", "false") or "").lower() == "true")
    secret_directory: str = field(default_factory=lambda: _env("NOC_SECRET_DIRECTORY", "/var/lib/noc") or "/var/lib/noc")
    controller_url: str = field(default_factory=lambda: _env("UNIFI_CONTROLLER_URL", "https://controller.example.invalid").rstrip("/"))
    api_key: str = field(default_factory=lambda: _env("UNIFI_API_KEY", "") or "")
    verify_tls: bool = field(default_factory=lambda: (_env("UNIFI_VERIFY_TLS", "true") or "").lower() == "true")
    controller_ca_file: str | None = field(default_factory=lambda: _env("UNIFI_CA_FILE"))
    monitored_sites: list[str] = field(default_factory=lambda: [s.strip() for s in (_env("MONITORED_SITES", "") or "").split(",") if s.strip()])
    poll_interval: int = field(default_factory=lambda: int(_env("POLL_INTERVAL", "15")))
    client_poll_interval: int = field(default_factory=lambda: int(_env("CLIENT_POLL_INTERVAL", "30")))
    event_poll_interval: int = field(default_factory=lambda: int(_env("EVENT_POLL_INTERVAL", "60")))
    slow_poll_interval: int = field(default_factory=lambda: int(_env("SLOW_POLL_INTERVAL", "300")))
    live_poll_interval: int = field(default_factory=lambda: int(_env("LIVE_POLL_INTERVAL", "5")))
    gateway_ssh_enabled: bool = field(default_factory=lambda: (_env("GATEWAY_SSH_ENABLED", "false") or "").lower() == "true")
    probe_enabled: bool = field(default_factory=lambda: (_env("PROBE_ENABLED", "false") or "").lower() == "true")
    probe_local_site: str = field(default_factory=lambda: _env("PROBE_LOCAL_SITE", "") or "")
    probe_count: int = field(default_factory=lambda: int(_env("PROBE_COUNT", "5")))
    metrics_retention: int = field(default_factory=lambda: _duration(_env("METRICS_RETENTION", "48h")))
    metrics_retention_5m: int = field(default_factory=lambda: _duration(_env("METRICS_RETENTION_5M", "30d")))
    metrics_retention_1h: int = field(default_factory=lambda: _duration(_env("METRICS_RETENTION_1H", "365d")))
    log_retention: int = field(default_factory=lambda: _duration(_env("LOG_RETENTION", "90d")))
    database_url: str = field(default_factory=_database_url)
    alert_thresholds: dict = field(default_factory=lambda: json.loads(_env("ALERT_THRESHOLDS", "{}") or "{}"))
    admin_username: str = field(default_factory=lambda: _env("ADMIN_USERNAME", "admin") or "admin")
    admin_password: str = field(default_factory=lambda: _env("ADMIN_PASSWORD", "") or "")
    session_ttl_hours: int = field(default_factory=lambda: int(_env("SESSION_TTL_HOURS", "12")))
    session_cookie: str = field(default_factory=lambda: _env("SESSION_COOKIE", "noc_session") or "noc_session")
    web_health_url: str = field(default_factory=lambda: _env("WEB_HEALTH_URL", "http://web:8080/healthz") or "")
    log_level: str = field(default_factory=lambda: _env("LOG_LEVEL", "INFO") or "INFO")

    def site_matches(self, desc: str) -> bool:
        wanted = {s.lower() for s in self.monitored_sites}
        return (desc or "").strip().lower() in wanted


settings = Settings()

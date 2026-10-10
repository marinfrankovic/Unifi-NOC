"""Local security regressions; never contacts the live controller or database."""
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import ssl
import subprocess
import sys
import time
from types import SimpleNamespace
import unittest
from contextlib import asynccontextmanager
from urllib.parse import urlsplit
from datetime import datetime, timezone
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.dont_write_bytecode = True

import anyio
from fastapi import HTTPException, Request, Response
from noc.api import auth
from noc.api import setup as api_setup
from noc.api import main as api_main
from noc.api.access import can_admin_site, can_read_site, require_delegation, require_owner
from noc.collector.health import overall_health
from noc.collector.live import LiveCollector, tunnel_health
from noc.collector.poll import _ports, _radios, poll_s2s
from noc.collector.store import MetricBuffer, Site, State, utcnow
from noc.config import Settings
from noc.entities import entity_device_mac
from noc.unifi import UniFiError, controller_tls_context
from noc.maintenance import active_window
from noc.secrets import encrypt_config, decrypt_config


class Pool:
    def __init__(self, user=None):
        self.attempts = 0
        self.user = user
        self.lock = asyncio.Lock()
        self.writes = []

    @asynccontextmanager
    async def acquire(self):
        yield self

    def transaction(self):
        return self.lock

    async def fetchval(self, query, *args):
        await asyncio.sleep(0)
        return self.attempts

    async def fetchrow(self, query, *args):
        return self.user

    async def fetch(self, query, *args):
        return []

    async def execute(self, query, *args):
        self.writes.append((query, args))
        if query.startswith("INSERT INTO auth_events") and args[2] == "login_attempt":
            self.attempts += 1


class ConnectionSettingsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from noc.config import settings
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        secret_directory = patch.object(settings, 'secret_directory', self.directory.name)
        secret_directory.start()
        self.addCleanup(secret_directory.stop)
        self.profile = {'url': 'https://controller.invalid', 'api_key': 'fixture-secret', 'ca_pem': 'saved-private-ca',
                        'tls_server_name': 'unifi.local', 'gateway_ssh': True, 'probes': True, 'probe_site': 'Fixture a'}
        self.values = {'connection.profile': encrypt_config(self.profile), 'setup.complete': True}
        self.pool = Pool()
        self.owner = {'is_owner': True, 'role': 'admin'}
        router = api_setup.routes(lambda: self.pool, lambda: self.owner)
        self.status = next(route.endpoint for route in router.routes if route.name == 'connection_status')
        self.connect = next(route.endpoint for route in router.routes if route.name == 'connect')
        async def get_value(pool, key):
            return self.values.get(key)
        async def set_value(pool, key, value):
            self.values[key] = value
        for name, replacement in (('kv_get', get_value), ('kv_set', set_value)):
            mocked = patch.object(api_setup, name, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)

    async def test_status_prefills_only_non_secret_settings(self):
        result = await self.status(user=self.owner)
        self.assertEqual(result['configuration']['url'], self.profile['url'])
        self.assertEqual(result['configuration']['probe_site'], 'Fixture a')
        self.assertTrue(result['configuration']['gateway_ssh'])
        self.assertTrue(result['configuration']['probes'])
        self.assertTrue(result['configuration']['has_api_key'])
        self.assertTrue(result['configuration']['ca_configured'])
        self.assertNotIn('fixture-secret', json.dumps(result))
        self.assertNotIn('saved-private-ca', json.dumps(result))

    async def test_settings_update_reuses_key_and_certificate(self):
        original = self.values['connection.profile']
        body = api_setup.ConnectionCreate(url=self.profile['url'] + '/', gateway_ssh=False, probes=False, tls_server_name='unifi.local')
        await self.connect(body, user=self.owner)
        queued = decrypt_config(self.values['setup.connection_job']['encrypted'])
        self.assertEqual(queued['api_key'], self.profile['api_key'])
        self.assertEqual(queued['ca_pem'], self.profile['ca_pem'])
        self.assertFalse(queued['gateway_ssh'])
        self.assertFalse(queued['probes'])
        self.assertEqual(self.values['connection.profile'], original)

    async def test_new_controller_cannot_reuse_saved_secrets(self):
        with self.assertRaises(HTTPException) as error:
            await self.connect(api_setup.ConnectionCreate(url='https://other.invalid'), user=self.owner)
        self.assertEqual(error.exception.status_code, 400)
        self.assertNotIn('setup.connection_job', self.values)
        await self.connect(api_setup.ConnectionCreate(url='https://other.invalid', api_key='replacement'), user=self.owner)
        queued = decrypt_config(self.values['setup.connection_job']['encrypted'])
        self.assertEqual(queued['api_key'], 'replacement')
        self.assertEqual(queued['ca_pem'], '')

    async def test_first_connection_requires_key_and_status_has_no_configuration(self):
        self.values.clear()
        self.assertIsNone((await self.status(user=self.owner))['configuration'])
        with self.assertRaises(HTTPException) as error:
            await self.connect(api_setup.ConnectionCreate(url=self.profile['url']), user=self.owner)
        self.assertEqual(error.exception.status_code, 400)


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pool = Pool()
        self.request = Request({"type": "http", "method": "POST", "path": "/api/auth/login", "headers": [(b"x-forwarded-proto", b"https")],
                                "client": ("127.0.0.1", 1234), "scheme": "https"})
        self.limiter = patch.object(auth, "_LOGIN_LIMITER", anyio.CapacityLimiter(2))
        self.limiter.start()
        self.addCleanup(self.limiter.stop)

    async def attempt(self, handler=auth.login):
        try:
            await handler(self.pool, self.request, Response(), "review-fixture", "invalid-password")
        except HTTPException as error:
            return error.status_code
        return 200

    async def test_concurrent_attempts_are_reserved_atomically(self):
        with patch.object(auth, "verify_password", return_value=False):
            codes = await asyncio.gather(*(self.attempt(auth._login) for attempt in range(12)))
        self.assertEqual(codes.count(401), 10)
        self.assertEqual(codes.count(429), 2)
        self.assertEqual(self.pool.attempts, 10)
        self.assertTrue(any("pg_advisory_xact_lock" in query for query, args in self.pool.writes))

    async def test_burst_is_bounded_and_event_loop_remains_responsive(self):
        done = asyncio.Event()
        ticks = []

        async def heartbeat():
            while not done.is_set():
                await asyncio.sleep(0.005)
                ticks.append(1)

        monitor = asyncio.create_task(heartbeat())
        try:
            codes = await asyncio.gather(*(self.attempt() for attempt in range(12)))
        finally:
            done.set()
            await monitor
        self.assertEqual(codes.count(401), 2)
        self.assertEqual(codes.count(429), 10)
        self.assertGreater(len(ticks), 5)

    async def test_success_uses_hashed_token_and_secure_cookie(self):
        self.pool.user = {"username": "review-fixture", "role": "viewer", "password_hash": auth._DUMMY_HASH}
        response = Response()
        with patch.object(auth, "verify_password", return_value=True):
            user = await auth.login(self.pool, self.request, response, "review-fixture", "fixture-password")
        cookie = response.headers["set-cookie"]
        self.assertEqual(user["role"], "viewer")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        self.assertIn("SameSite=strict", cookie)
        token = cookie.split(";", 1)[0].split("=", 1)[1]
        session = next(args for query, args in self.pool.writes if query.startswith("INSERT INTO sessions"))
        self.assertEqual(session[0], auth._hash_token(token))
        self.assertEqual(self.pool.attempts, 1)
        self.assertIn(f"Max-Age={auth.settings.session_ttl_hours * 3600}", cookie)

    async def test_remember_me_uses_thirty_day_cookie_and_server_expiry(self):
        self.pool.user = {"username": "review-fixture", "role": "viewer", "password_hash": auth._DUMMY_HASH}
        response = Response()
        before = datetime.now(timezone.utc)
        with patch.object(auth, "verify_password", return_value=True):
            await auth.login(self.pool, self.request, response, "review-fixture", "fixture-password", remember_me=True)
        cookie = response.headers['set-cookie']
        self.assertIn('Max-Age=2592000', cookie)
        self.assertIn('HttpOnly', cookie)
        self.assertIn('Secure', cookie)
        self.assertIn('SameSite=strict', cookie)
        session = next(args for query, args in self.pool.writes if query.startswith('INSERT INTO sessions'))
        self.assertAlmostEqual((session[2] - before).total_seconds(), 2592000, delta=2)


class OperationsSafetyTests(unittest.TestCase):
    def test_daily_maintenance_preserves_local_hour_across_dst(self):
        window={'enabled':True,'repeat':'daily','timezone':'Europe/Amsterdam',
                'starts_at':datetime(2026,3,28,10,tzinfo=timezone.utc),'ends_at':datetime(2026,3,28,11,tzinfo=timezone.utc)}
        self.assertTrue(active_window(window,datetime(2026,3,29,9,30,tzinfo=timezone.utc)))
        self.assertFalse(active_window(window,datetime(2026,3,29,10,30,tzinfo=timezone.utc)))

    def test_weekly_and_disabled_maintenance(self):
        window={'enabled':True,'repeat':'weekly','timezone':'UTC',
                'starts_at':datetime(2026,10,10,10,tzinfo=timezone.utc),'ends_at':datetime(2026,10,10,11,tzinfo=timezone.utc)}
        self.assertTrue(active_window(window,datetime(2026,10,17,10,30,tzinfo=timezone.utc)))
        self.assertFalse(active_window(window,datetime(2026,10,18,10,30,tzinfo=timezone.utc)))
        window['enabled']=False
        self.assertFalse(active_window(window,datetime(2026,10,17,10,30,tzinfo=timezone.utc)))

    def test_encrypted_connection_roundtrip_and_tamper_rejection(self):
        from noc.config import settings
        from cryptography.exceptions import InvalidTag
        with TemporaryDirectory() as directory, patch.object(settings,'secret_directory',directory):
            value={'url':'https://controller.invalid','api_key':'fixture-secret'}
            encrypted=encrypt_config(value)
            self.assertNotIn('fixture-secret',encrypted)
            self.assertEqual(decrypt_config(encrypted),value)
            import base64
            damaged=bytearray(base64.b64decode(encrypted)); damaged[-1]^=1
            with self.assertRaises(InvalidTag):
                decrypt_config(base64.b64encode(damaged).decode())


class AccessTests(unittest.TestCase):
    def test_owner_can_see_all_sites_but_global_admin_role_is_not_ownership(self):
        self.assertTrue(can_read_site({"is_owner": True}, "new-site"))
        self.assertTrue(can_admin_site({"is_owner": True}, "new-site"))
        self.assertFalse(can_read_site({"role": "admin"}, "new-site"))
        with self.assertRaises(HTTPException):
            require_owner({"role": "admin"})

    def test_mixed_roles_are_explicit_and_default_deny(self):
        user = {"site_roles": {"a": "admin", "b": "reader"}}
        self.assertTrue(can_read_site(user, "a"))
        self.assertTrue(can_admin_site(user, "a"))
        self.assertTrue(can_read_site(user, "b"))
        self.assertFalse(can_admin_site(user, "b"))
        self.assertFalse(can_read_site(user, "c"))
        self.assertFalse(can_read_site({}, "a"))

    def test_site_admin_can_delegate_only_inside_administered_sites(self):
        user = {"site_roles": {"a": "admin", "b": "reader"}}
        require_delegation(user, {"a": "reader"})
        require_delegation(user, {"a": "admin"})
        for grants in ({"b": "reader"}, {"c": "admin"}, {"a": "owner"}, {}):
            with self.subTest(grants=grants), self.assertRaises(HTTPException):
                require_delegation(user, grants)


class EntityTests(unittest.TestCase):
    cases = {
        "dev:02:00:00:00:10:01": "02:00:00:00:10:01",
        "port:02:00:00:00:10:01:8": "02:00:00:00:10:01",
        "radio:F4:92:BF:A0:BD:0F:na": "f4:92:bf:a0:bd:0f",
        "client:am:02:00:00:00:10:01": None,
        "dev:9c": None,
        "port:02:00:00:00:10:01": None,
        "dev:02:00:00:00:10:01:8": None,
        "dev:02:00:00:00:10:01/extra": None,
    }

    def test_device_entities_preserve_the_full_mac(self):
        for entity, expected in self.cases.items():
            with self.subTest(entity=entity):
                self.assertEqual(entity_device_mac(entity), expected)

    def test_frontend_parser_matches_backend(self):
        source = (ROOT / "frontend/src/format.ts").read_text(encoding="utf-8")
        script = """
const ts = require('typescript');
let input = '';
process.stdin.on('data', chunk => input += chunk);
process.stdin.on('end', () => {
    const { source, cases, statuses } = JSON.parse(input);
    const compiled = ts.transpileModule(source, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
    const moduleExports = {};
    new Function('exports', compiled)(moduleExports);
    for (const [entity, expected] of Object.entries(cases)) {
        const actual = moduleExports.entityDeviceMac(entity);
        if (actual !== expected) throw new Error(`${entity}: ${actual} !== ${expected}`);
    }
    for (const [online, freshness, severity, expected] of statuses) {
        const actual = moduleExports.deviceMonitoringStatus(online, freshness, severity);
        if (actual !== expected) throw new Error(`${online}/${freshness}/${severity}: ${actual} !== ${expected}`);
    }
});
"""
        result = subprocess.run(["node", "-e", script], cwd=ROOT / "frontend",
                                input=json.dumps({"source": source, "cases": self.cases, "statuses": [
                                    [True, "LIVE", None, "HEALTHY"], [True, "LIVE", "warning", "WARNING"],
                                    [True, "LIVE", "critical", "CRITICAL"], [False, "LIVE", None, "OFFLINE"],
                                    [True, "STALE", None, "UNKNOWN"], [False, "NO_DATA", "critical", "UNKNOWN"]]}),
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)


class MonitoringApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.mac = "02:00:00:00:10:01"
        self.observed_at = utcnow()
        self.device = {"mac": self.mac, "site": "fixture", "name": "Fixture gateway", "device_type": "gateway",
                       "model": "Fixture", "online": True, "last_poll": self.observed_at,
                       "uplink_mac": None, "uplink_port": None, "ip": "192.0.2.1"}

        async def fetch(query, *args):
            if "FROM devices" in query:
                return [self.device]
            if "FROM sites" in query:
                return [{"name": "fixture", "slug": "fixture", "description": "Fixture site"}]
            if "FROM alerts" in query:
                return [{"entity_id": f"port:{self.mac}:8", "severity": "critical"}]
            return []

        async def state(pool, key):
            if key.startswith("wan."):
                return {"ts": self.observed_at.timestamp(), "up": True}
            if key.startswith("health."):
                return {"ts": self.observed_at.timestamp(), "health_score": 100, "status": "HEALTHY"}
            return None

        for mocked in (patch.dict(api_main.STATE, {"pool": SimpleNamespace(fetch=fetch)}),
                       patch.object(api_main, "kv_get", side_effect=state),
                       patch.object(api_main, "latest", new=AsyncMock(return_value={})),
                       patch.object(api_main, "tunnels_summary", new=AsyncMock(return_value=[]))):
            mocked.start()
            self.addCleanup(mocked.stop)

    async def test_port_alert_colours_the_full_device_mac(self):
        devices = await api_main.api_devices(user={"is_owner": True})
        self.assertEqual(devices[0]["alert"], "critical")
        self.assertEqual(devices[0]["freshness"]["state"], "LIVE")
        topology = await api_main.api_topology(clients=False, user={"is_owner": True})
        device = next(node for node in topology["nodes"] if node["id"] == self.mac)
        self.assertEqual(device["status"], "critical")

    async def test_stale_topology_does_not_report_healthy_nodes_or_wan(self):
        self.observed_at = self.observed_at.replace(year=2020)
        self.device["last_poll"] = self.observed_at
        topology = await api_main.api_topology(clients=False, user={"is_owner": True})
        self.assertTrue(all(node["status"] == "unknown" for node in topology["nodes"]))
        self.assertEqual(next(edge for edge in topology["edges"] if edge["kind"] == "wan")["status"], "unknown")


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_outages_keep_unknown_gaps_and_maintenance(self):
        from noc.api.insights import observed_intervals, maintenance_occurrences, correlated_incidents
        start = datetime(2026, 10, 10, tzinfo=timezone.utc).timestamp()
        stamp = lambda offset: datetime.fromtimestamp(start + offset, timezone.utc)
        periods = observed_intervals([{'first': stamp(10), 'last': stamp(25), 'state': 'OFFLINE'},
                                     {'first': stamp(100), 'last': stamp(115), 'state': 'ONLINE'}], start, start + 150, 15)
        self.assertEqual([item['state'] for item in periods], ['UNKNOWN', 'OFFLINE', 'UNKNOWN', 'ONLINE', 'UNKNOWN'])
        self.assertEqual(sum(item['duration_s'] for item in periods), 150)
        windows = [{'enabled': True, 'starts_at': stamp(20), 'ends_at': stamp(60), 'timezone': 'UTC', 'repeat': 'none', 'title': 'Upgrade', 'site': 'a'}]
        self.assertEqual(maintenance_occurrences(windows, start, start + 150)[0]['end'], int((start + 60) * 1000))
        root = {'id': 1, 'state': 'firing', 'entity_type': 'wan', 'metric': 'up', 'value': 0, 'site': 'a', 'started_at': stamp(20)}
        related = {'id': 2, 'state': 'firing', 'entity_type': 'switch', 'site': 'a', 'started_at': stamp(30)}
        other = {**related, 'id': 3, 'site': 'b'}
        incidents = correlated_incidents([root, related, other])
        self.assertEqual(len(incidents), 2)
        self.assertEqual(incidents[0]['related'][0]['id'], 2)
        serialized = correlated_incidents([{**item, 'started_at': item['started_at'].isoformat()} for item in (root, related, other)])
        self.assertEqual(len(serialized), 2)
        self.assertEqual(serialized[0]['related'][0]['id'], 2)

    async def test_probe_identity_survives_rename_and_rejects_ambiguity(self):
        state = State()
        state.sites = {name: Site(name, f'id-{name}', None, 'Duplicate', name) for name in ('a', 'b')}
        self.assertIsNone(state.probe_origin('Duplicate'))
        self.assertEqual(state.probe_origin('a'), 'a')
        self.assertEqual(state.probe_origin('id-b'), 'b')
        state.sites['a'].description = 'Renamed'
        self.assertEqual(state.probe_origin('a'), 'a')
        self.assertEqual(state.probe_origin('Renamed'), 'a')

    async def test_metric_history_keeps_spikes_and_reports_cadence(self):
        from noc.api.series import series
        now = time.time()
        pool = SimpleNamespace(fetch=AsyncMock(return_value=[{'entity_id': 'tunnel:fixture', 'metric': 'rx_bps',
            't': datetime.fromtimestamp(now - 60, timezone.utc), 'avg': 10, 'min': 0, 'max': 500}]))
        response = await series(pool, ['tunnel:fixture'], ['rx_bps'], now - 600, now)
        point = response['series'][0]
        self.assertEqual(point['points'][0][1:], [10, 0, 500])
        self.assertGreater(point['expected_cadence_ms'], 0)
        self.assertLess(point['coverage']['percent'], 100)
        pool.fetch.return_value = [{'entity_id': 'wan:a:WAN', 'metric': 'loss_pct', 't': datetime.fromtimestamp(now - 300, timezone.utc), 'avg': 0, 'min': 0, 'max': 1}]
        response = await series(pool, ['wan:a:WAN'], ['loss_pct'], now - 600, now)
        self.assertEqual(response['series'][0]['expected_cadence_ms'], 300000)
        self.assertEqual(response['series'][0]['coverage']['expected'], 2)

    async def test_remote_traffic_fallback_persists_local_direction(self):
        state = State()
        state.sites = {name: Site(name, f'native-{name}', None, f'Fixture {name}', name) for name in ('a', 'b')}
        ctx = SimpleNamespace(state=state, pool=SimpleNamespace(execute=AsyncMock()), event=AsyncMock())
        poller = LiveCollector.__new__(LiveCollector)
        poller.ctx = ctx
        poller.samples = {'b:wg0:rate': {'ts': time.time(), 'rx_bps': 12, 'tx_bps': 34, 'err_per_min': 2}}
        tunnel = {'local_site': 'a', 'remote_site': 'b', 'members': [{'site': 'a'}, {'site': 'b'}], 'connected': True, 'ts': time.time()}
        buffer = MetricBuffer()
        with patch.object(poller, '_tunnel_iface', side_effect=lambda site, peer: ('wg0', None) if site == 'b' else (None, None)):
            await poller._tunnel_rollup('fixture', tunnel, buffer, utcnow(), {})
        values = {row[4]: row[5] for row in buffer.rows}
        self.assertEqual((values['rx_bps'], values['tx_bps'], values['total_bps']), (34, 12, 46))
        self.assertEqual(state.get_current('tunnel:fixture', 'rx_bps'), 34)
        self.assertEqual(tunnel['live']['source_side'], 'remote')
        self.assertEqual(tunnel['live']['source_gateway'], 'b')
        self.assertEqual(values['source_remote'], 1)
        self.assertEqual(tunnel['live']['controller_state'], 'CONNECTED')
        self.assertIn('coverage', tunnel['live'])
        self.assertEqual(tunnel['live']['measurements']['rx_bps']['state'], 'AVAILABLE')

    async def test_incomplete_tunnel_member_status_is_unknown_not_up(self):
        state = State()
        state.sites = {name: Site(name, f"native-{name}", None, f"Fixture {name}", name) for name in ('a', 'b')}
        state.by_site_id = {f"native-{name}": name for name in ('a', 'b')}
        config = {"id": "fixture-group", "name": "Fixture", "connections": [{"connectionId": "fixture-link",
                  "sites": [{"siteId": "native-a"}, {"siteId": "native-b"}]}],
                  "status": [{"siteId": "native-a", "connections": [{"connectionId": "fixture-link", "connected": True}]}]}
        ctx = SimpleNamespace(state=state, pool=SimpleNamespace(execute=AsyncMock()),
                              unifi=SimpleNamespace(v2=AsyncMock(return_value=[config])), event=AsyncMock())
        buffer = MetricBuffer()
        await poll_s2s(ctx, buffer)
        self.assertIsNone(state.tunnels['fixture-link']['connected'])
        self.assertFalse(any(row[4] == 'connected' for row in buffer.rows))
        config['status'].append({"siteId": "native-b", "connections": [{"connectionId": "fixture-link", "connected": True}]})
        await poll_s2s(ctx, buffer)
        self.assertTrue(state.tunnels['fixture-link']['connected'])

    async def test_missing_counters_do_not_become_zero_rates(self):
        for port in ({"port_idx": 1}, {"port_idx": 1, "rx_errors": 0, "rx_dropped": 0}):
            state = State()
            state.counters["port:02:00:00:00:10:01:1:ctr"] = (time.monotonic() - 15, {"err": 0, "drop": 0})
            ctx = SimpleNamespace(state=state, flags={}, event=AsyncMock())
            buf = MetricBuffer()
            await _ports(ctx, "fixture", {"mac": "02:00:00:00:10:01", "port_table": [port]}, "switch", "Fixture", buf, utcnow())
            self.assertFalse({"err_per_min", "drop_per_min"} & {row[4] for row in buf.rows})

    async def test_genuine_zero_counters_remain_valid_rates(self):
        state = State()
        state.counters["port:02:00:00:00:10:01:1:ctr"] = (time.monotonic() - 15, {"err": 0, "drop": 0})
        ctx = SimpleNamespace(state=state, flags={}, event=AsyncMock())
        buf = MetricBuffer()
        port = {"port_idx": 1, "rx_errors": 0, "tx_errors": 0, "rx_dropped": 0, "tx_dropped": 0}
        await _ports(ctx, "fixture", {"mac": "02:00:00:00:10:01", "port_table": [port]}, "switch", "Fixture", buf, utcnow())
        values = {row[4]: row[5] for row in buf.rows}
        self.assertEqual(values["err_per_min"], 0)
        self.assertEqual(values["drop_per_min"], 0)

    async def test_other_airtime_requires_all_source_measurements(self):
        for radio, expected in (({"radio": "na", "cu_total": 30}, None),
                                ({"radio": "na", "cu_total": 30, "cu_self_rx": 10, "cu_self_tx": 5}, 15)):
            buf = MetricBuffer()
            _radios(State(), "fixture", {"mac": "02:00:00:00:10:01", "radio_table_stats": [radio]}, "Fixture", buf, utcnow())
            values = {row[4]: row[5] for row in buf.rows}
            self.assertEqual(values.get("cu_other"), expected)


class HealthTests(unittest.TestCase):
    def test_unsupported_temperatures_do_not_make_healthy_site_unknown(self):
        from noc.collector.health import site_health
        state = State()
        state.sources['devices.a'] = {'last_ok': time.time()}
        state.devices['fixture'] = {'_site': 'a', '_kind': 'ap', 'has_temperature': False}
        for metric, value in (('online', 1), ('cpu_pct', 10), ('mem_pct', 20)):
            state.put_current('a', 'ap', 'fixture', 'AP', metric, value)
        for metric in ('disconnects_15m', 'clients_poor_signal'):
            state.put_current('a', 'site', 'site:a', 'Site', metric, 0)
        health = site_health(state, 'a', None)
        self.assertEqual(health['status'], 'HEALTHY')
        self.assertEqual(health['coverage']['measured'], health['coverage']['total'])
        self.assertNotIn('ap temp_c', health['unavailable_checks'])
        state.put_current('a', 'ap', 'fixture', 'AP', 'temp_c', 40, ts=time.time() - 3600)
        self.assertIn('ap temp_c', site_health(state, 'a', None)['unavailable_checks'])
        state.put_current('a', 'radio', 'radio:fixture:ng', 'AP 2.4 GHz', 'cu_total', 10)
        state.put_current('a', 'radio', 'radio:fixture:ng', 'AP 2.4 GHz', 'tx_retries_pct', 30)
        health = site_health(state, 'a', None)
        self.assertEqual(health['health_score'], 95)
        self.assertEqual(health['status'], 'WARNING')
        self.assertEqual(overall_health({'a': health}, {}, {})['status'], 'WARNING')

    def test_site_reports_missing_applicable_checks(self):
        from noc.collector.health import site_health
        state = State()
        state.sources['devices.a'] = {'last_ok': time.time()}
        state.devices['fixture'] = {'_site': 'a', '_kind': 'gateway'}
        state.put_current('a', 'gateway', 'fixture', 'Gateway', 'online', 1)
        health = site_health(state, 'a', None)
        self.assertEqual(health['health_score'], 100)
        self.assertEqual(health['status'], 'UNKNOWN')
        self.assertIn('gateway cpu_pct', health['unavailable_checks'])
        self.assertLess(health['coverage']['measured'], health['coverage']['total'])

    def test_ping_failure_is_not_controller_disconnection(self):
        health = tunnel_health({'connected': True}, {'loss_pct_1m': 100}, [])
        self.assertEqual(health['status'], 'DEGRADED')
        self.assertEqual(tunnel_health({'connected': False}, {}, [])['status'], 'DOWN')
        self.assertEqual(tunnel_health({'connected': None}, {}, [])['status'], 'UNKNOWN')

    def test_partial_coverage_never_reports_healthy(self):
        result = overall_health({"am": {"health_score": 100, "status": "HEALTHY"},
                                 "zg": {"health_score": None, "status": "UNKNOWN"}}, {}, {})
        self.assertIsNone(result["health_score"])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["coverage"], {"measured": 1, "total": 2})

    def test_critical_site_cannot_be_averaged_away(self):
        result = overall_health({"am": {"health_score": 100, "status": "HEALTHY"},
                                 "zg": {"health_score": 60, "status": "CRITICAL"}}, {}, {})
        self.assertEqual(result["health_score"], 80)
        self.assertEqual(result["status"], "CRITICAL")

    def test_warning_site_remains_visible_in_overall_status(self):
        result = overall_health({"am": {"health_score": 100, "status": "HEALTHY"},
                                 "zg": {"health_score": 85, "status": "WARNING"}}, {}, {})
        self.assertEqual(result["status"], "WARNING")

    def test_empty_installation_has_no_health_score(self):
        result = overall_health({}, {}, {})
        self.assertIsNone(result["health_score"])
        self.assertEqual(result["status"], "UNKNOWN")
        self.assertEqual(result["coverage"], {"measured": 0, "total": 0})


class TlsTests(unittest.TestCase):
    def test_private_trust_preserves_certificate_and_hostname_checks(self):
        context = ssl.create_default_context()
        with patch.multiple(auth.settings, verify_tls=True, controller_ca_file="fixture.pem", controller_url="https://unifi.local:11443"), patch("noc.unifi.ssl.create_default_context", return_value=context) as factory:
            self.assertIs(controller_tls_context(), context)
        factory.assert_called_once_with(cafile="fixture.pem")
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        self.assertFalse(context.verify_flags & ssl.VERIFY_X509_STRICT)

    def test_secure_default(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue(Settings().verify_tls)
        with patch.multiple(auth.settings, verify_tls=True, controller_ca_file=None, controller_url="https://unifi.local:11443"):
            context = controller_tls_context()
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_insecure_settings_fail_closed(self):
        for values in ({"verify_tls": False}, {"controller_url": "http://unifi.local:11443"}):
            with self.subTest(values=values), patch.multiple(auth.settings, **values):
                with self.assertRaises(UniFiError):
                    controller_tls_context()

    def test_missing_ca_fails_closed(self):
        with patch.multiple(auth.settings, verify_tls=True, controller_url="https://unifi.local:11443",
                            controller_ca_file=str(ROOT / "missing-review-ca.pem")):
            with self.assertRaises(FileNotFoundError):
                controller_tls_context()


class DeploymentTests(unittest.TestCase):
    def test_backup_permissions(self):
        source = (ROOT / "backup.sh").read_text(encoding="utf-8")
        self.assertIn("umask 077", source)
        self.assertIn('chmod 700 "$DEST"', source)
        self.assertIn('chmod 600 "$DEST/$NAME.tar.gz"', source)
        permissions = 'umask 077\nchmod 700 backups\nchmod 600 backups/*.tar.gz'
        script = '''set -eu
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cd "$work"
mkdir -m 755 backups
mkdir -m 700 private
touch backups/fixture.tar.gz .env fixture.sh private/secret
chmod 644 backups/fixture.tar.gz
chmod 600 .env private/secret
''' + permissions + '''
test "$(stat -c %a backups)" = 700
test "$(stat -c %a backups/fixture.tar.gz)" = 600
test "$(stat -c %a .env)" = 600
test "$(stat -c %a private/secret)" = 600
'''
        command = ["wsl.exe", "--exec", "sh"] if os.name == "nt" else ["sh"]
        if not shutil.which(command[0]):
            self.skipTest("Linux shell unavailable")
        result = subprocess.run(command, input=script.encode(), capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr.decode())

    def test_public_deployment_has_persistent_state_and_private_default_binding(self):
        source = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        self.assertIn("${BIND_ADDRESS:-127.0.0.1}", source)
        self.assertIn("noc-state:/var/lib/noc", source)
        self.assertIn("db-data:/var/lib/postgresql/data", source)
        self.assertIn("db-secrets:/run/noc-secrets:ro", source)
        self.assertNotIn("build:", source)


class MonitoringUiTests(unittest.TestCase):
    def test_login_layout_remember_me_and_autocomplete(self):
        from playwright.sync_api import expect, sync_playwright

        state = {'authenticated': False}
        attempts = []
        errors = []
        dist = ROOT / 'frontend/dist'
        output = ROOT / 'tools/_shots'
        output.mkdir(exist_ok=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("NOC_BROWSER_CHANNEL", "msedge"), headless=True)
            try:
                page = browser.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))

                def serve(route):
                    path = urlsplit(route.request.url).path
                    if path.startswith('/unifi/api/'):
                        endpoint = path.removeprefix('/unifi/api')
                        user = {'username': 'fixture-login', 'role': 'viewer', 'is_owner': False, 'site_roles': {}}
                        if endpoint == '/auth/login':
                            attempts.append(route.request.post_data_json)
                            state['authenticated'] = True
                            route.fulfill(json=user)
                        elif endpoint == '/auth/me':
                            route.fulfill(status=200 if state['authenticated'] else 401, json=user if state['authenticated'] else {'detail': 'Not authenticated'})
                        else:
                            responses = {'/setup/status': {'needs_owner': False, 'ui_setup': False}, '/sites': [],
                                '/status': {'collector': {'state': 'OK'}, 'sources': [], 'stale_sources': []},
                                '/overview': {'overall': {'health_score': None, 'status': 'UNKNOWN', 'freshness': {'state': 'NO_DATA'}}, 'sites': [], 'tunnels': [], 'alerts': []}}
                            route.fulfill(json=responses.get(endpoint, {}))
                        return
                    asset = (dist / path.removeprefix('/unifi/')).resolve()
                    if asset.is_relative_to(dist.resolve()) and asset.is_file():
                        route.fulfill(path=str(asset), content_type='application/javascript' if asset.suffix == '.js' else 'text/css' if asset.suffix == '.css' else 'image/svg+xml' if asset.suffix == '.svg' else 'text/html')
                    else:
                        route.fulfill(path=str(dist / 'index.html'), content_type='text/html')

                page.route('**/*', serve)
                page.goto('https://noc-fixture.invalid/unifi/')
                username = page.get_by_label('Username', exact=True)
                password = page.get_by_label('Password', exact=True)
                page.wait_for_function("document.querySelector('.brand-mark')?.naturalWidth > 0")
                self.assertTrue(page.locator('.brand-mark').evaluate("image => { const canvas=document.createElement('canvas'); canvas.width=64; canvas.height=64; const context=canvas.getContext('2d'); context.drawImage(image,0,0,64,64); const data=context.getImageData(0,0,64,64).data; let cyan=0,green=0,white=0; for(let index=0;index<data.length;index+=4) { if(data[index+1]>150 && data[index+2]>200 && data[index]<120) cyan++; if(data[index+1]>150 && data[index+2]<200 && data[index]<120) green++; if(data[index]>200 && data[index+1]>200 && data[index+2]>200) white++; } return cyan>30 && green>30 && white>30; }"))
                expect(username).to_have_attribute('autocomplete', 'username')
                expect(password).to_have_attribute('autocomplete', 'current-password')
                expect(page.locator('form')).to_have_attribute('autocomplete', 'on')
                expect(page.get_by_role('checkbox', name='Remember me for 30 days')).not_to_be_checked()
                for width, height in ((1440, 1000), (390, 844)):
                    page.set_viewport_size({'width': width, 'height': height})
                    first, second = username.bounding_box(), password.bounding_box()
                    self.assertGreater(second['y'], first['y'] + first['height'])
                    self.assertAlmostEqual(first['width'], second['width'], delta=1)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    box = page.locator('.login-form').bounding_box()
                    self.assertAlmostEqual(box['x'] + box['width'] / 2, width / 2, delta=2)
                    self.assertAlmostEqual(box['y'] + box['height'] / 2, height / 2, delta=2)
                    page.screenshot(path=str(output / f'login-redesign-{width}.png'), full_page=True)
                username.fill('fixture-login')
                password.fill('fixture-password-long')
                page.get_by_role('checkbox', name='Remember me for 30 days').check()
                page.get_by_role('button', name='Sign in', exact=True).click()
                expect(page.locator('.sidebar')).to_be_visible()
                self.assertEqual(attempts, [{'username': 'fixture-login', 'password': 'fixture-password-long', 'remember_me': True}])
                self.assertNotIn('fixture-password-long', page.evaluate('JSON.stringify(localStorage)'))
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_centered_setup_and_explicit_certificate_retry(self):
        from playwright.sync_api import expect, sync_playwright

        state = {'owner': False, 'connection': {'state': 'not_configured', 'complete': False},
                 'certificate': {'state': 'not_checked'}}
        probe_sites = [{'name': name, 'description': f'Fixture {name}'} for name in ('a', 'b', 'c')]
        probe_tunnels = [{'members': [{'site': 'a'}, {'site': 'b'}]}]
        attempts = []
        errors = []
        dist = ROOT / 'frontend/dist'
        output = ROOT / 'tools/_shots'
        output.mkdir(exist_ok=True)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("NOC_BROWSER_CHANNEL", "msedge"), headless=True)
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                page.on('pageerror', lambda error: errors.append(str(error)))

                def serve(route):
                    path = urlsplit(route.request.url).path
                    if path.startswith('/unifi/api/'):
                        endpoint = path.removeprefix('/unifi/api')
                        if route.request.method == 'POST' and endpoint == '/setup/connection':
                            body = route.request.post_data_json
                            attempts.append(body)
                            state['connection'] = {'state': 'connected', 'complete': True, 'version': 'fixture', 'discovered_sites': 2, 'job_id': 'fixture-job'} if body.get('ca_pem', state['connection'].get('configuration', {}).get('ca_configured', False)) else {
                                'state': 'error', 'complete': False, 'error': 'SSL CERTIFICATE_VERIFY_FAILED self-signed certificate'}
                            route.fulfill(status=202, json={'state': 'checking', 'job_id': 'fixture-job'})
                            return
                        if route.request.method == 'POST' and endpoint == '/setup/certificate':
                            state['certificate'] = {'state': 'ready', 'pem': 'fixture-pem', 'fingerprint': 'ab' * 32,
                                                    'dns_names': ['*.invalid', 'unifi.local'], 'subject': 'Fixture certificate', 'expires': '2030-01-01'}
                            route.fulfill(status=202, json={'state': 'checking'})
                            return
                        if endpoint == '/auth/me' and not state['owner']:
                            route.fulfill(status=401, json={'detail': 'Not authenticated'})
                            return
                        responses = {'/setup/status': {'needs_owner': not state['owner'], 'ui_setup': True},
                                     '/auth/me': {'username': 'fixture-owner', 'role': 'admin', 'is_owner': True, 'site_roles': {}},
                                     '/setup/connection': state['connection'], '/setup/certificate': state['certificate'], '/sites': probe_sites, '/s2s': probe_tunnels,
                                     '/overview': {'overall': {'health_score': None, 'status': 'UNKNOWN', 'freshness': {'state': 'NO_DATA'}}, 'sites': [], 'tunnels': [], 'alerts': []},
                                     '/status': {'collector': {'state': 'OK'}, 'sources': [], 'stale_sources': []}}
                        route.fulfill(json=responses.get(endpoint, {}))
                        return
                    asset = (dist / path.removeprefix('/unifi/')).resolve()
                    if asset.is_relative_to(dist.resolve()) and asset.is_file():
                        content_type = 'application/javascript' if asset.suffix == '.js' else 'text/css' if asset.suffix == '.css' else 'image/svg+xml' if asset.suffix == '.svg' else 'text/html'
                        route.fulfill(path=str(asset), content_type=content_type)
                    else:
                        route.fulfill(path=str(dist / 'index.html'), content_type='text/html')

                page.route('**/*', serve)
                page.goto('https://noc-fixture.invalid/unifi/')
                expect(page.get_by_role('button', name='Create administrator', exact=True)).to_be_visible()
                page.wait_for_function("document.querySelector('.brand-mark')?.naturalWidth > 0")
                for width, height in ((1440, 1000), (390, 844)):
                    page.set_viewport_size({'width': width, 'height': height})
                    box = page.locator('.setup-inner').bounding_box()
                    self.assertLess(abs(box['x'] + box['width'] / 2 - width / 2), 2)
                    self.assertLess(abs(box['y'] + box['height'] / 2 - height / 2), 2)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    page.screenshot(path=str(output / f'centered-owner-{width}.png'), full_page=True)
                animated = page.locator('.setup-mesh')
                expect(animated).to_have_attribute('aria-hidden', 'true')
                expect(page.locator('.setup-telemetry-bars')).to_have_count(0)
                pixels = """canvas => {
                    const data=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data;
                    let hash=0,green=0,blue=0;
                    for(let index=0;index<data.length;index+=4) {
                        hash=(hash+data[index]*3+data[index+1]*5+data[index+2]*7*(index%71))%2147483647;
                        if(data[index+1]>data[index+2]+15 && data[index+1]>data[index]+20) green++;
                        if(data[index+2]>data[index+1]+10 && data[index+2]>data[index]+20) blue++;
                    }
                    return {hash,green,blue};
                }"""
                initial = animated.evaluate(pixels)
                self.assertEqual(initial['green'], 0)
                self.assertGreater(initial['blue'], 100)
                self.assertEqual(animated.evaluate('canvas => getComputedStyle(canvas).pointerEvents'), 'none')
                page.wait_for_function('initial => { const canvas=document.querySelector(".setup-mesh"); return (' + pixels + ')(canvas).hash !== initial; }', arg=initial['hash'])
                page.emulate_media(reduced_motion='reduce')
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))')
                still = animated.evaluate(pixels)
                page.evaluate('() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(() => requestAnimationFrame(resolve))))')
                self.assertEqual(animated.evaluate(pixels)['hash'], still['hash'])
                page.emulate_media(reduced_motion='no-preference')
                state['owner'] = True
                page.set_viewport_size({'width': 1440, 'height': 1000})
                page.reload()
                expect(page.get_by_role('heading', name='Connect UniFi OS', exact=True)).to_be_visible()
                box = page.locator('.setup-inner').bounding_box()
                self.assertLess(abs(box['y'] + box['height'] / 2 - 500), 2)
                page.get_by_label('UniFi OS address', exact=True).fill('https://192.0.2.1')
                page.get_by_label('Network API key', exact=True).fill('fixture-api-key')
                page.get_by_role('button', name='Connect and discover sites', exact=True).click()
                expect(page.get_by_text('Certificate approval required', exact=True)).to_be_visible()
                expect(page.get_by_role('button', name='Inspect certificate', exact=True)).to_be_visible()
                expect(page.get_by_text('Upload a certificate', exact=True)).to_be_visible()
                expect(page.get_by_text('OR', exact=True)).to_be_visible()
                expect(page.get_by_label('Network API key', exact=True)).to_have_value('fixture-api-key')
                for width in (1440, 820):
                    page.set_viewport_size({'width': width, 'height': 1000})
                    methods = page.locator('.certificate-method')
                    for selector in ('strong', '.small'):
                        left = methods.nth(0).locator(selector).bounding_box()
                        right = methods.nth(1).locator(selector).bounding_box()
                        self.assertLess(abs(left['y'] - right['y']), 1)
                    upload = page.get_by_label('Trusted certificate', exact=True).bounding_box()
                    inspect_button = page.get_by_role('button', name='Inspect certificate', exact=True).bounding_box()
                    self.assertLess(abs(upload['y'] - inspect_button['y']), 1)
                    self.assertLess(abs(upload['height'] - inspect_button['height']), 1)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                page.locator('summary').filter(has_text='Optional live tunnel measurements').click()
                expect(page.get_by_text('Start with both options off.', exact=True)).to_be_visible()
                expect(page.get_by_role('checkbox', name='Read gateway counters over SSH', exact=True)).not_to_be_checked()
                expect(page.get_by_role('checkbox', name='Measure ICMP latency/loss through the tunnel', exact=True)).not_to_be_checked()
                expect(page.locator('#gateway-ssh-help')).to_contain_text('Requires reachable gateways')
                expect(page.locator('#tunnel-probe-help')).to_contain_text('Blocked pings can look like packet loss')
                expect(page.get_by_role('checkbox', name='Measure ICMP latency/loss through the tunnel', exact=True)).to_be_disabled()
                state['connection']['connected'] = True
                page.reload()
                page.locator('summary').filter(has_text='Optional live tunnel measurements').click()
                page.get_by_role('checkbox', name='Read gateway counters over SSH', exact=True).check()
                page.locator('summary').filter(has_text='Gateway SSH requirements').click()
                expect(page.get_by_text("Allow this collector to reach each gateway's LAN address", exact=False)).to_be_visible()
                page.get_by_role('checkbox', name='Measure ICMP latency/loss through the tunnel', exact=True).check()
                selector = page.get_by_role('combobox', name='Site containing this collector', exact=True)
                expect(selector.locator('option')).to_have_text(['Select the site containing this collector', 'Fixture a', 'Fixture b'])
                expect(page.get_by_role('button', name='Connect and discover sites', exact=True)).to_be_disabled()
                selector.select_option('Fixture a')
                page.get_by_label('UniFi OS address', exact=True).fill('https://192.0.2.1')
                page.get_by_label('Network API key', exact=True).fill('fixture-api-key')
                page.screenshot(path=str(output / 'setup-certificate-aligned-1440.png'), full_page=True)
                page.set_viewport_size({'width': 390, 'height': 844})
                page.evaluate('window.scrollTo(0, 0)')
                self.assertGreaterEqual(page.locator('.setup-inner').bounding_box()['y'], 0)
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                upload = page.get_by_label('Trusted certificate', exact=True).bounding_box()
                inspect_button = page.get_by_role('button', name='Inspect certificate', exact=True).bounding_box()
                self.assertGreater(inspect_button['y'], upload['y'] + upload['height'])
                page.screenshot(path=str(output / 'setup-certificate-aligned-390.png'), full_page=True)
                page.get_by_role('button', name='Inspect certificate', exact=True).click()
                expect(page.locator('.fingerprint')).to_be_visible()
                expect(page.get_by_role('button', name='Trust this certificate', exact=True)).to_be_disabled()
                page.get_by_role('checkbox', name='I verified this certificate fingerprint').check()
                page.get_by_role('button', name='Trust this certificate', exact=True).click()
                expect(page.get_by_label('TLS certificate hostname', exact=True)).to_have_value('unifi.local')
                page.get_by_role('button', name='Connect and discover sites', exact=True).click()
                expect(page.locator('.sidebar')).to_be_visible()
                expect(page.get_by_role('button', name='Select monitored sites', exact=True)).to_have_count(0)
                expect(page.get_by_label('Network API key', exact=True)).to_have_count(0)
                self.assertEqual(attempts[-1]['ca_pem'], 'fixture-pem')
                self.assertEqual(attempts[-1]['api_key'], 'fixture-api-key')
                self.assertEqual(attempts[-1]['probe_site'], 'Fixture a')
                self.assertTrue(attempts[-1]['probes'])
                self.assertTrue(attempts[-1]['gateway_ssh'])
                state['connection']['connected'] = True
                state['connection']['configuration'] = {'url': 'https://192.0.2.1', 'tls_server_name': 'unifi.local',
                    'gateway_ssh': True, 'probes': True, 'probe_site': 'Fixture a', 'has_api_key': True, 'ca_configured': True}
                page.goto('https://noc-fixture.invalid/unifi/connection')
                expect(page.get_by_label('UniFi OS address', exact=True)).to_have_value('https://192.0.2.1')
                key = page.get_by_label('Network API key', exact=True)
                expect(key).to_have_value('')
                expect(key).to_have_attribute('placeholder', 'Saved API key (unchanged)')
                self.assertFalse(key.evaluate('input => input.required'))
                page.locator('summary').filter(has_text='Certificate and connection help').click()
                expect(page.get_by_label('TLS certificate hostname', exact=True)).to_have_value('unifi.local')
                expect(page.get_by_text('A private trust certificate is configured.', exact=True)).to_be_visible()
                page.locator('summary').filter(has_text='Optional live tunnel measurements').click()
                ssh_option = page.get_by_role('checkbox', name='Read gateway counters over SSH', exact=True)
                expect(ssh_option).to_be_checked()
                expect(page.get_by_role('checkbox', name='Measure ICMP latency/loss through the tunnel', exact=True)).to_be_checked()
                expect(page.get_by_role('combobox', name='Site containing this collector', exact=True)).to_have_value('a')
                expect(page.get_by_role('button', name='Save connection', exact=True)).to_be_enabled()
                for width in (1440, 390):
                    page.set_viewport_size({'width':width,'height':1000})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    page.screenshot(path=str(output / f'connection-prefilled-{width}.png'), full_page=True)
                ssh_option.uncheck()
                expect(page.get_by_text('Unsaved changes', exact=True)).to_be_visible()
                page.get_by_role('button', name='Open navigation', exact=True).click()
                page.locator('.sidebar a[href$="/topology"]').click()
                expect(page.get_by_role('button', name='Keep editing', exact=True)).to_be_visible()
                page.get_by_role('button', name='Keep editing', exact=True).click()
                page.get_by_role('button', name='Discard', exact=True).click()
                expect(ssh_option).to_be_checked()
                expect(page.get_by_text('Unsaved changes', exact=True)).to_have_count(0)
                ssh_option.uncheck()
                with page.expect_response('**/api/setup/connection'):
                    page.get_by_label('TLS certificate hostname', exact=True).fill('unifi.local')
                expect(ssh_option).not_to_be_checked()
                page.get_by_label('UniFi OS address', exact=True).fill('https://other.invalid')
                expect(page.get_by_role('button', name='Save connection', exact=True)).to_be_disabled()
                self.assertTrue(key.evaluate('input => input.required'))
                page.get_by_label('UniFi OS address', exact=True).fill('https://192.0.2.1')
                page.get_by_role('button', name='Save connection', exact=True).click()
                expect(page).to_have_url('https://noc-fixture.invalid/unifi')
                self.assertEqual(attempts[-1]['api_key'], '')
                self.assertNotIn('ca_pem', attempts[-1])
                self.assertEqual(attempts[-1]['tls_server_name'], 'unifi.local')
                self.assertEqual(attempts[-1]['probe_site'], 'Fixture a')
                self.assertFalse(attempts[-1]['gateway_ssh'])
                self.assertEqual(attempts[-1]['probe_site_id'], 'a')
                state['connection'] = {'state': 'connected', 'connected': True, 'complete': True}
                probe_tunnels.clear()
                page.goto('https://noc-fixture.invalid/unifi/connection')
                page.locator('summary').filter(has_text='Optional live tunnel measurements').click()
                expect(page.get_by_role('checkbox', name='Measure ICMP latency/loss through the tunnel', exact=True)).to_be_disabled()
                expect(page.get_by_text('No monitored sites participate in a reported SD-WAN link.', exact=False)).to_be_visible()
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_owner_selection_and_site_admin_account_creation(self):
        from playwright.sync_api import expect, sync_playwright

        site_a, site_b = "00000000-0000-0000-0000-000000000001", "00000000-0000-0000-0000-000000000002"
        user = {"username": "fixture-owner", "role": "admin", "is_owner": True, "site_roles": {}}
        sites = [{"id": site_a, "name": "a", "slug": "a", "description": "Fixture A", "selected": True, "connection_name": "Fixture OS"},
                 {"id": site_b, "name": "b", "slug": "b", "description": "Fixture B", "selected": False, "connection_name": "Fixture OS"}]
        accounts = []
        mutations = []
        errors = []
        refresh_state = {'state': 'idle'}
        fail_selection = {'enabled': False}
        dist = ROOT / "frontend/dist"
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("NOC_BROWSER_CHANNEL", "msedge"), headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))

                def serve(route):
                    path = urlsplit(route.request.url).path
                    if path.startswith('/unifi/api/'):
                        endpoint = path.removeprefix('/unifi/api')
                        if route.request.method != 'GET':
                            body = route.request.post_data_json if route.request.post_data else None
                            mutations.append((endpoint, body))
                            if endpoint == '/monitoring/refresh':
                                sites.append({'id': '00000000-0000-0000-0000-000000000003', 'name': 'c', 'slug': 'c', 'description': 'New site', 'selected': False, 'connection_name': 'Fixture OS'})
                                refresh_state.update({'state': 'complete', 'completed_at': 100})
                                route.fulfill(status=202, json={'state': 'queued'})
                            elif endpoint == '/monitoring/sites':
                                if fail_selection['enabled']:
                                    route.fulfill(status=500, json={'detail': 'Fixture save failure'})
                                    return
                                for site in sites:
                                    if site['id'] in body['selections']:
                                        site['selected'] = body['selections'][site['id']]
                                route.fulfill(json={"ok": True})
                            elif endpoint == '/users':
                                accounts.append({"username": body['username'], "sites": body['sites'], "disabled": False, "is_owner": False})
                                route.fulfill(status=201, json={"username": body['username']})
                            else:
                                errors.append(f"Unexpected mutation {endpoint}")
                                route.abort()
                            return
                        responses = {
                            '/setup/status': {"needs_owner": False, "ui_setup": False},
                            '/monitoring/refresh': refresh_state,
                            '/auth/me': user, '/sites': [s for s in sites if s['selected']], '/monitoring/sites': sites, '/users': accounts,
                            '/users/sites': [s for s in sites if user['is_owner'] or (s['selected'] and user['site_roles'].get(s['id']) == 'admin')],
                            '/account/sessions': [], '/setup/connection': {'state': 'connected', 'complete': True, 'version': 'fixture', 'discovered_sites': len(sites)},
                            '/maintenance': [], '/notifications': {'enabled':False,'provider':'ntfy','url':'https://ntfy.sh','topic':'','severity':'warning','sites':[]}, '/notifications/history': [],
                            '/s2s': [],
                            '/reliability': [{'entity_id':'dev:02:00:00:00:00:01','name':'Fixture switch','site_name':'Fixture A','observed_online_pct':99,'coverage_pct':60,'offline_events_30d':2}],
                            '/changes': [{'id':1,'device_mac':'02:00:00:00:00:01','device_name':'Fixture switch','site_name':'Fixture A','ts':'2026-10-10T12:00:00Z',
                                'before':{'version':'1.0','radios':[{'band':'ng','channel':1,'width':20},{'band':'na','channel':36,'width':80}], 'ports':[{'port_idx':1,'speed':1000},{'port_idx':3,'speed':100}]},
                                'after':{'version':'2.0','radios':[{'band':'na','channel':44,'width':80},{'band':'ng','channel':1,'width':20}], 'ports':[{'port_idx':3,'speed':1000},{'port_idx':1,'speed':1000}]}}],
                            '/status': {"collector": {"state": "OK"}, "sources": [], "stale_sources": []},
                            '/overview': {"overall": {"health_score": None, "status": "UNKNOWN", "freshness": {"state": "NO_DATA"}},
                                          "sites": [], "tunnels": [], "alerts": []},
                        }
                        if endpoint == '/search':
                            from urllib.parse import parse_qs
                            offset = int(parse_qs(urlsplit(route.request.url).query).get('offset', ['0'])[0])
                            results = [{'id':f'event:{index}','type':'event','label':f'Fixture log {index}','site':'Fixture A','path':'/events','detail':'DEVICE_ONLINE'} for index in range(offset, min(offset+100,101))]
                            if offset == 0:
                                results += [{'id':kind,'type':kind,'label':f'Fixture {kind}','site':'Fixture A','path':path,'detail':'Fixture match'} for kind,path in (
                                    ('site','/site/a'),('switch','/device/02:00:00:00:00:01'),('ap','/devices/ap'),('client','/clients'),('alert','/alerts'),('change','/changes'),('maintenance','/maintenance'))]
                            route.fulfill(json=results)
                            return
                        if endpoint not in responses:
                            errors.append(f"Unexpected request {endpoint}")
                        route.fulfill(json=responses.get(endpoint, {}))
                        return
                    asset = (dist / path.removeprefix('/unifi/')).resolve()
                    if asset.is_relative_to(dist.resolve()) and asset.is_file():
                        content_type = 'application/javascript' if asset.suffix == '.js' else 'text/css' if asset.suffix == '.css' else 'image/svg+xml' if asset.suffix == '.svg' else 'text/html'
                        route.fulfill(path=str(asset), content_type=content_type)
                    else:
                        route.fulfill(path=str(dist / 'index.html'), content_type='text/html')

                page.route('**/*', serve)
                page.goto('https://noc-fixture.invalid/unifi/monitoring')
                expect(page.locator('.nav-group').last).to_have_text('Settings')
                expect(page.get_by_role('link', name='Settings Overview', exact=True)).to_have_count(0)
                expect(page.get_by_role('navigation', name='Breadcrumb').get_by_text('Settings', exact=True)).to_be_visible()
                expect(page.get_by_role('navigation', name='Breadcrumb').get_by_role('link', name='Settings', exact=True)).to_have_count(0)
                page.get_by_role('button', name='Refresh sites', exact=True).click()
                expect(page.get_by_text('New site', exact=True)).to_be_visible()
                expect(page.get_by_role('checkbox', name='Monitor New site', exact=True)).not_to_be_checked()
                expect(page.get_by_role('checkbox', name='Monitor Fixture B', exact=True)).not_to_be_checked()
                page.get_by_role('checkbox', name='Monitor Fixture B').click()
                expect(page.get_by_role('checkbox', name='Monitor Fixture B')).to_be_checked()
                page.get_by_role('checkbox', name='Monitor Fixture A').uncheck()
                self.assertEqual(len(mutations), 1)
                page.get_by_role('button', name='Discard changes', exact=True).click()
                expect(page.get_by_role('checkbox', name='Monitor Fixture A')).to_be_checked()
                expect(page.get_by_role('checkbox', name='Monitor Fixture B')).not_to_be_checked()
                page.get_by_role('checkbox', name='Monitor Fixture A').uncheck()
                page.get_by_role('checkbox', name='Monitor Fixture B').check()
                fail_selection['enabled'] = True
                page.get_by_role('button', name='Apply selection', exact=True).click()
                expect(page.get_by_text('Request failed: Fixture save failure', exact=True)).to_be_visible()
                expect(page.get_by_text('2 unsaved site changes', exact=True)).to_be_visible()
                self.assertTrue(sites[0]['selected'])
                fail_selection['enabled'] = False
                page.get_by_role('button', name='Apply selection', exact=True).click()
                expect(page.get_by_text('All changes saved', exact=True)).to_be_visible()
                self.assertIn(('/monitoring/sites', {"selections": {site_a: False, site_b: True}}), mutations)
                page.goto('https://noc-fixture.invalid/unifi/users')
                expect(page.get_by_role('combobox', name='Role for Fixture A')).to_be_visible()
                expect(page.get_by_role('combobox', name='Role for Fixture B')).to_be_visible()
                expect(page.get_by_role('combobox', name='Role for New site')).to_be_visible()
                for width, height in ((1440, 1000), (390, 844)):
                    page.set_viewport_size({'width': width, 'height': height})
                    first = page.get_by_label('Username', exact=True).bounding_box()
                    second = page.get_by_label('Initial password', exact=True).bounding_box()
                    self.assertAlmostEqual(first['width'], second['width'], delta=1)
                    if width == 390:
                        self.assertAlmostEqual(first['x'], second['x'], delta=1)
                    else:
                        self.assertAlmostEqual(first['y'], second['y'], delta=1)
                    page.screenshot(path=str(ROOT / f'tools/_shots/settings-users-{width}.png'), full_page=True)
                    for path, names, button_name in (
                        ('account', ('Current password', 'New password', 'Confirm password'), 'Change password and sign out'),
                        ('connection', ('UniFi OS address', 'Network API key'), 'Connect and discover sites'),
                    ):
                        page.goto(f'https://noc-fixture.invalid/unifi/{path}')
                        expect(page.get_by_role('button', name=button_name, exact=True)).to_be_visible()
                        boxes = [page.get_by_label(name, exact=True).bounding_box() for name in names]
                        for box in boxes[1:]:
                            self.assertAlmostEqual(box['x'], boxes[0]['x'], delta=1)
                            self.assertAlmostEqual(box['width'], boxes[0]['width'], delta=1)
                        button = page.get_by_role('button', name=button_name, exact=True).bounding_box()
                        self.assertLess(button['width'], 300)
                        self.assertLess(button['height'], 50)
                        self.assertAlmostEqual(button['x'], boxes[0]['x'], delta=1)
                        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > window.innerWidth'))
                        page.screenshot(path=str(ROOT / f'tools/_shots/settings-{path}-{width}.png'), full_page=True)
                    page.goto('https://noc-fixture.invalid/unifi/users')
                page.set_viewport_size({'width': 1440, 'height': 1000})
                expect(page.get_by_role('link', name='Maintenance', exact=True).locator('xpath=preceding-sibling::div[contains(@class,"nav-group")][1]')).to_have_text('Settings')
                expect(page.get_by_role('link', name='Reliability & Changes', exact=True)).to_have_count(0)
                for width,height in ((1440,1000),(390,844)):
                    page.set_viewport_size({'width':width,'height':height})
                    page.locator('.global-search').get_by_role('searchbox').fill('Fix')
                    page.locator('.global-search').get_by_role('button',name='Search',exact=True).click()
                    expect(page).to_have_url('https://noc-fixture.invalid/unifi/search?q=Fix')
                    for group in ('Sites','Switches','Access Points','Clients','Logs','Alerts','Configuration Changes','Maintenance'):
                        expect(page.get_by_role('region',name=group,exact=True)).to_be_visible()
                    page.get_by_role('button',name='Load more results',exact=True).click()
                    expect(page.get_by_role('cell',name='Fixture log 100',exact=True)).to_be_visible()
                    expect(page.get_by_role('button',name='Load more results',exact=True)).to_have_count(0)
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > window.innerWidth'))
                    page.screenshot(path=str(ROOT / f'tools/_shots/dashboard-search-{width}.png'),full_page=True)
                    page.goto('https://noc-fixture.invalid/unifi/availability')
                    expect(page.get_by_role('heading',name='Device Availability',exact=True)).to_be_visible()
                    expect(page.get_by_role('columnheader',name='24 h collection coverage')).to_be_visible()
                    expect(page.get_by_role('columnheader',name='Changed fields')).to_have_count(0)
                    page.screenshot(path=str(ROOT / f'tools/_shots/device-availability-{width}.png'),full_page=True)
                    page.goto('https://noc-fixture.invalid/unifi/changes')
                    expect(page.get_by_role('heading',name='Configuration Changes',exact=True)).to_be_visible()
                    expect(page.locator('.change-field-name').get_by_text('Firmware version',exact=True)).to_be_visible()
                    expect(page.locator('.change-before').get_by_text('1.0',exact=True)).to_be_visible()
                    expect(page.locator('.change-after').get_by_text('2.0',exact=True)).to_be_visible()
                    expect(page.locator('.change-field')).to_have_count(3)
                    expect(page.locator('.change-field-name').get_by_text('5 GHz radio / Channel',exact=True)).to_be_visible()
                    expect(page.locator('.change-field-name').get_by_text('Port 3 / Link speed',exact=True)).to_be_visible()
                    expect(page.locator('.change-before').get_by_text('100 Mbps',exact=True)).to_be_visible()
                    expect(page.locator('.change-after').get_by_text('1 Gbps',exact=True)).to_be_visible()
                    page.locator('.change-toolbar select').nth(1).select_option('Wi-Fi')
                    expect(page.locator('.change-field')).to_have_count(1)
                    page.locator('.change-toolbar select').nth(1).select_option('')
                    page.get_by_label('Device or changed value',exact=True).fill('missing')
                    expect(page.get_by_text('No changes match these filters',exact=True)).to_be_visible()
                    page.get_by_label('Device or changed value',exact=True).fill('')
                    page.locator('.change-record summary').click()
                    expect(page.locator('.change-comparison')).to_be_hidden()
                    page.locator('.change-record summary').click()
                    expect(page.get_by_role('columnheader',name='Online samples')).to_have_count(0)
                    self.assertFalse(page.evaluate('document.documentElement.scrollWidth > window.innerWidth'))
                    page.screenshot(path=str(ROOT / f'tools/_shots/configuration-changes-{width}.png'),full_page=True)
                    sites[0]['selected'] = True
                    for path, controls, buttons in (('maintenance',('Site','Reason','Start','End','Repeat'),('Schedule',)), ('notifications',('Provider','Severity','HTTPS server','Topic','Access token'),('Save channel','Send test'))):
                        page.goto(f'https://noc-fixture.invalid/unifi/{path}')
                        for label in controls:
                            expect(page.get_by_label(label)).to_be_visible()
                        for label in buttons:
                            box = page.get_by_role('button',name=label,exact=True).bounding_box()
                            self.assertLess(box['width'],200)
                            self.assertLess(box['height'],50)
                        boxes = [page.get_by_label(label).bounding_box() for label in controls[:2]]
                        if width == 1440:
                            self.assertAlmostEqual(boxes[0]['y'],boxes[1]['y'],delta=1)
                            self.assertAlmostEqual(boxes[0]['width'],boxes[1]['width'],delta=1)
                        else:
                            self.assertAlmostEqual(boxes[0]['x'],boxes[1]['x'],delta=1)
                        if path == 'notifications':
                            page.get_by_label('Provider').select_option('telegram')
                            expect(page.get_by_label('Chat ID',exact=True)).to_be_visible()
                            expect(page.get_by_label('HTTPS server',exact=True)).to_have_count(0)
                        self.assertFalse(page.evaluate('document.documentElement.scrollWidth > window.innerWidth'))
                        page.screenshot(path=str(ROOT / f'tools/_shots/settings-{path}-{width}.png'),full_page=True)
                page.set_viewport_size({'width':1440,'height':1000})
                sites[0]['selected'] = True
                user.update({"username": "fixture-admin", "is_owner": False, "site_roles": {site_a: 'admin', site_b: 'reader'}})
                page.goto('https://noc-fixture.invalid/unifi/users')
                expect(page.get_by_role('link', name='Monitoring Sites', exact=True)).to_have_count(0)
                expect(page.get_by_role('combobox', name='Role for Fixture B')).to_have_count(0)
                page.get_by_label('Username', exact=True).fill('new-reader')
                page.get_by_label('Initial password', exact=True).fill('fixture-password-long')
                page.get_by_role('combobox', name='Role for Fixture A').select_option('reader')
                page.get_by_role('button', name='Create user', exact=True).click()
                expect(page.get_by_role('status')).to_have_text('Saved')
                self.assertEqual(accounts[0]['sites'], {site_a: 'reader'})
                expect(page.get_by_role('cell', name='new-reader', exact=True)).to_be_visible()
                user.update({"role": "viewer", "site_roles": {site_a: 'reader'}})
                page.goto('https://noc-fixture.invalid/unifi/users')
                expect(page.get_by_text('Site administrator access required', exact=True)).to_be_visible()
                expect(page.get_by_role('link', name='Users', exact=True)).to_have_count(0)
                expect(page.get_by_role('link', name='My Account', exact=True)).to_be_visible()
                expect(page.get_by_role('link', name='Settings Overview', exact=True)).to_have_count(0)
                self.assertEqual(errors, [])
            finally:
                browser.close()

    def test_health_status_and_alert_navigation_in_built_frontend(self):
        from playwright.sync_api import expect, sync_playwright

        mac = "02:00:00:00:10:01"
        live = {"state": "LIVE", "age_s": 0, "last_update": int(time.time() * 1000)}
        alert = {"id": 1, "kind": "threshold", "site": "fixture", "site_name": "Fixture site", "slug": "fixture",
                 "entity_id": f"port:{mac}:8", "entity_name": "Fixture switch / Port 8", "metric": "err_per_min",
                 "severity": "critical", "state": "firing", "condition": "> 100", "value": 120, "threshold": 100,
                 "started_at": "2026-10-10T12:00:00Z", "message": "Fixture port failure"}
        overall = {"health_score": 80, "status": "CRITICAL", "reasons": [], "coverage": {"measured": 2, "total": 2}, "freshness": live}
        site_summaries = [{"name": name, "slug": name, "description": f"Fixture {name}", "health": {"health_score": score, "status": status, "reasons": []},
                   "health_freshness": live, "wan": {"up": True, "now": {}, "freshness": live}, "devices": {},
                   "clients": {"online": 0}, "alerts": {}, "gateway": None, "incidents": []}
                  for name, score, status in (("a", 100, "HEALTHY"), ("b", 60, "CRITICAL"))]
        device = {"name": "Fixture switch", "mac": mac, "device_type": "switch", "site_name": "Fixture site",
                  "slug": "fixture", "online": True, "now": {}, "capabilities": {}, "clients": [], "events": [],
                  "alerts": [alert], "port_clients": {}, "freshness": live,
                  "raw": {"port_table": [{"port_idx": 8, "name": "Port 8", "up": True, "speed": 1000}]}}
        responses = {"/auth/me": {"username": "fixture-reader", "role": "viewer"}, "/sites": [],
                     "/setup/status": {"needs_owner": False, "ui_setup": False},
                     "/status": {"collector": {"state": "OK"}, "sources": [], "stale_sources": []},
                     "/overview": {"overall": overall, "sites": site_summaries, "tunnels": [], "alerts": [alert]},
                     "/metrics": {"series": []}, "/events": {"items": [], "types": [], "total": 0},
                         "/alerts": [alert], "/alerts/1/ack": {"ok": True}, f"/devices/{mac}": device}
        responses['/devices'] = []
        responses['/clients'] = []
        responses[f'/clients/fixture/{mac}'] = {'name': 'Docker', 'mac': mac, 'site_name': 'Fixture site', 'is_wired': True,
            'online': True, 'raw': {}, 'now': {}, 'events': [], 'entity_id': f'client:fixture:{mac}'}
        responses[f'/clients/a/{mac}'] = {**responses[f'/clients/fixture/{mac}'], 'site_name': 'Fixture a', 'entity_id': f'client:a:{mac}'}
        topology_nodes = [{'id':f'site:{name}','type':'site','site':name,'label':f'Fixture {name}','slug':name,'status':'healthy','detail':'Health 100'} for name in ('a','b','c')]
        topology_nodes += [{'id':f'gateway:{name}','type':'gateway','site':name,'label':f'{name} gateway','status':'healthy'} for name in ('a','b')]
        topology_nodes += [{'id':f'{kind}:{name}','type':kind,'site':name,'label':f'{name} {kind}','status':'healthy'} for name in ('a','b') for kind in ('internet','switch','ap')]
        topology_nodes += [{'id':'switch:z','type':'switch','site':'a','label':'a first switch','status':'healthy'}, {'id':'ap:z','type':'ap','site':'a','label':'a first AP','status':'healthy'}]
        topology_nodes += [{'id':f'client:a:{mac}','type':'client','site':'a','label':'Topology workstation','mac':mac,'status':'healthy'},
                   {'id':'client:b:00:11:22:33:44:55','type':'client','site':'b','label':'','mac':'00:11:22:33:44:55','status':'healthy'}]
        topology_nodes += [{'id':f'c:{index}','type':'switch' if index < 2 else 'ap','site':'c','label':f'Demo device with longer name {index}','status':'healthy'} for index in range(9)]
        topology_edges = [{'source':f'gateway:{name}','target':f'site:{name}','kind':'site'} for name in ('a','b')]
        topology_edges += [{'source':f'site:{name}','target':f'internet:{name}','kind':'wan'} for name in ('a','b')]
        topology_edges += [{'source':f'{child}:{name}','target':f'{parent}:{name}','kind':'wired'} for name in ('a','b') for child,parent in (('switch','gateway'),('ap','switch'))]
        topology_edges += [{'source':'switch:z','target':'gateway:a','kind':'wired'}, {'source':'ap:z','target':'switch:z','kind':'wifi'}]
        topology_edges += [{'source':f'client:a:{mac}','target':'switch:a','kind':'wired'}, {'source':'client:b:00:11:22:33:44:55','target':'ap:b','kind':'wifi'}]
        topology_edges += [{'source':f'c:{index}','target':'c:0','kind':'wired','label':f'port {index}'} for index in range(2,9)]
        topology_edges.append({'source':'gateway:a','target':'gateway:b','kind':'tunnel','status':'healthy','label':'SD-WAN CONNECTED','tunnel':'fixture-link'})
        responses['/topology'] = {'nodes':topology_nodes,'edges':topology_edges}
        fixture_end = int(time.time() * 1000)
        fixture_start = fixture_end - 86400000
        tunnel_fixture = {'id': 'fixture-link', 'label': 'Fixture a / Fixture b', 'name': 'Fixture link', 'technology': 'WireGuard SD-WAN',
            'connected': True, 'local_site': 'a', 'remote_site': 'b', 'config_freshness': live, 'freshness': live,
            'members': [{'site': name, 'site_desc': f'Demo {name.upper()} extended site name', 'subnets': []} for name in ('a', 'b')],
            'status': {'a': {'connected': True}, 'b': {'connected': True}},
            'live': {'health_score': 60, 'status': 'DEGRADED', 'controller_state': 'CONNECTED', 'probe_state': 'NO_REPLY',
                'rx_bps': 333, 'tx_bps': 777, 'loss_pct_1m': 100, 'coverage': {'measured': 3, 'total': 7},
                'measurements': {'rx_bps': {'state': 'AVAILABLE', 'reason': 'Available', 'source': 'b'},
                    'latency_ms': {'state': 'UNAVAILABLE', 'reason': 'No ICMP replies', 'source': 'collector ICMP'}}}}
        responses['/s2s'] = [tunnel_fixture, {**tunnel_fixture, 'id': 'fixture-second-link', 'label': 'Other link'}]
        responses['/s2s/fixture-link/live'] = {'live': tunnel_fixture['live'], 'freshness': live, 'interval_s': 5,
            'series': {'rx_bps': [[fixture_end - index * 5000, 333 + index] for index in range(30, -1, -1)],
                       'tx_bps': [[fixture_end - index * 5000, 777 - index] for index in range(30, -1, -1)]}}
        responses['/operations'] = {'attention': [{'id': 'alert:1', 'severity': 'critical', 'site': 'Fixture site', 'title': alert['message'], 'path': '/alerts'},
            {'id': 'stale:fixture', 'severity': 'warning', 'site': 'Fixture site', 'title': 'Gateway collection stale', 'path': '/alerts'}],
            'capacity': [], 'updates': [], 'incidents': [{'id': '1', 'primary': alert, 'related': [{**alert, 'id': 2, 'message': 'Related AP offline'}],
                'basis': 'Same-site timing correlation, not proven causation', 'path': '/alerts'}]}
        responses['/reliability'] = [{'entity_id': 'dev:' + mac, 'name': 'Fixture switch', 'site_name': 'Fixture site', 'observed_online_pct': 99,
            'coverage_pct': 70, 'offline_events_30d': 1, 'intervals': [
                {'state': 'ONLINE', 'start': fixture_start, 'end': fixture_start + 60000000, 'duration_s': 60000},
                {'state': 'OFFLINE', 'start': fixture_start + 60000000, 'end': fixture_start + 60300000, 'duration_s': 300},
                {'state': 'UNKNOWN', 'start': fixture_start + 60300000, 'end': fixture_end, 'duration_s': 26100}],
            'maintenance': [{'title': 'Fixture maintenance', 'start': fixture_start + 60000000, 'end': fixture_start + 60600000}]}]
        history_active = [False]
        errors = []
        dist = ROOT / "frontend/dist"
        self.assertTrue((dist / "index.html").exists(), "Run npm run build before browser regressions")
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("NOC_BROWSER_CHANNEL", "msedge"), headless=True)
            try:
                page = browser.new_page(viewport={"width": 1440, "height": 1000})
                page.on("pageerror", lambda error: errors.append(str(error)))

                def serve(route):
                    path = urlsplit(route.request.url).path
                    if path.startswith("/unifi/api/"):
                        endpoint = path.removeprefix("/unifi/api")
                        if endpoint not in responses:
                            errors.append(f"Unexpected fixture request: {endpoint}")
                            route.fulfill(status=404, json={"detail": "Unknown fixture"})
                        else:
                            response = responses[endpoint]
                            if endpoint == '/alerts/1/ack' and route.request.method == 'POST':
                                alert.update({'acked_by': 'fixture-admin', 'acked_at': '2026-10-10T13:00:00Z'})
                            if endpoint == '/metrics' and history_active[0]:
                                from urllib.parse import parse_qs
                                query = parse_qs(urlsplit(route.request.url).query)
                                response = {'from': fixture_end - 7200000, 'to': fixture_end, 'resolution': '5 minutes', 'source': '5m',
                                    'series': [{'entity_id': entity, 'metric': metric, 'expected_cadence_ms': 300000,
                                        'coverage': {'observed': 16, 'expected': 24, 'percent': 66.7},
                                        'points': [[fixture_end - 7200000 + index * 300000, 10 + index, index, 60 + index] for index in range(24) if index not in range(8, 16)]}
                                        for entity in query.get('entity', [''])[0].split(',') for metric in query.get('metric', [''])[0].split(',')]}
                            if endpoint == '/topology' and 'clients=true' not in urlsplit(route.request.url).query:
                                ids = {node['id'] for node in response['nodes'] if node['type'] != 'client'}
                                response = {'nodes': [node for node in response['nodes'] if node['id'] in ids],
                                            'edges': [edge for edge in response['edges'] if edge['source'] in ids and edge['target'] in ids]}
                            route.fulfill(json=response)
                        return
                    asset = (dist / path.removeprefix("/unifi/")).resolve()
                    if not asset.is_relative_to(dist.resolve()):
                        route.abort()
                    elif asset.is_file():
                        content_type = "application/javascript" if asset.suffix == ".js" else "text/css" if asset.suffix == ".css" else "image/svg+xml" if asset.suffix == ".svg" else "text/html"
                        route.fulfill(path=str(asset), content_type=content_type)
                    else:
                        route.fulfill(path=str(dist / "index.html"), content_type="text/html")

                page.route("**/*", serve)
                page.goto("https://noc-fixture.invalid/unifi/")
                page.wait_for_function("document.querySelector('.brand-mark')?.naturalWidth > 0")
                expect(page.locator(".topbar .pill").first).to_have_text("80 CRITICAL")
                expect(page.get_by_text("Current data: 2/2 sites", exact=True)).to_be_visible()
                first_site = site_summaries[0]
                site_summaries[:] = [first_site]
                page.reload()
                expect(page.locator('.sitecard h2')).to_have_count(1)
                site_summaries[:] = [first_site, {**first_site, "name": "b", "slug": "b", "description": "Fixture b"},
                                     {**first_site, "name": "c", "slug": "c", "description": "Fixture c"}]
                page.reload()
                expect(page.locator('.sitecard h2')).to_have_count(3)
                expect(page.get_by_role('link', name='Site-to-Site / SD-WAN', exact=True)).to_have_count(0)
                expect(page.locator('.nav-group').filter(has_text='Monitored Sites')).to_have_count(1)
                responses['/sites'] = [{'slug': 'a', 'description': 'Demo East'}, {'slug': 'b', 'description': 'Demo West'}]
                first_site['health'] = {'health_score': 95, 'status': 'WARNING', 'reasons': []}
                site_summaries[1]['health'] = {'health_score': 100, 'status': 'HEALTHY', 'reasons': []}
                responses['/sites/a'] = {**first_site, 'name': 'demo0001', 'description': 'Demo East', 'timezone': 'Europe/Amsterdam',
                    'tunnels': [], 'device_list': [], 'unifi_health': {}}
                responses['/metrics/heatmap'] = {'bucket': '5 minutes', 'series': {}}
                page.goto('https://noc-fixture.invalid/unifi/site/a')
                expect(page.locator('.nav a[href="/unifi/site/a"] .dot')).to_have_class('dot warn')
                expect(page.locator('.nav a[href="/unifi/site/b"] .dot')).to_have_class('dot ok')
                expect(page.get_by_text('Demo East · Europe/Amsterdam', exact=True)).to_be_visible()
                expect(page.get_by_text('Site demo0001 · Europe/Amsterdam', exact=True)).to_have_count(0)
                expect(page.locator('[title="Site ID: demo0001"]')).to_be_visible()
                page.goto('https://noc-fixture.invalid/unifi/')
                responses['/overview']['tunnels'] = [{'id':'fixture-link','label':'Fixture a / Fixture b','members':[], 'config_freshness':live,'connected':True}]
                page.reload()
                expect(page.locator('.overview-sites > .sitecard')).to_have_count(3)
                expect(page.locator('.overview-sites .overview-link')).to_have_count(0)
                expect(page.get_by_role('region', name='Inter-site connections')).to_be_visible()
                connection_button = page.get_by_role('button', name='Connection details', exact=True)
                self.assertLess(connection_button.bounding_box()['width'], 200)
                history_active[0] = True
                page.goto('https://noc-fixture.invalid/unifi/s2s?tunnel=fixture-link&range=24h')
                expect(page.get_by_role('combobox', name='Tunnel', exact=True)).to_have_value('fixture-link')
                expect(page.get_by_text('Controller: CONNECTED', exact=True)).to_be_visible()
                expect(page.get_by_text('Probe: NO_REPLY', exact=True)).to_be_visible()
                expect(page.get_by_text('No ICMP replies', exact=True)).to_be_visible()
                expect(page.get_by_role('heading', name='Connection details', exact=True)).to_have_count(1)
                page.wait_for_function("document.querySelectorAll('[data-chart-group] canvas').length >= 6")
                groups = page.locator('[data-chart-group]').evaluate_all('elements => [...new Set(elements.map(element => element.dataset.chartGroup))]')
                self.assertEqual(len(groups), 1)
                page.wait_for_function("[...document.querySelectorAll('[data-chart-group] canvas')].every(canvas => { const pixels=canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data; return pixels.some((value,index)=>index%4===3 && value>0); })")
                page.evaluate("""window.zoomBands = () => [...document.querySelectorAll('[data-chart-group] canvas')].map(canvas => {
                    const pixels=canvas.getContext('2d').getImageData(0,canvas.height-30,canvas.width,30).data;
                    return pixels.reduce((sum,value,index)=>(sum + value * (index%997+1)) % 2147483647,0);
                }); window.beforeZoom=window.zoomBands();""")
                chart = page.locator('[data-chart-group] canvas').first
                chart.scroll_into_view_if_needed()
                box = chart.bounding_box()
                page.mouse.move(box['x'] + box['width'] / 2, box['y'] + 80)
                page.mouse.wheel(0, -400)
                page.wait_for_function('window.zoomBands().every((value,index)=>value!==window.beforeZoom[index])')
                expect(page.locator('.chart-metadata').filter(has_text='average with min/max bounds').first).to_be_visible()
                for width in (1440, 390):
                    page.set_viewport_size({'width': width, 'height': 1000})
                    page.mouse.move(0, 0)
                    page.wait_for_function('document.documentElement.scrollWidth <= innerWidth')
                    self.assertTrue(page.locator('.controller-sites .pill').evaluate_all('elements => elements.length === 2 && elements.every(element => { const badge = element.getBoundingClientRect(); const card = element.closest(".card").getBoundingClientRect(); return badge.left >= card.left && badge.right <= card.right && element.scrollWidth <= element.clientWidth + 1; })'))
                    page.screenshot(path=str(ROOT / f'tools/_shots/s2s-linked-charts-{width}.png'), full_page=True)
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width, page.evaluate("[...document.querySelectorAll('.main *')].filter(element=>element.getBoundingClientRect().right>innerWidth+2).slice(0,12).map(element=>element.tagName+'.'+element.className+': '+element.getBoundingClientRect().width).join('; ')"))
                page.goto('https://noc-fixture.invalid/unifi/operations?site=a&range=24h')
                expect(page.locator('.incident-group')).to_have_count(1)
                page.locator('.incident-group summary').click()
                expect(page.get_by_role('link', name='Related AP offline', exact=True)).to_be_visible()
                expect(page.get_by_text(alert['message'], exact=False)).to_have_count(1)
                page.goto('https://noc-fixture.invalid/unifi/availability')
                expect(page).to_have_url(re.compile(r'/availability\?site=a&range=24h$'))
                page.locator('.availability-device summary').click()
                expect(page.locator('.availability-period.offline')).to_have_count(1)
                expect(page.locator('.availability-period.unknown')).to_have_count(1)
                expect(page.locator('.maintenance-overlay')).to_have_count(1)
                expect(page.get_by_role('cell', name='Fixture maintenance', exact=True)).to_have_count(2)
                expect(page.get_by_role('cell', name='Fixture maintenance', exact=True).first).to_be_visible()
                self.assertAlmostEqual(page.locator('.availability-period').evaluate_all('elements => elements.reduce((sum,element)=>sum+element.getBoundingClientRect().width,0)'), page.locator('.availability-timeline').bounding_box()['width'], delta=2)
                page.screenshot(path=str(ROOT / 'tools/_shots/availability-390.png'), full_page=True)
                responses['/overview']['tunnels'] = [tunnel_fixture]
                page.goto('https://noc-fixture.invalid/unifi/wallboard')
                expect(page.locator('.sidebar')).to_have_count(0)
                expect(page.get_by_role('heading', name='Active Incidents', exact=True)).to_be_visible()
                expect(page.locator('.link-matrix .pill')).to_have_count(2)
                for width in (1440, 390):
                    page.set_viewport_size({'width': width, 'height': 1000})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    page.screenshot(path=str(ROOT / f'tools/_shots/wallboard-{width}.png'), full_page=True)
                page.set_viewport_size({'width': 1440, 'height': 1000})
                responses['/sites'] = [{'name':name,'slug':name,'description':f'Fixture {name}'} for name in ('a','b','c')]
                page.add_init_script("""window.topologyLabels = new Map(); window.topologyLabelPositions = new Map(); window.topologyClickPositions = new Map();
                    const original = CanvasRenderingContext2D.prototype.fillText;
                    CanvasRenderingContext2D.prototype.fillText = function(text, ...args) {
                        if (!window.topologyLabels.has(this.canvas)) window.topologyLabels.set(this.canvas, new Set());
                        window.topologyLabels.get(this.canvas).add(text);
                        const position = this.getTransform().transformPoint(new DOMPoint(args[0], args[1]));
                        window.topologyLabelPositions.set(text, position.x);
                        window.topologyClickPositions.set(text, {x: position.x, y: position.y});
                        return original.call(this, text, ...args);
                    };""")
                page.goto('https://noc-fixture.invalid/unifi/topology')
                expect(page.get_by_role('combobox', name='Site', exact=True)).to_have_value('all')
                expect(page.locator('.topology-site')).to_have_count(2)
                expect(page.locator('.topology-site[data-site="a,b"]')).to_contain_text('8 devices · 1 SD-WAN link')
                expect(page.locator('.topology-site[data-site="c"]')).to_contain_text('9 devices')
                expect(page.get_by_role('region', name='Reported inter-site connections')).to_have_count(0)
                page.wait_for_function("""() => {
                    const canvas = document.querySelector('.topology-site[data-site="a,b"] canvas');
                    const labels = window.topologyLabels.get(canvas);
                    return labels && ['a gateway','b gateway','a internet','b internet','a switch','b switch','a ap','b ap','SD-WAN CONNECTED'].every(label => labels.has(label));
                }""")
                page.wait_for_function("""() => {
                    const positions = window.topologyLabelPositions;
                    return positions.has('a first AP') && positions.get('a first switch') < positions.get('a switch')
                        && positions.get('a first AP') < positions.get('a ap');
                }""")
                for width in (1440, 390):
                    page.set_viewport_size({'width':width,'height':1000})
                    expect(page.locator('.topology-viewport canvas')).to_have_count(2)
                    page.wait_for_function("Array.from(document.querySelectorAll('.topology-viewport canvas')).every(canvas => { const data = canvas.getContext('2d').getImageData(0,0,canvas.width,canvas.height).data; let painted=0; for(let index=3;index<data.length;index+=4) if(data[index]) painted++; return painted > 1000; })")
                    if width == 390:
                        page.wait_for_function("Array.from(document.querySelectorAll('.topology-viewport')).every(element => Math.abs(element.scrollLeft - (element.scrollWidth - element.clientWidth) / 2) < 2)")
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    page.screenshot(path=str(ROOT / f'tools/_shots/topology-three-sites-{width}.png'), full_page=True)
                page.set_viewport_size({'width':1440,'height':1000})
                page.get_by_role('checkbox', name='Show clients', exact=True).check()
                page.wait_for_function("window.topologyClickPositions.has('1 clients')")
                page.get_by_role('button', name='Expand all', exact=True).click()
                page.wait_for_function("window.topologyClickPositions.has('Topology workstation') && window.topologyClickPositions.has('00:11:22:33:44:55')")
                for width in (1440, 390):
                    page.set_viewport_size({'width':width,'height':1000})
                    self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), width)
                    page.screenshot(path=str(ROOT / f'tools/_shots/topology-clients-{width}.png'), full_page=True)
                page.set_viewport_size({'width':1440,'height':1000})
                canvas = page.locator('.topology-site[data-site="a,b"] canvas')
                canvas.scroll_into_view_if_needed()
                position = page.evaluate("window.topologyClickPositions.get('Topology workstation')")
                box = canvas.bounding_box()
                page.mouse.click(box['x'] + position['x'], box['y'] + position['y'] - 4)
                expect(page).to_have_url(f'https://noc-fixture.invalid/unifi/client/a/{mac}')
                expect(page.get_by_role('heading', name='Docker', exact=True)).to_be_visible()
                page.goto('https://noc-fixture.invalid/unifi/topology')
                topology_edges.append({'source':'gateway:b','target':'c:0','kind':'tunnel','status':'offline','label':'SD-WAN DISCONNECTED','tunnel':'fixture-second-link'})
                page.reload()
                expect(page.locator('.topology-site')).to_have_count(1)
                expect(page.locator('.topology-site[data-site="a,b,c"]')).to_contain_text('17 devices · 2 SD-WAN links')
                topology_edges[:] = [edge for edge in topology_edges if edge['kind'] != 'tunnel']
                page.reload()
                expect(page.locator('.topology-site')).to_have_count(3)
                expect(page.locator('.topology-site[data-site="a"]')).to_contain_text('5 devices')
                expect(page.locator('.topology-site[data-site="b"]')).to_contain_text('3 devices')
                expect(page.locator('.topology-site[data-site="c"]')).to_contain_text('9 devices')
                page.set_viewport_size({'width':1440,'height':1000})
                page.goto('https://noc-fixture.invalid/unifi/')
                overall.update({"health_score": None, "status": "UNKNOWN", "coverage": {"measured": 1, "total": 2}})
                page.reload()
                expect(page.locator(".topbar .pill").first).to_have_text("? UNKNOWN")
                expect(page.get_by_text("Current data: 1/2 sites", exact=True)).to_be_visible()
                overall["freshness"] = {"state": "STALE", "age_s": 100}
                page.reload()
                expect(page.locator(".topbar .pill").first).to_have_text("? DATA STALE")
                page.goto("https://noc-fixture.invalid/unifi/alerts")
                badge = page.locator('.nav a[href="/unifi/alerts"] .tag')
                expect(badge).to_have_text('1')
                expect(badge).to_have_class('tag crit')
                responses['/overview']['alerts'] = [
                    {**alert, 'acked_by': 'fixture-admin', 'acked_at': '2026-10-10T13:00:00Z'},
                    {**alert, 'id': 2, 'severity': 'warning'},
                    {**alert, 'id': 3, 'kind': 'anomaly'},
                ]
                page.reload()
                expect(badge).to_have_text('1')
                expect(badge).to_have_class('tag warn')
                responses['/overview']['alerts'] = [alert]
                responses['/auth/me'].update({'username': 'fixture-admin', 'role': 'admin', 'is_owner': True, 'site_roles': {}})
                alert['can_ack'] = True
                page.reload()
                expect(badge).to_have_text('1')
                page.get_by_role('button', name='Ack', exact=True).click()
                expect(badge).to_have_count(0, timeout=3000)
                expect(page.get_by_role('cell', name='fixture-admin', exact=True)).to_be_visible()
                expect(page.get_by_text('Fixture port failure', exact=True)).to_be_visible()
                page.get_by_text("Fixture port failure", exact=True).click()
                expect(page).to_have_url(f"https://noc-fixture.invalid/unifi/device/{mac}")
                expect(page.get_by_role("heading", name="Fixture switch", exact=True)).to_be_visible()
                expect(page.get_by_role('navigation', name='Breadcrumb').get_by_role('link', name='Switches', exact=True)).to_have_attribute('href', '/unifi/devices/switch')
                expect(page.locator(".page-head .pill")).to_have_text("CRITICAL")
                device["freshness"] = {"state": "STALE", "age_s": 100}
                page.reload()
                expect(page.locator(".page-head .pill")).to_have_text("DATA STALE")
                page.get_by_role('navigation', name='Breadcrumb').get_by_role('link', name='Switches', exact=True).click()
                expect(page.get_by_role('heading', name='Switches', exact=True)).to_be_visible()
                page.goto(f'https://noc-fixture.invalid/unifi/client/fixture/{mac}')
                expect(page.get_by_role('heading', name='Docker', exact=True)).to_be_visible()
                breadcrumb = page.get_by_role('navigation', name='Breadcrumb')
                expect(breadcrumb).to_contain_text('Docker')
                page.set_viewport_size({'width': 390, 'height': 844})
                self.assertLessEqual(page.evaluate('document.documentElement.scrollWidth'), 390)
                page.screenshot(path=str(ROOT / 'tools/_shots/client-breadcrumb-390.png'), full_page=True)
                breadcrumb.get_by_role('link', name='Clients', exact=True).click()
                expect(page.get_by_role('heading', name='Clients', exact=True)).to_be_visible()
                self.assertEqual(errors, [])
            finally:
                browser.close()


class TooltipTests(unittest.TestCase):
    def test_html_is_rendered_as_text_even_without_csp(self):
        from playwright.sync_api import sync_playwright

        topology = (ROOT / "frontend/src/pages/Topology.tsx").read_text(encoding="utf-8")
        charts = (ROOT / "frontend/src/components/charts.tsx").read_text(encoding="utf-8")
        formatter = topology.split("tooltip: { formatter: (p: any) => ", 1)[1].split(" },", 1)[0]
        helper = charts.split("export function escapeHtml(s: string) {", 1)[1].split("\n}", 1)[0]
        helper = "function escapeHtml(s) {" + helper.replace("[c]!", "[c]") + "\n}"
        heatmap = charts.split("export function Heatmap(", 1)[1].split("tooltip: { formatter: (p: any) => ", 1)[1].split(" },", 1)[0]
        payload = '<span id="injected">untrusted</span><img src="/missing" onerror="window.executed=true">'
        script = helper + "\nwindow.executed=false;const payload=" + json.dumps(payload) + ";"
        script += "const formatter=(p)=>" + formatter + ";"
        script += "const names={fixture:payload},ents=['fixture'],times=[0],fmt=undefined,fmtTime=()=>'';"
        script += "const heatmap=(p)=>" + heatmap + ";"
        script += "const outputs=[formatter({dataType:'node',data:{name:payload,ntype:payload,model:payload,status:payload,detail:payload}}),formatter({dataType:'edge',data:{linkLabel:payload}}),heatmap({value:[0,0,42]})];"
        script += "document.getElementById('checks').innerHTML=outputs.join('<br>');"
        script += "const chart=echarts.init(document.getElementById('chart'));chart.setOption({animation:false,tooltip:{formatter},series:[{type:'graph',layout:'none',data:[{name:payload,ntype:'client',status:'healthy',x:100,y:100,symbolSize:50}]}]});chart.dispatchAction({type:'showTip',seriesIndex:0,dataIndex:0});"
        html = '<!doctype html><div id="chart" style="width:600px;height:400px"></div><div id="checks"></div><script src="/echarts.js"></script><script src="/probe.js"></script>'
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel=os.environ.get("NOC_BROWSER_CHANNEL", "msedge"), headless=True)
            try:
                page = browser.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))

                def serve(route):
                    path = route.request.url.split("noc-review.invalid", 1)[-1]
                    if path == "/":
                        route.fulfill(body=html, content_type="text/html")
                    elif path == "/echarts.js":
                        route.fulfill(path=str(ROOT / "frontend/node_modules/echarts/dist/echarts.min.js"), content_type="application/javascript")
                    elif path == "/probe.js":
                        route.fulfill(body=script, content_type="application/javascript")
                    else:
                        route.abort()

                page.route("**/*", serve)
                page.goto("https://noc-review.invalid/")
                page.wait_for_load_state("networkidle")
                self.assertEqual(errors, [])
                self.assertEqual(page.locator("#injected").count(), 0)
                self.assertIn('<span id="injected">', page.locator("#checks").inner_text())
                self.assertFalse(page.evaluate("window.executed"))
            finally:
                browser.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
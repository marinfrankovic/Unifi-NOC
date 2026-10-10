"""Isolated PostgreSQL/TimescaleDB migrations; requires NOC_TEST_DATABASE_URL, never uses production settings."""
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.dont_write_bytecode = True

import asyncpg
import httpx
from noc.api import main as api_main
from noc.db import SCHEMA, _init_conn, migrate
from noc.config import settings
from noc.collector.poll import Ctx, discover_sites, refresh_selection, process_site_refresh
from noc.collector.setup import process_job
from noc.collector.store import State
from noc.secrets import decrypt_config, encrypt_config
from noc.notifications import process_notifications
from noc.collector.changes import record_change, snapshot
from noc.collector.alerts import AlertEngine
from noc.migrations import apply_migrations, bind_environment_connection


@unittest.skipUnless(os.environ.get("NOC_TEST_DATABASE_URL"), "Set a dedicated NOC_TEST_DATABASE_URL")
class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        configuration = patch.multiple(settings, controller_url="https://controller.invalid", monitored_sites=["Fixture A", "Fixture B"])
        configuration.start()
        self.addCleanup(configuration.stop)
        self.conn = await asyncpg.connect(os.environ["NOC_TEST_DATABASE_URL"])
        await _init_conn(self.conn)
        self.addAsyncCleanup(self.conn.close)
        self.assertEqual(await self.conn.fetchval("SELECT current_database()"), "noc_test", "Use a disposable noc_test database")
        await self.conn.execute("CREATE EXTENSION IF NOT EXISTS timescaledb")
        self.schema = "noc_fixture_" + uuid4().hex
        await self.conn.execute(f'CREATE SCHEMA "{self.schema}"')
        self.addAsyncCleanup(self.conn.execute, f'DROP SCHEMA "{self.schema}" CASCADE')
        await self.conn.execute(f'SET search_path TO "{self.schema}", public')
        await self.conn.execute(SCHEMA)
        await self.conn.execute("""INSERT INTO sites (name, site_id, description, slug) VALUES
            ('fixture-a', 'native-a', 'Fixture A', 'a'), ('fixture-b', 'native-b', 'Fixture B', 'b');
            INSERT INTO devices (mac, site, name, device_type) VALUES ('02:00:00:00:00:01', 'fixture-a', 'Fixture switch', 'switch');
            INSERT INTO users (username, password_hash, role) VALUES ('fixture-owner', 'fixture-hash', 'admin');
            INSERT INTO metrics (ts, site, entity_type, entity_id, metric, value)
                VALUES (now(), 'fixture-a', 'switch', 'dev:02:00:00:00:00:01', 'online', 1);
        """)

    async def test_startup_preserves_inventory_users_history_and_stable_ids(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn

        await migrate(SimpleNamespace(acquire=acquire))
        first = await self.conn.fetch("SELECT name, id FROM sites ORDER BY name")
        device_id = await self.conn.fetchval("SELECT id FROM devices")
        await migrate(SimpleNamespace(acquire=acquire))
        self.assertEqual(await self.conn.fetch("SELECT name, id FROM sites ORDER BY name"), first)
        self.assertEqual(await self.conn.fetchval("SELECT id FROM devices"), device_id)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM metrics"), 1)
        self.assertEqual(await self.conn.fetchval("SELECT role FROM users WHERE username='fixture-owner'"), "admin")
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM schema_migrations"), 6)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM connections"), 1)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM sites WHERE selected AND connection_id IS NOT NULL"), 2)
        self.assertTrue(await self.conn.fetchval("SELECT is_owner FROM users WHERE username='fixture-owner'"))

    async def test_environment_binding_preserves_selection_and_rejects_silent_replacement(self):
        await apply_migrations(self.conn)
        with patch.object(settings, "monitored_sites", ["Fixture A"]):
            first = await bind_environment_connection(self.conn)
        self.assertEqual(await self.conn.fetchval("SELECT selected FROM sites WHERE name='fixture-a'"), True)
        self.assertEqual(await self.conn.fetchval("SELECT selected FROM sites WHERE name='fixture-b'"), False)
        await self.conn.execute("UPDATE sites SET selected=false, description='Renamed site' WHERE name='fixture-a'")
        self.assertEqual(await bind_environment_connection(self.conn), first)
        self.assertEqual(await self.conn.fetchval("SELECT selected FROM sites WHERE name='fixture-a'"), False)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM connections"), 1)
        await self.conn.execute("INSERT INTO sites (name, site_id, description, slug) VALUES ('new-site', 'new-native', 'New site', 'new')")
        self.assertEqual(await self.conn.fetchval("SELECT selected FROM sites WHERE name='new-site'"), False)
        with patch.object(settings, "controller_url", "https://different.invalid"):
            with self.assertRaisesRegex(ValueError, "explicit connection migration"):
                await bind_environment_connection(self.conn)
        self.assertEqual(await self.conn.fetchval("SELECT controller_url FROM connections"), "https://controller.invalid")

    async def test_failed_migration_rolls_back_schema_and_ledger(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "0001_failure.sql").write_text("CREATE TABLE failure_fixture (id integer); SELECT 1 / 0;", encoding="utf-8")
            with self.assertRaises(asyncpg.DivisionByZeroError):
                await apply_migrations(self.conn, directory)
        self.assertIsNone(await self.conn.fetchval("SELECT to_regclass('failure_fixture')"))
        self.assertIsNone(await self.conn.fetchval("SELECT to_regclass('schema_migrations')"))
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM metrics"), 1)

    async def test_changed_or_unknown_applied_migrations_are_rejected(self):
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            source = directory / "0001_fixture.sql"
            source.write_text("CREATE TABLE checksum_fixture (id integer);", encoding="utf-8")
            await apply_migrations(self.conn, directory)
            source.write_text("CREATE TABLE checksum_fixture (id text);", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "has changed"):
                await apply_migrations(self.conn, directory)
            source.unlink()
            with self.assertRaisesRegex(ValueError, "not present"):
                await apply_migrations(self.conn, directory)
        self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM schema_migrations"), 1)

    async def test_site_native_identity_is_unique_within_each_connection(self):
        await apply_migrations(self.conn)
        connection = await self.conn.fetchval("INSERT INTO connections (name, controller_url) VALUES ('Fixture', 'https://controller.invalid') RETURNING id")
        await self.conn.execute("UPDATE sites SET connection_id=$1 WHERE name='fixture-a'", connection)
        with self.assertRaises(asyncpg.UniqueViolationError):
            await self.conn.execute("UPDATE sites SET connection_id=$1, site_id='native-a' WHERE name='fixture-b'", connection)
        other = await self.conn.fetchval("INSERT INTO connections (name, controller_url) VALUES ('Other', 'https://other.invalid') RETURNING id")
        await self.conn.execute("UPDATE sites SET connection_id=$1, site_id='native-a' WHERE name='fixture-b'", other)
        self.assertEqual(await self.conn.fetchval("SELECT count(DISTINCT id) FROM sites"), 2)

    async def test_scoped_api_does_not_leak_other_sites_or_global_metrics(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn

        await migrate(SimpleNamespace(acquire=acquire))
        await self.conn.execute("""INSERT INTO devices (mac, site, name, device_type) VALUES
            ('02:00:00:00:00:02', 'fixture-b', 'Hidden switch', 'switch');
            INSERT INTO clients (site, mac, name, online) VALUES
            ('fixture-a', '02:00:00:00:01:01', 'Visible client', true),
            ('fixture-b', '02:00:00:00:01:02', 'Hidden client', true);
            INSERT INTO metrics (ts, site, entity_type, entity_id, metric, value) VALUES
            (now(), 'fixture-b', 'switch', 'dev:02:00:00:00:00:02', 'online', 1),
            (now(), '_all', 'site', 'site:_all', 'health_score', 100),
            (now(), 'fixture-a', 'tunnel', 'tunnel:hidden-link', 'rx_bps', 123);
            INSERT INTO events (id, ts, site, severity, event_type, message, source) VALUES
            ('visible-event', now(), 'fixture-a', 'info', 'VISIBLE', 'Visible message', 'unifi'),
            ('hidden-event', now(), 'fixture-b', 'info', 'HIDDEN', 'Hidden message', 'unifi'),
            ('hidden-tunnel-event', now(), 'fixture-a', 'info', 'TUNNEL_UP', 'Secret remote site connected', 'noc');
            INSERT INTO users (username, password_hash) VALUES ('fixture-reader', 'fixture-hash');
            INSERT INTO user_site_roles (username, site_id, role) SELECT 'fixture-reader', id, 'reader' FROM sites WHERE name='fixture-a';
        """)
        await self.conn.execute("INSERT INTO kv_state (key, value) VALUES ('tunnels', $1)",
                                [{"id": "hidden-link", "members": [{"site": "fixture-a"}, {"site": "fixture-b"}]}])
        site_id = await self.conn.fetchval("SELECT id::text FROM sites WHERE name='fixture-a'")
        user = {"username": "fixture-reader", "role": "viewer", "is_owner": False, "site_roles": {site_id: "reader"}}
        with patch.dict(api_main.STATE, {"pool": self.conn}):
            api_main.app.dependency_overrides[api_main.current_user] = lambda: user
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="https://noc-fixture.invalid") as client:
                    self.assertEqual([s["name"] for s in (await client.get('/api/sites')).json()], ['fixture-a'])
                    for path in ('/api/sites/b', '/api/devices/02:00:00:00:00:02', '/api/clients/b/02:00:00:00:01:02',
                                 '/api/devices?site=b', '/api/s2s/hidden-link/live'):
                        with self.subTest(path=path):
                            self.assertEqual((await client.get(path)).status_code, 404)
                    devices = (await client.get('/api/devices')).json()
                    self.assertEqual([d['name'] for d in devices], ['Fixture switch'])
                    self.assertEqual([c['name'] for c in (await client.get('/api/clients')).json()], ['Visible client'])
                    events = (await client.get('/api/events')).json()
                    self.assertEqual(events['total'], 1)
                    self.assertEqual([e['id'] for e in events['items']], ['visible-event'])
                    self.assertEqual([e['event_type'] for e in events['types']], ['VISIBLE'])
                    for range_name in ('1h', '24h', '30d'):
                        response = await client.get('/api/metrics', params={"entity": "dev:02:00:00:00:00:01,dev:02:00:00:00:00:02,site:_all,tunnel:hidden-link",
                                                                          "metric": "online,health_score,rx_bps", "range": range_name})
                        self.assertEqual(response.status_code, 200, response.text)
                        self.assertTrue(all(s['entity_id'] == 'dev:02:00:00:00:00:01' for s in response.json()['series']))
                    top = await client.get('/api/metrics/top?entity_type=tunnel&metric=rx_bps')
                    self.assertEqual(top.status_code, 200, top.text)
                    self.assertEqual(top.json(), [])
                    topology = (await client.get('/api/topology')).json()
                    self.assertTrue(all(n['site'] == 'fixture-a' for n in topology['nodes']))
                    overview = await client.get('/api/overview')
                    self.assertEqual(overview.status_code, 200, overview.text)
                    self.assertEqual([s['name'] for s in overview.json()['sites']], ['fixture-a'])
                    self.assertEqual(overview.json()['tunnels'], [])
                    self.assertEqual((await client.get('/api/diagnostics')).status_code, 403)
                    for path in ('/api/operations','/api/wireless','/api/reliability','/api/changes','/api/maintenance','/api/search?q=Fixture'):
                        scoped=await client.get(path)
                        self.assertEqual(scoped.status_code,200,scoped.text)
                        self.assertNotIn('Hidden',scoped.text)
                    self.assertEqual((await client.get('/api/notifications')).status_code,403)
                    await self.conn.execute("UPDATE kv_state SET value='[]'::jsonb WHERE key='tunnels'")
                    historical = await client.get('/api/metrics', params={"entity": "tunnel:hidden-link", "metric": "rx_bps"})
                    self.assertEqual(historical.status_code, 200, historical.text)
                    self.assertEqual(historical.json()['series'], [])
                    user['site_roles'] = {}
                    self.assertEqual((await client.get('/api/sites')).json(), [])
                    self.assertEqual((await client.get('/api/devices')).json(), [])
            finally:
                api_main.app.dependency_overrides.clear()

    async def test_user_management_delegation_and_owner_safeguards(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn

        fixture_pool = SimpleNamespace(acquire=acquire, fetch=self.conn.fetch, fetchrow=self.conn.fetchrow,
                                       fetchval=self.conn.fetchval, execute=self.conn.execute)
        await migrate(fixture_pool)
        ids = {row['name']: str(row['id']) for row in await self.conn.fetch('SELECT name, id FROM sites')}
        user = {"username": "fixture-owner", "is_owner": True, "role": "admin", "site_roles": {}}
        with patch.dict(api_main.STATE, {"pool": fixture_pool}):
            api_main.app.dependency_overrides[api_main.current_user] = lambda: user
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url="https://noc-fixture.invalid") as client:
                    response = await client.post('/api/users', json={"username": "site-admin", "password": "fixture-password-long", "sites": {ids['fixture-a']: 'admin'}})
                    self.assertEqual(response.status_code, 201, response.text)
                    self.assertFalse(await self.conn.fetchval("SELECT is_owner FROM users WHERE username='site-admin'"))
                    self.assertEqual((await client.patch('/api/users/fixture-owner', json={"disabled": True})).status_code, 409)
                    await self.conn.execute("UPDATE sites SET selected=false WHERE name='fixture-b'")
                    assignable = await client.get('/api/users/sites')
                    self.assertEqual(assignable.status_code, 200, assignable.text)
                    self.assertEqual({site['id'] for site in assignable.json()}, set(ids.values()))
                    response = await client.post('/api/users', json={"username": "future-reader", "password": "fixture-password-long", "sites": {ids['fixture-b']: 'reader'}})
                    self.assertEqual(response.status_code, 201, response.text)
                    self.assertEqual(await self.conn.fetchval("SELECT role FROM user_site_roles WHERE username='future-reader'"), 'reader')
                    self.assertFalse(await self.conn.fetchval("SELECT selected FROM sites WHERE name='fixture-b'"))
                    user.update({"username": "future-reader", "is_owner": False, "role": "viewer", "site_roles": {ids['fixture-b']: 'reader'}})
                    self.assertEqual((await client.get('/api/sites')).json(), [])
                    self.assertEqual((await client.get('/api/site/b')).status_code, 404)
                    self.assertEqual((await client.get('/api/users/sites')).status_code, 403)
                    user.update({"username": "fixture-owner", "is_owner": True, "role": "admin", "site_roles": {}})
                    self.assertEqual((await client.put('/api/users/future-reader/sites', json={"sites": {ids['fixture-b']: 'admin'}})).status_code, 200)
                    self.assertEqual((await client.delete(f"/api/users/future-reader/sites/{ids['fixture-b']}")).status_code, 200)
                    user.update({"username": "site-admin", "is_owner": False, "site_roles": {ids['fixture-a']: 'admin'}})
                    self.assertEqual([site['id'] for site in (await client.get('/api/users/sites')).json()], [ids['fixture-a']])
                    response = await client.post('/api/users', json={"username": "site-reader", "password": "fixture-password-long", "sites": {ids['fixture-a']: 'reader'}})
                    self.assertEqual(response.status_code, 201, response.text)
                    for path, body in (('/api/users/site-reader/sites', {"sites": {ids['fixture-b']: 'admin'}}),):
                        self.assertEqual((await client.put(path, json=body)).status_code, 403)
                    self.assertEqual((await client.post('/api/users/site-reader/password', json={"password": "another-fixture-password"})).status_code, 403)
                    self.assertEqual((await client.patch('/api/users/site-reader', json={"disabled": True})).status_code, 403)
                    self.assertEqual((await client.put('/api/users/fixture-owner/sites', json={"sites": {ids['fixture-a']: 'reader'}})).status_code, 403)
                    await self.conn.execute("INSERT INTO sessions (token_hash, username, expires_at) VALUES ('fixture-session', 'site-reader', now()+interval '1 day')")
                    self.assertEqual((await client.put('/api/users/site-reader/sites', json={"sites": {ids['fixture-a']: 'admin'}})).status_code, 200)
                    self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM sessions WHERE username='site-reader'"), 0)
                    listed = (await client.get('/api/users')).json()
                    self.assertNotIn('password_hash', str(listed))
                    self.assertTrue(all(set(account['sites']).issubset({ids['fixture-a']}) for account in listed))
                    self.assertEqual((await client.delete(f"/api/users/site-reader/sites/{ids['fixture-a']}")).status_code, 200)
                    self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM user_site_roles WHERE username='site-reader'"), 0)
                    user['site_roles'] = {ids['fixture-a']: 'reader'}
                    self.assertEqual((await client.get('/api/users')).status_code, 403)
                    self.assertEqual((await client.get('/api/users/sites')).status_code, 403)
                    self.assertGreater(await self.conn.fetchval('SELECT count(*) FROM admin_audit'), 0)
            finally:
                api_main.app.dependency_overrides.clear()

    async def test_dashboard_search_matches_literal_partial_terms_and_respects_scope(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn

        fixture_pool = SimpleNamespace(acquire=acquire, fetch=self.conn.fetch, fetchrow=self.conn.fetchrow,
                                       fetchval=self.conn.fetchval, execute=self.conn.execute)
        await migrate(fixture_pool)
        ids = {row['name']: str(row['id']) for row in await self.conn.fetch('SELECT name,id FROM sites')}
        await self.conn.execute("""UPDATE devices SET name='Needle switch' WHERE site='fixture-a';
            INSERT INTO devices (mac,site,name,device_type,model_name,ip) VALUES
                ('02:00:00:00:00:02','fixture-a','Needle AP','ap','U6 Needle','192.0.2.2'),
                ('02:00:00:00:00:03','fixture-b','Hidden Needle AP','ap','Hidden model','192.0.2.3');
            INSERT INTO clients (mac,site,name,hostname,network) VALUES ('02:00:00:00:00:04','fixture-a','Needle client','station','Needle VLAN');
            INSERT INTO events (id,ts,site,severity,event_type,message,source)
                SELECT 'needle-log-' || generate_series, now()-generate_series*interval '1 second','fixture-a','info','DEVICE_ONLINE','Needle log ' || generate_series,'noc' FROM generate_series(1,120);
            INSERT INTO events (id,ts,site,severity,event_type,message,source) VALUES
                ('literal-percent',now(),'fixture-a','info','DEVICE_ONLINE','100% packet loss','noc'),
                ('hidden-log',now(),'fixture-b','info','DEVICE_ONLINE','Hidden Needle log','noc'),
                ('hidden-tunnel',now(),'fixture-a','info','TUNNEL_DOWN','Hidden Needle tunnel','noc');
            INSERT INTO alerts (site,entity_type,entity_id,kind,metric,condition,severity,state,started_at,updated_at,message)
                VALUES ('fixture-a','switch','dev:02:00:00:00:00:01','threshold','cpu_pct','> 90','warning','firing',now(),now(),'Needle alert');
            INSERT INTO configuration_changes (device_mac,device_name,site,before,after)
                VALUES ('02:00:00:00:00:01','Needle switch','fixture-a','{"version":"1"}','{"version":"Needle 2"}');
            INSERT INTO maintenance_windows (site,title,starts_at,ends_at,created_by)
                VALUES ('fixture-a','Needle maintenance',now(),now()+interval '1 hour','fixture-owner');""")
        user = {'username':'fixture-reader','is_owner':False,'role':'viewer','site_roles':{ids['fixture-a']:'reader'}}
        with patch.dict(api_main.STATE, {'pool':fixture_pool}):
            api_main.app.dependency_overrides[api_main.current_user] = lambda: user
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app),base_url='https://noc-fixture.invalid') as client:
                    response = await client.get('/api/search',params={'q':'eEdL'})
                    self.assertEqual(response.status_code,200,response.text)
                    results = response.json()
                    self.assertEqual({item['type'] for item in results},{'switch','ap','client','event','alert','change','maintenance'})
                    self.assertEqual(sum(item['type']=='event' for item in results),100)
                    self.assertNotIn('Hidden',response.text)
                    second = await client.get('/api/search',params={'q':'eEdL','offset':100})
                    self.assertEqual(sum(item['type']=='event' for item in second.json()),20)
                    self.assertFalse({item['id'] for item in results} & {item['id'] for item in second.json()})
                    literal = await client.get('/api/search',params={'q':'%'})
                    self.assertEqual([item['label'] for item in literal.json()],['100% packet loss'])
                    self.assertEqual((await client.get('/api/search',params={'q':'Hidden model'})).json(),[])
                    self.assertTrue(any(item['type']=='site' for item in (await client.get('/api/search',params={'q':'Fixture A'})).json()))
                    self.assertTrue(any(item['type']=='ap' for item in (await client.get('/api/search',params={'q':'192.0.2.2'})).json()))
                    user.update({'username':'fixture-owner','is_owner':True,'role':'admin','site_roles':{}})
                    self.assertIn('Hidden Needle AP',(await client.get('/api/search',params={'q':'Needle'})).text)
                    await self.conn.execute("UPDATE sites SET selected=false WHERE name='fixture-b'")
                    metadata = await client.get('/api/search',params={'q':'Fixture B'})
                    self.assertTrue(any(item['type']=='site' and item['detail']=='Not monitored' for item in metadata.json()))
                    self.assertFalse(any(item['type']=='ap' for item in metadata.json()))
            finally:
                api_main.app.dependency_overrides.clear()

    async def test_selection_stops_collection_and_rejects_late_telemetry(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn

        fixture_pool = SimpleNamespace(acquire=acquire, fetch=self.conn.fetch, fetchrow=self.conn.fetchrow,
                                       fetchval=self.conn.fetchval, execute=self.conn.execute)
        await migrate(fixture_pool)
        unifi = SimpleNamespace(sites=AsyncMock(return_value=([
            {"name": "fixture-a", "_id": "native-a", "desc": "Fixture A"},
            {"name": "fixture-b", "_id": "native-b", "desc": "Fixture B"},
            {"name": "excluded", "_id": "native-excluded", "desc": "Excluded site"}], [])),
            sysinfo=AsyncMock(return_value={}), info=AsyncMock(return_value={"applicationVersion": "fixture"}))
        ctx = Ctx(State(), fixture_pool, unifi)
        await discover_sites(ctx)
        self.assertEqual(set(ctx.state.sites), {"fixture-a", "fixture-b"})
        self.assertNotIn('excluded', [call.args[0] for call in unifi.sysinfo.call_args_list])
        self.assertFalse(await self.conn.fetchval("SELECT selected FROM sites WHERE name='excluded'"))
        self.assertIsNotNone(await self.conn.fetchval("SELECT connection_id FROM sites WHERE name='excluded'"))
        site_id = await self.conn.fetchval("SELECT id::text FROM sites WHERE name='fixture-a'")
        owner = {"username": "fixture-owner", "is_owner": True, "site_roles": {}}
        with patch.dict(api_main.STATE, {"pool": fixture_pool}):
            api_main.app.dependency_overrides[api_main.current_user] = lambda: owner
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url='https://noc-fixture.invalid') as client:
                    site_b = await self.conn.fetchval("SELECT id::text FROM sites WHERE name='fixture-b'")
                    audit_before = await self.conn.fetchval('SELECT count(*) FROM admin_audit')
                    invalid = {site_id: False, str(uuid4()): False}
                    self.assertEqual((await client.put('/api/monitoring/sites', json={'selections': invalid})).status_code, 404)
                    self.assertTrue(await self.conn.fetchval("SELECT selected FROM sites WHERE name='fixture-a'"))
                    self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM admin_audit'), audit_before)
                    self.assertEqual((await client.put('/api/monitoring/sites', json={'selections': {site_id: False, site_b: False}})).status_code, 200)
                    self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM sites WHERE selected'), 0)
                    self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM admin_audit'), audit_before + 2)
                    self.assertEqual((await client.put(f'/api/monitoring/sites/{site_b}', json={"selected": True})).status_code, 200)
                    self.assertEqual([s['name'] for s in (await client.get('/api/sites')).json()], ['fixture-b'])
                    self.assertEqual(len((await client.get('/api/monitoring/sites')).json()), 3)
            finally:
                api_main.app.dependency_overrides.clear()
        await refresh_selection(ctx)
        self.assertEqual(set(ctx.state.sites), {"fixture-b"})
        original = await self.conn.fetchval('SELECT count(*) FROM metrics')
        await self.conn.execute("INSERT INTO metrics (ts, site, entity_type, entity_id, metric, value) VALUES (now(), 'fixture-a', 'switch', 'late-sample', 'online', 1)")
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM metrics'), original)
        await self.conn.execute("UPDATE devices SET name='Late update' WHERE site='fixture-a'")
        self.assertEqual(await self.conn.fetchval("SELECT name FROM devices WHERE site='fixture-a'"), 'Fixture switch')

    async def test_real_login_session_enforces_site_grants_and_revocation(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn

        fixture_pool = SimpleNamespace(acquire=acquire, fetch=self.conn.fetch, fetchrow=self.conn.fetchrow,
                                       fetchval=self.conn.fetchval, execute=self.conn.execute)
        await migrate(fixture_pool)
        site_id = await self.conn.fetchval("SELECT id::text FROM sites WHERE name='fixture-a'")
        owner = {"username": "fixture-owner", "is_owner": True, "site_roles": {}}
        with patch.dict(api_main.STATE, {"pool": fixture_pool}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url='https://noc-fixture.invalid') as client:
                api_main.app.dependency_overrides[api_main.current_user] = lambda: owner
                try:
                    created = await client.post('/api/users', json={"username": "session-reader", "password": "fixture-password-long", "sites": {site_id: 'reader'}})
                    self.assertEqual(created.status_code, 201, created.text)
                finally:
                    api_main.app.dependency_overrides.clear()
                self.assertEqual((await client.get('/api/sites')).status_code, 401)
                login = {"username": "session-reader", "password": "fixture-password-long", "remember_me": True}
                self.assertEqual((await client.post('/api/auth/login', json=login)).status_code, 403)
                response = await client.post('/api/auth/login', json=login, headers={"X-Requested-With": "noc-ui"})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertFalse(response.json()['is_owner'])
                self.assertEqual(response.json()['site_roles'], {site_id: 'reader'})
                self.assertIn('HttpOnly', response.headers['set-cookie'])
                self.assertIn('Secure', response.headers['set-cookie'])
                self.assertIn('Max-Age=2592000', response.headers['set-cookie'])
                remaining = await self.conn.fetchval("SELECT EXTRACT(EPOCH FROM (expires_at-now())) FROM sessions WHERE username='session-reader'")
                self.assertAlmostEqual(float(remaining), 2592000, delta=5)
                self.assertEqual([site['name'] for site in (await client.get('/api/sites')).json()], ['fixture-a'])
                self.assertEqual((await client.get('/api/sites/b')).status_code, 404)
                self.assertEqual((await client.get('/api/users')).status_code, 403)
                self.assertEqual((await client.get('/api/monitoring/sites')).status_code, 403)
                self.assertEqual((await client.put('/api/monitoring/sites', json={'selections': {site_id: False}}, headers={'X-Requested-With':'noc-ui'})).status_code, 403)
                sessions=await client.get('/api/account/sessions')
                self.assertEqual(sessions.status_code,200,sessions.text)
                self.assertEqual(len(sessions.json()),1)
                self.assertNotIn('token_hash',sessions.text)
                self.assertEqual((await client.post('/api/account/password',json={'current_password':'wrong','new_password':'new-fixture-password-long'},headers={'X-Requested-With':'noc-ui'})).status_code,400)
                changed=await client.post('/api/account/password',json={'current_password':'fixture-password-long','new_password':'new-fixture-password-long'},headers={'X-Requested-With':'noc-ui'})
                self.assertEqual(changed.status_code,200,changed.text)
                self.assertEqual((await client.get('/api/sites')).status_code,401)
                login['password']='new-fixture-password-long'
                self.assertEqual((await client.post('/api/auth/login',json=login,headers={'X-Requested-With':'noc-ui'})).status_code,200)
                await self.conn.execute("DELETE FROM user_site_roles WHERE username='session-reader'")
                self.assertEqual((await client.get('/api/sites')).json(), [])
                await self.conn.execute("UPDATE users SET disabled=true WHERE username='session-reader'")
                self.assertEqual((await client.get('/api/sites')).status_code, 401)

    async def test_ui_onboarding_creates_owner_and_encrypts_connection(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn

        fixture_pool = SimpleNamespace(acquire=acquire, fetch=self.conn.fetch, fetchrow=self.conn.fetchrow,
                                       fetchval=self.conn.fetchval, execute=self.conn.execute)
        with TemporaryDirectory() as directory, patch.multiple(settings, ui_setup=True, secret_directory=directory):
            await migrate(fixture_pool)
            await self.conn.execute('DELETE FROM users')
            with patch.dict(api_main.STATE, {"pool": fixture_pool}):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url='https://noc-fixture.invalid',
                                             headers={"X-Requested-With": "noc-ui"}) as client:
                    self.assertTrue((await client.get('/api/setup/status')).json()['needs_owner'])
                    created = await client.post('/api/setup/owner', json={"username": "setup-owner", "password": "fixture-password-long"})
                    self.assertEqual(created.status_code, 200, created.text)
                    self.assertTrue(created.json()['is_owner'])
                    self.assertEqual((await client.post('/api/setup/owner', json={"username": "second-owner", "password": "fixture-password-long"})).status_code, 409)
                    self.assertEqual((await client.post('/api/setup/connection', json={"url": "http://controller.invalid", "api_key": "fixture-key"})).status_code, 422)
                    response = await client.post('/api/setup/connection', json={"url": "https://controller.invalid", "api_key": "fixture-key"})
                    self.assertEqual(response.status_code, 202, response.text)
                    job = await self.conn.fetchval("SELECT value FROM kv_state WHERE key='setup.connection_job'")
                    self.assertNotIn('fixture-key', str(job))
                    self.assertEqual(decrypt_config(job['encrypted'])['api_key'], 'fixture-key')
                    self.assertEqual((await client.post('/api/setup/complete')).status_code, 400)
                    self.assertEqual((await client.get('/api/setup/connection')).json()['state'], 'checking')
                    self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM users'), 1)


    async def test_notifications_retry_maintenance_and_scoped_operations(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn

        fixture_pool = SimpleNamespace(acquire=acquire, fetch=self.conn.fetch, fetchrow=self.conn.fetchrow,
                                       fetchval=self.conn.fetchval, execute=self.conn.execute)
        await migrate(fixture_pool)
        ids = {row['name']: str(row['id']) for row in await self.conn.fetch('SELECT name,id FROM sites')}
        owner = {'username':'fixture-owner','is_owner':True,'site_roles':{}}
        with TemporaryDirectory() as directory, patch.object(settings,'secret_directory',directory), patch.dict(api_main.STATE,{'pool':fixture_pool}):
            api_main.app.dependency_overrides[api_main.current_user] = lambda: owner
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app),base_url='https://noc-fixture.invalid') as client:
                    config={'enabled':True,'provider':'ntfy','url':'https://notification.invalid','topic':'fixture','token':'fixture-secret','sites':[ids['fixture-a']]}
                    self.assertEqual((await client.put('/api/notifications',json=config)).status_code,200)
                    self.assertNotIn('fixture-secret',(await client.get('/api/notifications')).text)
                    self.assertEqual((await client.post('/api/notifications/test')).status_code,200)
                    await self.conn.execute("INSERT INTO maintenance_windows (site,title,starts_at,ends_at,created_by) VALUES ('fixture-a','Fixture',now()-interval '1 hour',now()+interval '1 hour','fixture-owner')")
                    await self.conn.execute("INSERT INTO alerts (site,entity_type,entity_id,metric,condition,severity,state,message,started_at,updated_at) VALUES ('fixture-a','switch','dev:02:00:00:00:00:01','online','< 1','critical','firing','Fixture down',now(),now())")
                    transport = AsyncMock()
                    transport.__aenter__.return_value = transport
                    transport.post.return_value = httpx.Response(200,request=httpx.Request('POST','https://notification.invalid'))
                    with patch('noc.notifications.httpx.AsyncClient',return_value=transport):
                        await process_notifications(fixture_pool)
                    self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM notification_outbox WHERE status='sent'"),1)
                    self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM notification_outbox WHERE last_error='Held by maintenance window'"),1)
                    await self.conn.execute('DELETE FROM maintenance_windows')
                    await self.conn.execute("UPDATE notification_outbox SET next_attempt=now() WHERE status='pending'")
                    transport.post.side_effect = httpx.ConnectError('fixture failure')
                    with patch('noc.notifications.httpx.AsyncClient',return_value=transport):
                        await process_notifications(fixture_pool)
                    self.assertEqual(await self.conn.fetchval("SELECT attempts FROM notification_outbox WHERE status='pending'"),1)
                    self.assertEqual(await self.conn.fetchval("SELECT last_error FROM notification_outbox WHERE status='pending'"),'ConnectError')
                    transport.post.side_effect = None
                    await self.conn.execute("UPDATE notification_outbox SET next_attempt=now() WHERE status='pending'")
                    with patch('noc.notifications.httpx.AsyncClient',return_value=transport):
                        await process_notifications(fixture_pool)
                    self.assertEqual(await self.conn.fetchval("SELECT count(*) FROM notification_outbox WHERE status='sent'"),2)
                    self.assertEqual((await client.post('/api/notifications/test')).status_code,200)
                    config['url']='https://different-notification.invalid'
                    self.assertEqual((await client.put('/api/notifications',json=config)).status_code,200)
                    with patch('noc.notifications.httpx.AsyncClient',return_value=transport):
                        await process_notifications(fixture_pool)
                    self.assertGreater(await self.conn.fetchval("SELECT count(*) FROM notification_outbox WHERE status='cancelled' AND last_error='Delivery scope changed'"),0)
                    for path in ('/operations','/wireless','/search?q=Fixture','/reliability','/changes','/maintenance'):
                        response=await client.get('/api'+path)
                        self.assertEqual(response.status_code,200,response.text)
            finally:
                api_main.app.dependency_overrides.clear()

    async def test_observed_availability_and_correlated_incidents_respect_scope(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn

        pool = SimpleNamespace(acquire=acquire, fetch=self.conn.fetch, fetchrow=self.conn.fetchrow,
                               fetchval=self.conn.fetchval, execute=self.conn.execute)
        await migrate(pool)
        site_id = str(await self.conn.fetchval("SELECT id FROM sites WHERE name='fixture-a'"))
        await self.conn.execute("""DELETE FROM metrics;
            INSERT INTO devices (mac,site,name,device_type) VALUES
                ('02:00:00:00:00:03','fixture-a','No samples','ap'), ('02:00:00:00:00:04','fixture-b','Hidden device','ap');
            INSERT INTO metrics (ts,site,entity_type,entity_id,metric,value) VALUES
                (now()-interval '120 seconds','fixture-a','switch','dev:02:00:00:00:00:01','online',0),
                (now()-interval '105 seconds','fixture-a','switch','dev:02:00:00:00:00:01','online',0),
                (now()-interval '30 seconds','fixture-a','switch','dev:02:00:00:00:00:01','online',1),
                (now()-interval '15 seconds','fixture-a','switch','dev:02:00:00:00:00:01','online',1),
                (now(),'fixture-b','ap','dev:02:00:00:00:00:04','online',0);
            INSERT INTO maintenance_windows (site,title,starts_at,ends_at,created_by)
                VALUES ('fixture-a','Fixture maintenance',now()-interval '115 seconds',now()-interval '85 seconds','fixture-owner');
            INSERT INTO alerts (site,entity_type,entity_id,metric,condition,value,severity,state,message,started_at,updated_at) VALUES
                ('fixture-a','wan','wan:fixture-a:WAN','up','< 1',0,'critical','firing','Primary WAN offline',now()-interval '120 seconds',now()),
                ('fixture-a','switch','dev:02:00:00:00:00:01','online','< 1',0,'critical','firing','Related switch offline',now()-interval '105 seconds',now()),
                ('fixture-b','ap','dev:02:00:00:00:00:04','online','< 1',0,'critical','firing','Hidden alert',now(),now());""")
        with patch.dict(api_main.STATE, {'pool': pool}):
            api_main.app.dependency_overrides[api_main.current_user] = lambda: {'username': 'fixture-reader', 'is_owner': False, 'site_roles': {site_id: 'reader'}}
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app), base_url='https://noc-fixture.invalid') as client:
                    response = await client.get('/api/reliability', params={'site': 'a'})
                    self.assertEqual(response.status_code, 200, response.text)
                    devices = response.json()
                    self.assertEqual({device['name'] for device in devices}, {'Fixture switch', 'No samples'})
                    device = next(device for device in devices if device['name'] == 'Fixture switch')
                    self.assertLess(device['coverage_pct'], 1)
                    self.assertEqual(device['observed_online_pct'], 50)
                    self.assertEqual(sum(item['duration_s'] for item in device['intervals'] if item['state'] == 'OFFLINE'), 30)
                    self.assertAlmostEqual(sum(item['duration_s'] for item in device['intervals']), 86400, delta=.3)
                    self.assertEqual(device['maintenance'][0]['title'], 'Fixture maintenance')
                    empty = next(device for device in devices if device['name'] == 'No samples')
                    self.assertEqual(empty['coverage_pct'], 0)
                    self.assertEqual(empty['intervals'][0]['state'], 'UNKNOWN')
                    response = await client.get('/api/operations', params={'site': 'a'})
                    self.assertEqual(response.status_code, 200, response.text)
                    incidents = response.json()['incidents']
                    self.assertEqual(len(incidents), 1)
                    self.assertEqual(incidents[0]['primary']['message'], 'Primary WAN offline')
                    self.assertEqual(incidents[0]['related'][0]['message'], 'Related switch offline')
                    self.assertNotIn('Hidden', response.text)
                    for path in ('/api/reliability', '/api/operations', '/api/wireless', '/api/wan'):
                        allowed = await client.get(path, params={'site': 'Fixture A'})
                        self.assertEqual(allowed.status_code, 200, allowed.text)
                        self.assertNotIn('Hidden', allowed.text)
                        self.assertEqual((await client.get(path, params={'site': 'b'})).status_code, 404, path)
            finally:
                api_main.app.dependency_overrides.clear()

    async def test_configuration_snapshots_ignore_counter_noise(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn

        pool=SimpleNamespace(acquire=acquire)
        await migrate(pool)
        device={'mac':'02:00:00:00:00:01','version':'1.0','port_table':[{'port_idx':1,'speed':1000,'rx_bytes':100}]}
        ctx=SimpleNamespace(pool=pool)
        await record_change(ctx,'fixture-a',device,'Fixture switch')
        device['port_table'][0]['rx_bytes']=200
        await record_change(ctx,'fixture-a',device,'Fixture switch')
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM configuration_changes'),0)
        device['version']='1.1'
        await record_change(ctx,'fixture-a',device,'Fixture switch')
        self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM configuration_changes'),1)
        self.assertNotIn('rx_bytes',str(snapshot(device)))

    async def test_wireless_config_numeric_strings_are_normalized(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn
        pool=SimpleNamespace(acquire=acquire,fetch=self.conn.fetch,fetchval=self.conn.fetchval,execute=self.conn.execute)
        await migrate(pool)
        await self.conn.execute("INSERT INTO devices (mac,site,name,device_type,raw) VALUES ('02:00:00:00:00:03','fixture-a','Fixture AP','ap',$1)",
            {'radio_table':[{'radio':'na','ht':'80'}],'radio_table_stats':[{'radio':'na','channel':'36','cu_total':'12','tx_retries_pct':'1.2','num_sta':'3'}]})
        with patch.dict(api_main.STATE,{'pool':pool}):
            api_main.app.dependency_overrides[api_main.current_user]=lambda:{'is_owner':True,'username':'fixture-owner','site_roles':{}}
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app),base_url='https://noc-fixture.invalid') as client:
                    response=await client.get('/api/wireless')
                    self.assertEqual(response.status_code,200,response.text)
                    radio=response.json()['radios'][0]
                    self.assertEqual(radio['width'],80)
                    self.assertEqual(radio['channel'],36)
                    self.assertEqual(radio['retries'],1.2)
            finally:
                api_main.app.dependency_overrides.clear()


    async def test_site_rule_admin_cannot_change_other_sites_or_global_rules(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn
        pool=SimpleNamespace(acquire=acquire,fetch=self.conn.fetch,fetchrow=self.conn.fetchrow,fetchval=self.conn.fetchval,execute=self.conn.execute)
        await migrate(pool)
        await self.conn.execute("INSERT INTO alert_rules (id,name,entity_type,metric,op,warn,crit,for_s,clear_for_s,hysteresis) VALUES ('fixture-cpu','CPU','device','cpu_pct','>',85,95,60,60,5)")
        site_id=await self.conn.fetchval("SELECT id::text FROM sites WHERE name='fixture-a'")
        user={'username':'fixture-owner','is_owner':False,'site_roles':{site_id:'admin'}}
        with patch.dict(api_main.STATE,{'pool':pool}):
            api_main.app.dependency_overrides[api_main.current_user]=lambda:user
            try:
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app),base_url='https://noc-fixture.invalid') as client:
                    self.assertEqual((await client.put('/api/alert-rules/fixture-cpu',json={'warn':70})).status_code,403)
                    self.assertEqual((await client.put('/api/alert-rules/fixture-cpu?site=b',json={'warn':70})).status_code,404)
                    self.assertEqual((await client.put('/api/alert-rules/fixture-cpu?site=a',json={'for_s':None})).status_code,400)
                    saved=await client.put('/api/alert-rules/fixture-cpu?site=a',json={'warn':70})
                    self.assertEqual(saved.status_code,200,saved.text)
                    self.assertEqual((await client.get('/api/alert-rules?site=a')).json()[0]['warn'],70)
                    self.assertEqual(await self.conn.fetchval("SELECT warn FROM alert_rules WHERE id='fixture-cpu'"),85)
                    user['site_roles']={site_id:'reader'}
                    self.assertEqual((await client.put('/api/alert-rules/fixture-cpu?site=a',json={'warn':65})).status_code,403)
            finally:
                api_main.app.dependency_overrides.clear()
        engine=AlertEngine(SimpleNamespace(pool=pool,state=State()))
        rule=(await engine._rules())[0]
        self.assertEqual(rule['_site_overrides']['fixture-a']['warn'],70)


    async def test_first_connection_enables_all_and_refresh_preserves_exclusions(self):
        @asynccontextmanager
        async def acquire():
            yield self.conn
        pool=SimpleNamespace(acquire=acquire,fetch=self.conn.fetch,fetchrow=self.conn.fetchrow,fetchval=self.conn.fetchval,execute=self.conn.execute)
        with TemporaryDirectory() as directory, patch.multiple(settings,ui_setup=True,secret_directory=directory):
            await migrate(pool)
            legacy=[{'name':'fixture-a','_id':'native-a','desc':'Fixture A'}, {'name':'fixture-b','_id':'native-b','desc':'Fixture B'}]
            client=SimpleNamespace(info=AsyncMock(return_value={'applicationVersion':'fixture'}),sites=AsyncMock(return_value=(legacy,[])),
                                   close=AsyncMock(),sysinfo=AsyncMock(return_value={}))
            job={'id':'first','kind':'connection','encrypted':encrypt_config({'url':settings.controller_url,'api_key':'fixture-key'})}
            await self.conn.execute("INSERT INTO kv_state (key,value) VALUES ('setup.connection_job',$1)",job)
            with patch('noc.collector.setup.UniFiClient',return_value=client):
                await process_job(pool)
            self.assertTrue(await self.conn.fetchval("SELECT value FROM kv_state WHERE key='setup.complete'"))
            self.assertEqual(await self.conn.fetchval('SELECT count(*) FROM sites WHERE selected'),2)
            self.assertEqual((await self.conn.fetchval("SELECT value FROM kv_state WHERE key='setup.connection_state'"))['job_id'],'first')
            await self.conn.execute("UPDATE sites SET selected=false WHERE name='fixture-b'")
            await self.conn.execute("INSERT INTO kv_state (key,value) VALUES ('setup.connection_job',$1)",{**job,'id':'second'})
            with patch('noc.collector.setup.UniFiClient',return_value=client):
                await process_job(pool)
            self.assertFalse(await self.conn.fetchval("SELECT selected FROM sites WHERE name='fixture-b'"))
            legacy.append({'name':'future-site','_id':'native-future','desc':'Future site'})
            user={'username':'fixture-owner','is_owner':True,'site_roles':{}}
            with patch.dict(api_main.STATE,{'pool':pool}):
                api_main.app.dependency_overrides[api_main.current_user]=lambda:user
                try:
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_main.app),base_url='https://noc-fixture.invalid') as api_client:
                        self.assertEqual((await api_client.post('/api/monitoring/refresh')).status_code,202)
                        self.assertEqual((await api_client.post('/api/monitoring/refresh')).status_code,409)
                        await process_site_refresh(Ctx(State(),pool,client))
                        self.assertEqual((await api_client.get('/api/monitoring/refresh')).json()['state'],'complete')
                        self.assertEqual(len((await api_client.get('/api/monitoring/sites')).json()),3)
                        self.assertFalse(await self.conn.fetchval("SELECT selected FROM sites WHERE name='future-site'"))
                        self.assertFalse(await self.conn.fetchval("SELECT selected FROM sites WHERE name='fixture-b'"))
                        self.assertNotIn('future-site',[call.args[0] for call in client.sysinfo.call_args_list])
                        self.assertEqual((await api_client.post('/api/monitoring/refresh')).status_code,202)
                        client.sites.side_effect=RuntimeError('Fixture controller unavailable')
                        await process_site_refresh(Ctx(State(),pool,client))
                        self.assertEqual((await api_client.get('/api/monitoring/refresh')).json()['state'],'error')
                        self.assertFalse(await self.conn.fetchval("SELECT EXISTS(SELECT 1 FROM kv_state WHERE key='sites.refresh_job')"))
                        self.assertFalse(await self.conn.fetchval("SELECT selected FROM sites WHERE name='fixture-b'"))
                        user['is_owner']=False
                        self.assertEqual((await api_client.post('/api/monitoring/refresh')).status_code,403)
                        self.assertEqual((await api_client.get('/api/monitoring/refresh')).status_code,403)
                finally:
                    api_main.app.dependency_overrides.clear()


if __name__ == "__main__":
    if not os.environ.get("NOC_TEST_DATABASE_URL"):
        raise SystemExit("Set NOC_TEST_DATABASE_URL to a disposable noc_test database")
    unittest.main(verbosity=2)
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
from zoneinfo import ZoneInfoNotFoundError
import ssl

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator

from ..db import kv_set
from ..maintenance import active_window
from ..notifications import configuration
from ..secrets import encrypt_config
from .access import require_owner, require_site_admin


class NotificationConfig(BaseModel):
    enabled: bool = False
    provider: Literal['telegram', 'ntfy'] = 'ntfy'
    url: str = 'https://ntfy.sh'
    topic: str = Field(default='', max_length=200)
    token: str = Field(default='', max_length=1024)
    chat_id: str = Field(default='', max_length=100)
    severity: Literal['warning', 'critical'] = 'warning'
    sites: list[UUID] = []
    ca_pem: str = Field(default='', max_length=65536)

    @model_validator(mode='after')
    def validate_destination(self):
        parsed = urlsplit(self.url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError('Use an HTTPS notification server address')
        if self.provider == 'ntfy' and self.enabled and not self.topic:
            raise ValueError('An ntfy topic is required')
        if self.ca_pem:
            try:
                ssl.create_default_context(cadata=self.ca_pem)
            except ssl.SSLError as error:
                raise ValueError('Use a valid PEM trust certificate') from error
        return self


class MaintenanceCreate(BaseModel):
    site_id: UUID
    title: str = Field(min_length=1, max_length=200)
    starts_at: datetime
    ends_at: datetime
    repeat: Literal['none', 'daily', 'weekly'] = 'none'
    timezone: str = 'UTC'

    @model_validator(mode='after')
    def dates(self):
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as error:
            raise ValueError('Use a valid IANA timezone') from error
        if not self.starts_at.tzinfo or not self.ends_at.tzinfo or self.ends_at <= self.starts_at:
            raise ValueError('Use timezone-aware dates with an end after the start')
        if (self.ends_at - self.starts_at).total_seconds() > 7 * 86400:
            raise ValueError('Maintenance duration must be at most seven days')
        return self


def routes(get_pool, current_user, get_sites) -> APIRouter:
    router = APIRouter(prefix='/api', tags=['Operations settings'])

    @router.get('/notifications')
    async def get_configuration(user=Depends(current_user)):
        require_owner(user)
        config = await configuration(get_pool()) or {}
        return {**{key: value for key, value in config.items() if key not in ('token','ca_pem')}, 'token_configured': bool(config.get('token'))}

    @router.put('/notifications')
    async def save_configuration(body: NotificationConfig, user=Depends(current_user)):
        require_owner(user)
        old = await configuration(get_pool()) or {}
        config = body.model_dump(mode='json')
        same_destination = old.get('provider') == config['provider'] and old.get('url') == config['url']
        if not config['token'] and same_destination:
            config['token'] = old.get('token', '')
        if config['provider'] == 'telegram' and config['enabled'] and (not config['token'] or not config['chat_id']):
            raise HTTPException(400, 'Telegram bot token and chat ID are required')
        allowed = {site['id'] for site in (await get_sites(user)).values()}
        if not set(config['sites']).issubset(allowed):
            raise HTTPException(404, 'Unknown or unmonitored site')
        config['id'] = old.get('id', str(uuid4())) if same_destination else str(uuid4())
        await kv_set(get_pool(), 'notifications.config', encrypt_config(config))
        await get_pool().execute("INSERT INTO admin_audit (actor, action, target) VALUES ($1,'notification_settings','notifications')", user['username'])
        return {'ok': True}

    @router.post('/notifications/test')
    async def send_test(user=Depends(current_user)):
        require_owner(user)
        config = await configuration(get_pool())
        if not config or not config.get('enabled'):
            raise HTTPException(400, 'Enable and save a delivery channel first')
        await get_pool().execute("INSERT INTO notification_outbox (source_key, site, payload) VALUES ($1,'_all',$2)",
            'test:' + str(uuid4()), {'message': 'UniFi NOC test notification', 'severity': 'info', 'channel_id': config['id']})
        return {'queued': True}

    @router.get('/notifications/history')
    async def history(user=Depends(current_user)):
        require_owner(user)
        return [dict(row) for row in await get_pool().fetch('SELECT id, site, status, attempts, created_at, sent_at, last_error FROM notification_outbox ORDER BY id DESC LIMIT 100')]

    @router.get('/maintenance')
    async def windows(user=Depends(current_user)):
        sites = await get_sites(user)
        rows = await get_pool().fetch('SELECT * FROM maintenance_windows WHERE site=ANY($1::text[]) ORDER BY starts_at DESC', list(sites))
        return [{**dict(row), 'site_id': sites[row['site']]['id'], 'site_name': sites[row['site']]['description'], 'active': active_window(row)} for row in rows]

    @router.post('/maintenance', status_code=201)
    async def add_window(body: MaintenanceCreate, user=Depends(current_user)):
        sites = await get_sites(user)
        site = next((name for name, value in sites.items() if value['id'] == str(body.site_id)), None)
        if not site:
            raise HTTPException(404, 'Unknown or unmonitored site')
        require_site_admin(user, str(body.site_id))
        await get_pool().execute("""INSERT INTO maintenance_windows (site, title, starts_at, ends_at, repeat, timezone, created_by)
            VALUES ($1,$2,$3,$4,$5,$6,$7)""", site, body.title, body.starts_at, body.ends_at, body.repeat, body.timezone, user['username'])
        return {'ok': True}

    @router.delete('/maintenance/{window_id}')
    async def remove_window(window_id: int, user=Depends(current_user)):
        window = await get_pool().fetchrow('SELECT site FROM maintenance_windows WHERE id=$1', window_id)
        sites = await get_sites(user)
        if not window or window['site'] not in sites:
            raise HTTPException(404, 'Window not found')
        require_site_admin(user, sites[window['site']]['id'])
        await get_pool().execute('DELETE FROM maintenance_windows WHERE id=$1', window_id)
        return {'ok': True}

    return router
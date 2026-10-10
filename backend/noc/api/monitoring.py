from uuid import UUID, uuid4
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from .access import require_owner
from ..db import kv_get, kv_set


class SiteSelection(BaseModel):
    selected: bool


class SiteSelections(BaseModel):
    selections: dict[UUID, bool]


def routes(get_pool, current_user) -> APIRouter:
    router = APIRouter(prefix="/api/monitoring", tags=["Monitoring"])

    @router.get("/sites")
    async def list_sites(user=Depends(current_user)):
        require_owner(user)
        return [dict(row) for row in await get_pool().fetch("""SELECT s.id::text, s.name, s.description, s.selected,
            s.connection_id::text, c.name AS connection_name FROM sites s LEFT JOIN connections c ON c.id=s.connection_id
            ORDER BY s.description""")]

    @router.get('/refresh')
    async def refresh_status(user=Depends(current_user)):
        require_owner(user)
        return await kv_get(get_pool(), 'sites.refresh_state') or {'state': 'idle'}

    @router.post('/refresh', status_code=202)
    async def refresh_sites(user=Depends(current_user)):
        require_owner(user)
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock(424245)')
                if await kv_get(conn, 'sites.refresh_job'):
                    raise HTTPException(409, 'Site discovery is already queued or running')
                if not await conn.fetchval('SELECT EXISTS(SELECT 1 FROM connections WHERE enabled)'):
                    raise HTTPException(400, 'Connect UniFi OS first')
                job = {'id': str(uuid4()), 'requested_at': time.time()}
                await kv_set(conn, 'sites.refresh_job', job)
                await kv_set(conn, 'sites.refresh_state', {'state': 'queued', 'job_id': job['id']})
                await conn.execute("INSERT INTO admin_audit (actor,action,target) VALUES ($1,'refresh_sites','UniFi OS')", user['username'])
        return {'state': 'queued'}

    @router.put("/sites")
    async def select_sites(body: SiteSelections, user=Depends(current_user)):
        require_owner(user)
        if not body.selections or len(body.selections) > 1000:
            raise HTTPException(400, "Select between 1 and 1000 site changes")
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                sites = await conn.fetch("SELECT id, name, selected FROM sites WHERE id=ANY($1::uuid[]) ORDER BY id FOR UPDATE", list(body.selections))
                if len(sites) != len(body.selections):
                    raise HTTPException(404, "Site not found; no changes applied")
                for site in sites:
                    selected = body.selections[site['id']]
                    if selected == site['selected']:
                        continue
                    if not selected:
                        await conn.execute("""UPDATE alerts SET state='resolved', resolved_at=now(), updated_at=now(),
                            metadata=metadata || '{"resolution_reason":"monitoring_disabled"}'::jsonb
                            WHERE site=$1 AND state='firing'""", site['name'])
                    await conn.execute("UPDATE sites SET selected=$2, updated_at=now() WHERE id=$1", site['id'], selected)
                    await conn.execute("INSERT INTO admin_audit (actor, action, target, site_id) VALUES ($1,$2,$3,$4)",
                        user['username'], 'monitoring_enabled' if selected else 'monitoring_disabled', site['name'], site['id'])
        return {"ok": True}

    @router.put("/sites/{site_id}")
    async def select_site(site_id: UUID, body: SiteSelection, user=Depends(current_user)):
        require_owner(user)
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                site = await conn.fetchrow("SELECT name FROM sites WHERE id=$1 FOR UPDATE", site_id)
                if not site:
                    raise HTTPException(404, "Site not found")
                if not body.selected:
                    await conn.execute("""UPDATE alerts SET state='resolved', resolved_at=now(), updated_at=now(),
                        metadata=metadata || '{"resolution_reason":"monitoring_disabled"}'::jsonb
                        WHERE site=$1 AND state='firing'""", site['name'])
                await conn.execute("UPDATE sites SET selected=$2, updated_at=now() WHERE id=$1", site_id, body.selected)
                await conn.execute("INSERT INTO admin_audit (actor, action, target, site_id) VALUES ($1,$2,$3,$4)",
                                   user['username'], 'monitoring_enabled' if body.selected else 'monitoring_disabled', site['name'], site_id)
        return {"ok": True, "selected": body.selected}

    return router
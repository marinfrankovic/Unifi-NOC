from typing import Literal
from uuid import UUID

import anyio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .access import can_admin_site, is_owner, require_delegation, require_owner
from .auth import hash_password


class CreateUser(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: str = Field(min_length=12, max_length=72)
    sites: dict[UUID, Literal["admin", "reader"]]


class SiteGrants(BaseModel):
    sites: dict[UUID, Literal["admin", "reader"]]
    replace: bool = False


class UserState(BaseModel):
    disabled: bool


class PasswordReset(BaseModel):
    password: str = Field(min_length=12, max_length=72)


async def password_hash(password: str) -> str:
    if len(password.encode()) > 72:
        raise HTTPException(400, "Password must be at most 72 UTF-8 bytes")
    return await anyio.to_thread.run_sync(hash_password, password)


def routes(get_pool, current_user) -> APIRouter:
    router = APIRouter(prefix="/api/users", tags=["Users"])

    async def manageable_sites(user):
        rows = await get_pool().fetch("SELECT id::text FROM sites WHERE $1 OR selected", is_owner(user))
        return [row["id"] for row in rows if can_admin_site(user, row["id"])]

    async def validate_grants(user, grants):
        normalized = {str(site): role for site, role in grants.items()}
        require_delegation(user, normalized)
        available = set(await manageable_sites(user))
        if not set(normalized).issubset(available):
            raise HTTPException(404, "Unknown or unavailable site")
        return normalized

    async def grant(conn, actor, username, grants):
        for site_id, role in grants.items():
            await conn.execute("""INSERT INTO user_site_roles (username, site_id, role, granted_by)
                VALUES ($1,$2::uuid,$3,$4) ON CONFLICT (username, site_id)
                DO UPDATE SET role=EXCLUDED.role, granted_by=EXCLUDED.granted_by""", username, UUID(site_id), role, actor)
            await conn.execute("INSERT INTO admin_audit (actor, action, target, site_id) VALUES ($1,$2,$3,$4)",
                               actor, f"grant:{role}", username, UUID(site_id))
        await conn.execute("DELETE FROM sessions WHERE username=$1", username)

    @router.get("/sites")
    async def assignment_sites(user=Depends(current_user)):
        rows = await get_pool().fetch("SELECT id::text, name, description, selected FROM sites WHERE $1 OR selected ORDER BY description", is_owner(user))
        allowed = [dict(row) for row in rows if can_admin_site(user, row['id'])]
        if not is_owner(user) and not allowed:
            raise HTTPException(403, "Site administrator required")
        return allowed

    @router.get("")
    async def list_users(user=Depends(current_user)):
        allowed = await manageable_sites(user)
        if not is_owner(user) and not allowed:
            raise HTTPException(403, "Site administrator required")
        rows = await get_pool().fetch("""SELECT username, is_owner, disabled, created_at, last_login FROM users u
            WHERE $1 OR EXISTS (SELECT 1 FROM user_site_roles g WHERE g.username=u.username AND g.site_id::text=ANY($2::text[]))
            ORDER BY username""", is_owner(user), allowed)
        grants = await get_pool().fetch("""SELECT username, site_id::text, role FROM user_site_roles
            WHERE $1 OR site_id::text=ANY($2::text[])""", is_owner(user), allowed)
        return [{**dict(row), "sites": {g["site_id"]: g["role"] for g in grants if g["username"] == row["username"]}} for row in rows]

    @router.post("", status_code=201)
    async def create_user(body: CreateUser, user=Depends(current_user)):
        grants = await validate_grants(user, body.sites)
        hashed = await password_hash(body.password)
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(424244)")
                if await conn.fetchval("SELECT EXISTS(SELECT 1 FROM users WHERE username=$1)", body.username):
                    raise HTTPException(409, "Account already exists; assign sites without changing its password")
                await conn.execute("INSERT INTO users (username, password_hash, role) VALUES ($1,$2,'viewer')", body.username, hashed)
                await grant(conn, user["username"], body.username, grants)
        return {"username": body.username, "sites": grants}

    @router.put("/{username}/sites")
    async def assign_sites(username: str, body: SiteGrants, user=Depends(current_user)):
        grants = await validate_grants(user, body.sites)
        manageable = await manageable_sites(user)
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                target = await conn.fetchrow("SELECT is_owner FROM users WHERE username=$1 FOR UPDATE", username)
                if not target:
                    raise HTTPException(404, "User not found")
                if target["is_owner"]:
                    raise HTTPException(403, "Dashboard owner access is not delegated through site grants")
                if body.replace:
                    removed = await conn.fetch("""DELETE FROM user_site_roles WHERE username=$1
                        AND site_id::text=ANY($2::text[]) AND NOT(site_id::text=ANY($3::text[])) RETURNING site_id""",
                        username, manageable, list(grants))
                    for row in removed:
                        await conn.execute("INSERT INTO admin_audit (actor, action, target, site_id) VALUES ($1,'revoke',$2,$3)",
                                           user['username'], username, row['site_id'])
                await grant(conn, user["username"], username, grants)
        return {"ok": True}

    @router.delete("/{username}/sites/{site_id}")
    async def remove_site(username: str, site_id: UUID, user=Depends(current_user)):
        await validate_grants(user, {site_id: "reader"})
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute("DELETE FROM user_site_roles WHERE username=$1 AND site_id=$2", username, site_id)
                await conn.execute("DELETE FROM sessions WHERE username=$1", username)
                await conn.execute("INSERT INTO admin_audit (actor, action, target, site_id) VALUES ($1,'revoke',$2,$3)",
                                   user["username"], username, site_id)
        return {"ok": True}

    @router.patch("/{username}")
    async def set_state(username: str, body: UserState, user=Depends(current_user)):
        require_owner(user)
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(424244)")
                target = await conn.fetchrow("SELECT is_owner FROM users WHERE username=$1 FOR UPDATE", username)
                if not target:
                    raise HTTPException(404, "User not found")
                if target["is_owner"] and body.disabled:
                    owners = await conn.fetchval("SELECT count(*) FROM users WHERE is_owner AND NOT disabled")
                    if owners <= 1:
                        raise HTTPException(409, "Cannot disable the last dashboard owner")
                await conn.execute("UPDATE users SET disabled=$2 WHERE username=$1", username, body.disabled)
                await conn.execute("DELETE FROM sessions WHERE username=$1", username)
                await conn.execute("INSERT INTO admin_audit (actor, action, target) VALUES ($1,$2,$3)",
                                   user["username"], "disable" if body.disabled else "enable", username)
        return {"ok": True}

    @router.post("/{username}/password")
    async def reset_password(username: str, body: PasswordReset, user=Depends(current_user)):
        require_owner(user)
        hashed = await password_hash(body.password)
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                result = await conn.execute("UPDATE users SET password_hash=$2 WHERE username=$1", username, hashed)
                if result.endswith(" 0"):
                    raise HTTPException(404, "User not found")
                await conn.execute("DELETE FROM sessions WHERE username=$1", username)
                await conn.execute("INSERT INTO admin_audit (actor, action, target) VALUES ($1,'password_reset',$2)", user["username"], username)
        return {"ok": True}

    return router
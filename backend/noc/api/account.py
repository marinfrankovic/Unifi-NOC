from datetime import datetime, timezone

import anyio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from .auth import verify_password
from .users import password_hash


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=12, max_length=72)


def routes(get_pool, current_user) -> APIRouter:
    router=APIRouter(prefix='/api/account',tags=['Account'])

    @router.get('/sessions')
    async def sessions(user=Depends(current_user)):
        return [dict(row) for row in await get_pool().fetch('SELECT created_at, expires_at, ip, user_agent FROM sessions WHERE username=$1 ORDER BY created_at DESC',user['username'])]

    @router.post('/sessions/revoke')
    async def revoke_sessions(user=Depends(current_user)):
        await get_pool().execute('DELETE FROM sessions WHERE username=$1',user['username'])
        await get_pool().execute("INSERT INTO admin_audit (actor,action,target) VALUES ($1,'revoke_own_sessions',$1)",user['username'])
        return {'ok':True}

    @router.post('/password')
    async def change_password(body:PasswordChange,user=Depends(current_user)):
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute('SELECT pg_advisory_xact_lock(hashtextextended($1,0))','password:'+user['username'])
                attempts=await conn.fetchval("SELECT count(*) FROM admin_audit WHERE actor=$1 AND action='password_attempt' AND ts>now()-interval '15 minutes'",user['username'])
                if attempts>=10:
                    raise HTTPException(429,'Too many password-change attempts; try later')
                await conn.execute("INSERT INTO admin_audit (actor,action,target) VALUES ($1,'password_attempt',$1)",user['username'])
        row=await get_pool().fetchrow('SELECT password_hash FROM users WHERE username=$1',user['username'])
        if not row or not await anyio.to_thread.run_sync(verify_password,body.current_password,row['password_hash']):
            raise HTTPException(400,'Current password is incorrect')
        hashed=await password_hash(body.new_password)
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute('UPDATE users SET password_hash=$2 WHERE username=$1',user['username'],hashed)
                await conn.execute('DELETE FROM sessions WHERE username=$1',user['username'])
                await conn.execute("INSERT INTO admin_audit (actor,action,target) VALUES ($1,'change_own_password',$1)",user['username'])
        return {'ok':True}

    return router
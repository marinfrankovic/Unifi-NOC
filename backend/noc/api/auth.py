"""Session authentication: bcrypt passwords, opaque random session tokens (only their SHA-256 is stored)."""
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import anyio
import bcrypt
from fastapi import Depends, HTTPException, Request, Response

from ..config import settings
from .access import require_owner

COOKIE = settings.session_cookie
MAX_ATTEMPTS_PER_15M = 10
_LOGIN_LIMITER = anyio.CapacityLimiter(2)


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=12)).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ValueError:
        return False


def _hash_token(tok: str) -> str:
    return hashlib.sha256(tok.encode()).hexdigest()


_DUMMY_HASH = hash_password(secrets.token_hex(16))


def client_ip(request: Request) -> str:
    # nginx sets X-Real-IP; it is the only hop in front of the API.
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "?")


def is_https(request: Request) -> bool:
    return request.headers.get("x-forwarded-proto", request.url.scheme) == "https"


async def bootstrap_admin(pool) -> None:
    if settings.ui_setup:
        return
    if not settings.admin_password:
        return
    password_hash = await anyio.to_thread.run_sync(hash_password, settings.admin_password)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(424244)")
            if await conn.fetchval("SELECT count(*) FROM users"):
                return
            await conn.execute("INSERT INTO users (username, password_hash, role, is_owner) VALUES ($1, $2, 'admin', true)",
                               settings.admin_username, password_hash)


async def user_profile(pool, row) -> dict:
    grants = await pool.fetch("SELECT site_id::text, role FROM user_site_roles WHERE username=$1", row["username"])
    roles = {grant["site_id"]: grant["role"] for grant in grants}
    owner = bool(row.get("is_owner", False))
    return {"username": row["username"], "is_owner": owner, "site_roles": roles,
            "role": "admin" if owner or "admin" in roles.values() else "viewer"}


async def audit(pool, event: str, request: Request, username: str | None = None) -> None:
    await pool.execute("INSERT INTO auth_events (username, ip, event, user_agent) VALUES ($1,$2,$3,$4)",
                       username, client_ip(request), event, (request.headers.get("user-agent") or "")[:300])


async def login(pool, request: Request, response: Response, username: str, password: str, remember_me: bool = False) -> dict:
    try:
        _LOGIN_LIMITER.acquire_nowait()
    except anyio.WouldBlock:
        raise HTTPException(429, "Login service busy. Try again shortly.", headers={"Retry-After": "2"}) from None
    try:
        return await _login(pool, request, response, username, password, remember_me)
    finally:
        _LOGIN_LIMITER.release()


async def _login(pool, request: Request, response: Response, username: str, password: str, remember_me: bool = False) -> dict:
    ip = client_ip(request)
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"noc-login:{ip}")
            attempts = await conn.fetchval(
                "SELECT count(*) FROM auth_events WHERE ip=$1 AND event='login_attempt' AND ts > now() - interval '15 minutes'", ip)
            if attempts >= MAX_ATTEMPTS_PER_15M:
                raise HTTPException(429, "Too many login attempts. Try again later.", headers={"Retry-After": "900"})
            await audit(conn, "login_attempt", request, username)
    row = await pool.fetchrow("SELECT username, password_hash, role, is_owner, disabled FROM users WHERE username=$1", username)
    # Always run bcrypt so response timing does not reveal whether the user exists.
    ok = await anyio.to_thread.run_sync(verify_password, password, row["password_hash"] if row else _DUMMY_HASH)
    if not row or not ok or row.get("disabled", False):
        await audit(pool, "login_failed", request, username)
        raise HTTPException(401, "Invalid username or password")
    token = secrets.token_urlsafe(32)
    ttl_seconds = 30 * 24 * 3600 if remember_me else settings.session_ttl_hours * 3600
    expires = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
    await pool.execute("INSERT INTO sessions (token_hash, username, expires_at, ip, user_agent) VALUES ($1,$2,$3,$4,$5)",
                       _hash_token(token), row["username"], expires, ip, (request.headers.get("user-agent") or "")[:300])
    await pool.execute("UPDATE users SET last_login=now() WHERE username=$1", row["username"])
    await audit(pool, "login_ok", request, row["username"])
    response.set_cookie(COOKIE, token, max_age=ttl_seconds, httponly=True, secure=is_https(request),
                        samesite="strict", path="/")
    return await user_profile(pool, row)


async def logout(pool, request: Request, response: Response) -> None:
    tok = request.cookies.get(COOKIE)
    if tok:
        user = await pool.fetchval("DELETE FROM sessions WHERE token_hash=$1 RETURNING username", _hash_token(tok))
        await audit(pool, "logout", request, user)
    response.delete_cookie(COOKIE, path="/")


def make_dependencies(get_pool):
    async def current_user(request: Request) -> dict:
        tok = request.cookies.get(COOKIE)
        if not tok:
            raise HTTPException(401, "Not authenticated")
        pool = get_pool()
        row = await pool.fetchrow(
            """SELECT s.username, s.expires_at, u.role, u.is_owner, u.disabled FROM sessions s JOIN users u USING (username)
               WHERE s.token_hash=$1""", _hash_token(tok))
        if not row or row.get("disabled", False):
            raise HTTPException(401, "Session expired")
        if row["expires_at"] < datetime.now(timezone.utc):
            await pool.execute("DELETE FROM sessions WHERE token_hash=$1", _hash_token(tok))
            await audit(pool, "session_expired", request, row["username"])
            raise HTTPException(401, "Session expired")
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get("x-requested-with") != "noc-ui":
            raise HTTPException(403, "Missing anti-CSRF header")
        return await user_profile(pool, row)

    async def admin_user(user: dict = Depends(current_user)) -> dict:
        require_owner(user)
        return user

    return current_user, admin_user

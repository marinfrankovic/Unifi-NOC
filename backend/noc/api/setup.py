import ssl
from urllib.parse import urlsplit
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field, field_validator

from ..db import kv_get, kv_set
from ..config import settings
from ..secrets import decrypt_config, encrypt_config
from . import auth
from .access import require_owner
from .users import password_hash


class OwnerCreate(BaseModel):
    username: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: str = Field(min_length=12, max_length=72)


class ConnectionCreate(BaseModel):
    url: str = Field(max_length=512)
    api_key: str = Field(default="", max_length=512)
    ca_pem: str | None = Field(default=None, max_length=65536)
    tls_server_name: str = Field(default="", max_length=253, pattern=r"^[a-zA-Z0-9.-]*$")
    gateway_ssh: bool = False
    probes: bool = False
    probe_site: str = Field(default="", max_length=200)
    probe_site_id: str = Field(default="", max_length=200)

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise ValueError("Use an HTTPS UniFi OS address without credentials or a path")
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError("Invalid port")
        return value.rstrip("/")

    @field_validator("ca_pem")
    @classmethod
    def valid_certificate(cls, value):
        if value:
            try:
                ssl.create_default_context(cadata=value)
            except ssl.SSLError as error:
                raise ValueError("Upload a valid PEM certificate") from error
        return value


class CertificateRequest(BaseModel):
    url: str
    tls_server_name: str = Field(default="", max_length=253, pattern=r"^[a-zA-Z0-9.-]*$")

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        return ConnectionCreate.valid_url(value)


def routes(get_pool, current_user) -> APIRouter:
    router = APIRouter(prefix="/api/setup", tags=["Setup"])

    @router.get("/status")
    async def status():
        return {"needs_owner": not bool(await get_pool().fetchval("SELECT count(*) FROM users")), "ui_setup": settings.ui_setup}

    @router.post("/owner")
    async def create_owner(body: OwnerCreate, request: Request, response: Response):
        if request.headers.get("x-requested-with") != "noc-ui":
            raise HTTPException(403, "Missing anti-CSRF header")
        if await get_pool().fetchval("SELECT count(*) FROM users"):
            raise HTTPException(409, "The dashboard owner has already been created")
        hashed = await password_hash(body.password)
        async with get_pool().acquire() as conn:
            async with conn.transaction():
                await conn.execute("SELECT pg_advisory_xact_lock(424244)")
                if await conn.fetchval("SELECT count(*) FROM users"):
                    raise HTTPException(409, "The dashboard owner has already been created")
                await conn.execute("INSERT INTO users (username, password_hash, role, is_owner) VALUES ($1,$2,'admin',true)", body.username, hashed)
        return await auth.login(get_pool(), request, response, body.username, body.password)

    @router.get("/connection")
    async def connection_status(user=Depends(current_user)):
        require_owner(user)
        state = await kv_get(get_pool(), "setup.connection_state") or {"state": "not_configured"}
        encrypted = await kv_get(get_pool(), "connection.profile")
        profile = decrypt_config(encrypted) if encrypted else None
        configuration = {key: profile.get(key, default) for key, default in
                         (("url", ""), ("tls_server_name", ""), ("gateway_ssh", False), ("probes", False), ("probe_site", ""), ("probe_site_id", ""))} if profile else None
        if configuration is not None:
            configuration.update(has_api_key=bool(profile.get("api_key")), ca_configured=bool(profile.get("ca_pem")))
        return {**state, "connected": bool(encrypted), "configuration": configuration,
                "complete": bool(await kv_get(get_pool(), "setup.complete")),
                "selected_sites": await get_pool().fetchval("SELECT count(*) FROM sites WHERE selected")}

    @router.post("/connection", status_code=202)
    async def connect(body: ConnectionCreate, user=Depends(current_user)):
        require_owner(user)
        pending = await kv_get(get_pool(), "setup.connection_job")
        if pending:
            raise HTTPException(409, "A connection check is already running")
        encrypted = await kv_get(get_pool(), "connection.profile")
        saved = decrypt_config(encrypted) if encrypted else {}
        same_controller = saved.get("url", "").rstrip("/") == body.url
        profile = body.model_dump()
        if not profile["api_key"]:
            if not same_controller or not saved.get("api_key"):
                raise HTTPException(400, "Enter an API key when connecting a new UniFi OS instance")
            profile["api_key"] = saved["api_key"]
        if profile["ca_pem"] is None:
            profile["ca_pem"] = saved.get("ca_pem", "") if same_controller else ""
        job = {"id": str(uuid4()), "kind": "connection", "encrypted": encrypt_config(profile)}
        await kv_set(get_pool(), "setup.connection_state", {"state": "checking", "job_id": job['id']})
        await kv_set(get_pool(), "setup.connection_job", job)
        return {"state": "checking", "job_id": job['id']}

    @router.post("/certificate", status_code=202)
    async def certificate(body: CertificateRequest, user=Depends(current_user)):
        require_owner(user)
        if await kv_get(get_pool(), "setup.connection_job"):
            raise HTTPException(409, "A connection check is already running")
        await kv_set(get_pool(), "setup.certificate", {"state": "checking"})
        await kv_set(get_pool(), "setup.connection_job", {"id": str(uuid4()), "kind": "certificate", "encrypted": encrypt_config(body.model_dump())})
        return {"state": "checking"}

    @router.get("/certificate")
    async def certificate_status(user=Depends(current_user)):
        require_owner(user)
        return await kv_get(get_pool(), "setup.certificate") or {"state": "not_checked"}

    @router.post("/complete")
    async def complete(user=Depends(current_user)):
        require_owner(user)
        if not await kv_get(get_pool(), "connection.profile") or not await get_pool().fetchval("SELECT count(*) FROM sites WHERE selected"):
            raise HTTPException(400, "Connect UniFi OS and select at least one site first")
        await kv_set(get_pool(), "setup.complete", True)
        return {"ok": True}

    return router
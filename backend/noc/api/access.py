from fastapi import HTTPException


def is_owner(user: dict) -> bool:
    return user.get("is_owner") is True


def can_read_site(user: dict, site_id: str) -> bool:
    return is_owner(user) or str(site_id) in user.get("site_roles", {})


def can_admin_site(user: dict, site_id: str) -> bool:
    return is_owner(user) or user.get("site_roles", {}).get(str(site_id)) == "admin"


def require_owner(user: dict) -> None:
    if not is_owner(user):
        raise HTTPException(403, "Dashboard owner required")


def require_site_admin(user: dict, site_id: str) -> None:
    if not can_admin_site(user, site_id):
        raise HTTPException(403, "Site administrator required")


def require_delegation(user: dict, grants: dict[str, str]) -> None:
    if not grants:
        raise HTTPException(400, "At least one site assignment is required")
    for site_id, role in grants.items():
        if role not in ("admin", "reader"):
            raise HTTPException(400, "Site role must be admin or reader")
        require_site_admin(user, site_id)